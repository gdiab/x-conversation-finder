"""The offline research eval (issue #44) with fake adapters.

The eval reads stored cards read-only, runs the research step and the code's
fit checks, and reports. These tests prove it makes no X call, writes
nothing to the database it reads, uses only posts that still have their
text, keeps to its spend cap, stops a post once its removal is recorded,
counts quotes copied from the brief, and that its summary carries no text.
Synthetic data only.
"""

import dataclasses
import hashlib
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.adapters import FakeReplyAgent
from backend.adapters.plain_reply_agent import POST_QUOTE_KEY, PlainReplyAgent
from backend.database import Base, ReviewCardDB
from backend.eval.research_harness import (
    EXCLUDED_DUPLICATE,
    brief_quote_kind,
    load_research_posts,
    redact_removed,
    report_json,
    run_research,
    summarize,
    summary_markdown,
)
from backend.eval.written_harness import SpendCap, WatchSettings
from backend.models import Evidence, FounderMaterial

BRIEF = (
    "Relay delivers webhooks. Once you run many consumers, you become the one who replays every "
    "dropped event by hand. Relay can retry a failed delivery up to five times."
)
BRIEF_PROBLEM = "Once you run many consumers, you become the one who replays every dropped event by hand."
POST_TEXT = "Synthetic post: our webhook consumer drops events. How do you retry deliveries?"
TOPIC = "webhooks"
SETTINGS = {f"t:{TOPIC}": WatchSettings(BRIEF, "Relay")}
TOPICS = {f"review:t:{n}": TOPIC for n in range(3001, 3008)}


def _post(post_id: str, removed: bool = False) -> dict:
    post = {
        "id": post_id,
        "text": "[Post text removed]" if removed else POST_TEXT,
        "author": {"id": "a1", "username": "synthetic_user", "name": "Synthetic", "followers_count": 10,
                   "verified": False},
        "created_at": "2026-11-20T00:00:00+00:00",
        "like_count": 0, "reply_count": 0, "repost_count": 0, "quote_count": 0,
        "conversation_id": post_id, "reply_settings": "everyone",
    }
    if removed:
        post["text_removed"] = True
    return post


def _evidence(post_id: str) -> dict:
    return dataclasses.asdict(Evidence(
        post_id=post_id, matched_topic=TOPIC, why_it_fits="Synthetic.", confidence_score=0.1,
        confidence_inputs={}, founder_materials=[],
    ))


def _make_db(path: Path, extra_rows=()) -> Path:
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        ("review:t:3001", _post("3001")),  # skipped at research: still researched again
        ("review:t:3002", _post("3002")),
        ("review:t:3003", _post("3003", removed=True)),  # removed: never sent to a model
        ("review:x:3004", _post("3004")),  # no exported Review topic
        ("review:t:3005", _post("3005")),
        *extra_rows,
    ]
    with sessionmaker(bind=engine)() as session:
        for workflow_id, post in rows:
            session.add(ReviewCardDB(
                workflow_id=workflow_id, post_data=post, evidence_data=_evidence(post["id"]), status="skipped",
                created_at=now, updated_at=now, edit_seq=0,
            ))
        session.commit()
    engine.dispose()
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class QuotingAgent(FakeReplyAgent):
    """Proposes a scripted quote per post id; counts its calls."""

    def __init__(self, quotes):
        self.quotes = quotes
        self.calls = []

    async def research(self, post, topic, product_brief, idempotency_key):
        self.calls.append(post.id)
        evidence = await super().research(post, topic, product_brief, idempotency_key)
        evidence.founder_materials = [FounderMaterial("product_brief", BRIEF, "r", "passage_0")]
        evidence.reply_fit.brief_problem_span = self.quotes.get(post.id, evidence.reply_fit.brief_problem_span)
        return evidence


# ---------- which posts ----------


def test_posts_with_text_and_settings_are_loaded_read_only(tmp_path):
    db = _make_db(tmp_path / "cards.db", extra_rows=[
        ("review:t:3006", _post("3001")),  # a second card of post 3001 under the same settings
        ("review:t:3007", _post("3007")),  # its watch has no exported settings
    ])
    topics = {**TOPICS, "review:t:3007": "other-topic"}
    before = _digest(db)

    posts, excluded = load_research_posts(db, topics, SETTINGS)

    assert [p.post.id for p in posts] == ["3001", "3002", "3005"]
    assert excluded == Counter({
        "post_text_removed": 1, "no_review_topic": 1, EXCLUDED_DUPLICATE: 1, "no_watch_settings": 1,
    })
    assert _digest(db) == before


# ---------- the run ----------


@pytest.mark.asyncio
async def test_a_run_counts_brief_quotes_and_fit_passes_and_writes_nothing(tmp_path):
    db = _make_db(tmp_path / "cards.db")
    before = _digest(db)
    posts, excluded = load_research_posts(db, TOPICS, SETTINGS)
    agent = QuotingAgent({
        "3001": BRIEF_PROBLEM,  # copied from the brief
        "3002": "How do you retry deliveries?",  # from the post: passes
        "3005": "a quote nobody wrote anywhere",  # neither
    })

    records, stopped = await run_research(posts, SETTINGS, agent, samples=2)

    assert not stopped
    assert agent.calls == ["3001", "3001", "3002", "3002", "3005", "3005"]
    summary = summarize(records, excluded, None)
    assert summary["researched"] == 6
    assert summary["not_in_post"] == 4
    assert summary["not_in_post_share"] == round(4 / 6, 3)
    assert summary["not_in_post_from_brief"] == 2
    assert summary["not_in_post_from_brief_share"] == 0.5
    assert summary["not_in_post_from_brief_kind"] == {"verbatim": 2}
    assert summary["passed_fit_rules"] == 2
    assert summary["posts_passing_in_any_sample"] == 1
    assert _digest(db) == before


@pytest.mark.asyncio
async def test_the_summary_has_no_text_and_no_ids(tmp_path):
    db = _make_db(tmp_path / "cards.db")
    posts, excluded = load_research_posts(db, TOPICS, SETTINGS)
    records, _ = await run_research(posts, SETTINGS, QuotingAgent({"3001": BRIEF_PROBLEM}))
    text = summary_markdown(summarize(records, excluded, None))
    for forbidden in (POST_TEXT, BRIEF_PROBLEM, "retry", "3001", "review:", "synthetic_user", TOPIC):
        assert forbidden not in text
    # The full report keeps the quotes (it stays in a git-ignored directory)
    assert BRIEF_PROBLEM in report_json("before", records, summarize(records, excluded, None))


@pytest.mark.asyncio
async def test_a_removal_recorded_during_the_run_stops_the_post(tmp_path):
    db = _make_db(tmp_path / "cards.db")
    posts, _ = load_research_posts(db, TOPICS, SETTINGS)
    agent = QuotingAgent({})
    removed = {"3002"}

    records, _ = await run_research(posts, SETTINGS, agent, samples=2, is_removed=lambda pid: pid in removed)

    assert "3002" not in agent.calls
    assert [r.outcome for r in records if r.post_id == "3002"] == ["removed"]
    # A removal recorded after a post was researched empties its quotes
    removed.add("3001")
    assert redact_removed(records, lambda pid: pid in removed) == 2
    assert all(r.accepted_span is None and r.rejected_span is None for r in records if r.post_id == "3001")


@pytest.mark.asyncio
async def test_the_limit_counts_posts(tmp_path):
    posts, _ = load_research_posts(_make_db(tmp_path / "cards.db"), TOPICS, SETTINGS)
    agent = QuotingAgent({})
    await run_research(posts, SETTINGS, agent, samples=2, limit=1)
    assert agent.calls == ["3001", "3001"]


class _Completions:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        body = {"post_kind": "question", POST_QUOTE_KEY: "How do you retry deliveries?",
                "in_others_thread": False, "is_vendor_post": False, "founder_materials": []}
        return SimpleNamespace(
            model="fake-model",
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(body)), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100),
        )


@pytest.mark.asyncio
async def test_the_spend_cap_refuses_the_call_that_could_pass_it(tmp_path):
    posts, _ = load_research_posts(_make_db(tmp_path / "cards.db"), TOPICS, SETTINGS)

    def run_with(cap_usd):
        completions = _Completions()
        cap = SpendCap(cap_usd, 1e-6, 1e-6)
        client = cap.wrap(SimpleNamespace(chat=SimpleNamespace(completions=completions)))
        agent = PlainReplyAgent(openai_client=client, require_budget_meter=False)
        return cap, completions, run_research(posts, SETTINGS, agent, cap=cap, samples=1)

    # One call's worst case (prompt bound plus max output) and actual cost
    probe, _, running = run_with(1.0)
    worst = []
    original = probe.require_room
    probe.require_room = lambda usd, label: (worst.append(usd), original(usd, label))[1]
    await running
    actual = probe.calls[0].cost_usd
    assert worst[0] > actual
    # Room for two calls; the third's worst case would pass the cap
    cap, completions, running = run_with(2 * actual + worst[0] - 1e-9)
    records, stopped = await running

    assert stopped
    assert completions.calls == len(records) == 2
    assert cap.spent_usd <= cap.cap_usd
    summary = summarize(records, Counter(), cap, stopped_by_cap=stopped)
    assert summary["calls"] == 2 and summary["stopped_by_spend_cap"] is True
    assert summary["passed_fit_rules"] == 0  # no founder material in the fake answer


@pytest.mark.parametrize("span,kind", [
    (BRIEF_PROBLEM, "verbatim"),
    ("  once you run MANY consumers, you become the one  ", "verbatim"),  # case and spaces
    ("You become the one who replays every dropped event by hand, again", "partial"),
    ("our webhook consumer drops events", "none"),
    ("Relay delivers", "verbatim"),  # a short quote counts when all of it is in the brief
    ("Relay delivers events on time", "none"),  # shorter than a run, and not all in the brief
    (None, "none"),
    ("", "none"),
])
def test_brief_quote_kind(span, kind):
    assert brief_quote_kind(span, BRIEF) == kind


# ---------- the script ----------


def test_the_script_refuses_an_output_path_git_would_track(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("eval-out/\n")
    db = _make_db(tmp_path / "cards.db")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"watches": {}, "review_topics": {}}))
    script = Path(__file__).resolve().parents[2] / "scripts" / "research_harness.py"
    result = subprocess.run(
        [sys.executable, str(script), "--db", str(db), "--settings", str(settings),
         "--out-root", str(repo / "reports"), "--label", "before"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert result.returncode != 0
    assert "Refusing to write" in result.stderr
    assert not (repo / "reports").exists()
