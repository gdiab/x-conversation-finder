"""The offline redraft harness (issue #29) with fake adapters.

The harness reads stored Reviews, runs the written path (draft, check, one
redraft) and reports. These tests prove it makes no X call, writes nothing to
the database it reads, follows the workflow's redraft steps, keeps to its
spend cap, and that its summary carries no text. Synthetic data only.
"""

import dataclasses
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.adapters import FakeClaimChecker, FakeReplyAgent
from backend.adapters.fake_x_client import FakeXClient
from backend.adapters.http_x_client import HttpXClient
from backend.database import Base, ReviewCardDB
from backend.eval import written_harness as harness
from backend.eval.written_harness import (
    SpendCap,
    SpendCapReached,
    WatchSettings,
    load_stored_reviews,
    report_json,
    run_harness,
    summarize,
    summary_markdown,
)
from backend.models import ClaimGateResult, Draft, Evidence, FounderMaterial, ReplyPlan
from backend.tests.written_helpers import RELAY_ALIASES, RELAY_BRIEF, RELAY_NAME
from backend.workflows.review import redraft_steer

POST_TEXT = "Synthetic post: our webhook consumer drops events. How do you retry deliveries?"
TOPIC = "webhooks"
SETTINGS = {f"t:{TOPIC}": WatchSettings(RELAY_BRIEF, RELAY_NAME, RELAY_ALIASES, ["Signed deliveries"])}
PLAN = ReplyPlan(
    reply_type="answer_question",
    target_point="How do you retry deliveries?",
    mention_product=True,
    mention_product_reason="The brief covers retries.",
)
UNSUPPORTED = "Retries are easy to skip. Relay makes every delivery effortless and instant."


def _post(post_id: str, removed: bool = False) -> dict:
    post = {
        "id": post_id,
        "text": "[Post text removed]" if removed else POST_TEXT,
        "author": {"id": "a1", "username": "synthetic_user", "name": "Synthetic", "followers_count": 10, "verified": False},
        "created_at": "2026-11-20T00:00:00+00:00",
        "like_count": 0, "reply_count": 0, "repost_count": 0, "quote_count": 0,
        "conversation_id": post_id, "reply_settings": "everyone",
    }
    if removed:
        post["text_removed"] = True
    return post


def _evidence(post_id: str, *, draft: bool = True, plan: bool = False) -> dict:
    evidence = Evidence(
        post_id=post_id,
        matched_topic=TOPIC,
        why_it_fits="Synthetic: the post asks about retries.",
        confidence_score=0.8,
        confidence_inputs={},
        founder_materials=[FounderMaterial(
            source="product_brief", passage="Relay can retry a failed delivery up to five times.",
            relevance="retries", passage_id="passage_0",
        )],
    )
    if draft:
        evidence.draft = Draft(
            text="An old assembled synthetic draft.", segments=[],
            claim_gate=ClaimGateResult(passed=True, segments=[], failed_sentences=[], reasoning=""),
            steers_applied=[],
        )
    if plan:
        evidence.reply_plan = PLAN
        evidence.draft_mode = "written"
    return dataclasses.asdict(evidence)


def _plan_skipped_evidence(post_id: str) -> dict:
    evidence = _evidence(post_id, draft=False, plan=True)
    evidence["reply_plan_errors"] = ["synthetic: target_point is not in the post"]
    return evidence


def _make_db(path: Path) -> Path:
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        ("review:t:2001", _post("2001"), _evidence("2001"), "pending"),  # drafted before the plan step
        ("review:t:2002", _post("2002"), _evidence("2002", plan=True), "skipped"),  # written, with a plan
        ("review:t:2003", _post("2003", removed=True), _evidence("2003"), "skipped"),  # removed post
        ("review:t:2004", _post("2004"), _evidence("2004", draft=False), "skipped"),  # skipped at research
        ("review:t:2005", _post("2005"), _plan_skipped_evidence("2005"), "skipped"),  # plan invalid twice
    ]
    with sessionmaker(bind=engine)() as session:
        for workflow_id, post, evidence, status in rows:
            session.add(ReviewCardDB(
                workflow_id=workflow_id, post_data=post, evidence_data=evidence, status=status,
                created_at=now, updated_at=now, edit_seq=0,
            ))
        session.commit()
    engine.dispose()
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ScriptedAgent(FakeReplyAgent):
    """FakeReplyAgent whose written drafts can be scripted; records requests."""

    def __init__(self, texts=None):
        self._texts = list(texts or [])
        self.requests = []
        self.previous = []  # (text, passed) of the draft each request rewrites
        self.plans = 0

    async def plan(self, post, evidence, idempotency_key, feedback=None):
        self.plans += 1
        return await super().plan(post, evidence, idempotency_key, feedback)

    async def draft_written(self, request, idempotency_key=None):
        self.requests.append(request)
        draft = request.evidence.draft  # read now: the call below replaces it
        self.previous.append((draft.text, draft.claim_gate.passed) if draft else None)
        evidence = await super().draft_written(request, idempotency_key)
        if self._texts:
            evidence.draft.text = self._texts.pop(0)
            evidence.draft.written.drafter_hints = []
        return evidence


class CountingChecker(FakeClaimChecker):
    def __init__(self):
        self.calls = 0

    async def check(self, request, idempotency_key):
        self.calls += 1
        return await super().check(request, idempotency_key)


@pytest.fixture
def no_x(monkeypatch):
    """Any X call fails the test."""
    def refuse(*args, **kwargs):
        raise AssertionError("the harness must not call X")

    for cls in (FakeXClient, HttpXClient):
        for name in ("search_recent", "lookup_post", "lookup_user", "create_post"):
            if hasattr(cls, name):
                monkeypatch.setattr(cls, name, refuse)


def test_only_stored_reviews_that_reached_drafting_are_loaded(tmp_path):
    db = _make_db(tmp_path / "cards.db")

    reviews, excluded = load_stored_reviews(db)

    assert [r.workflow_id for r in reviews] == ["review:t:2001", "review:t:2002"]
    assert dict(excluded) == {"post_text_removed": 1, "not_drafted": 1, "plan_skipped": 1}
    assert reviews[1].evidence.reply_plan == PLAN
    assert reviews[0].settings_key == "t:webhooks"


@pytest.mark.asyncio
async def test_a_run_makes_no_x_call_and_leaves_the_database_unchanged(tmp_path, no_x):
    db = _make_db(tmp_path / "cards.db")
    before = _digest(db)
    reviews, excluded = load_stored_reviews(db)
    plans = {}

    records, excluded, stopped = await run_harness(
        reviews, SETTINGS, ScriptedAgent(), CountingChecker(),
        plans=plans, max_attempts=2, excluded=excluded,
    )

    assert _digest(db) == before
    assert not stopped
    assert [(r.workflow_id, r.plan_source, r.outcome) for r in records] == [
        ("review:t:2001", "planned", "passed_first"),
        ("review:t:2002", "stored", "passed_first"),
    ]
    # A card stored before the plan step is planned once and its plan cached
    assert set(plans) == {"review:t:2001"}


def test_the_database_is_opened_read_only(tmp_path, monkeypatch):
    db = _make_db(tmp_path / "cards.db")
    opened = []
    real_connect = harness.sqlite3.connect

    def spy(database, *args, **kwargs):
        opened.append((database, kwargs.get("uri")))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(harness.sqlite3, "connect", spy)
    load_stored_reviews(db)

    assert opened and all(uri.endswith("?mode=ro") and flag for uri, flag in opened)


@pytest.mark.asyncio
async def test_a_failed_draft_is_redrafted_once_with_the_workflows_steer(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"))
    agent, checker = ScriptedAgent(texts=[UNSUPPORTED]), CountingChecker()

    records, _, _ = await run_harness(reviews[1:], SETTINGS, agent, checker, plans={}, max_attempts=2)

    (record,) = records
    assert record.outcome == "passed_redraft"
    first, second = record.attempts
    assert not first.passed and first.steer is None
    assert "unsupported" in first.failure_kinds
    # The steer is the one the Review workflow builds from the same failures
    assert second.steer == redraft_steer([(s.sentence, s.reason) for s in first.sentences if not s.passed])
    assert agent.requests[1].steer == second.steer
    # The redraft is shown the failed draft with its check, as in the workflow
    assert agent.previous == [None, (UNSUPPORTED, False)]
    assert second.passed and checker.calls == 2


@pytest.mark.asyncio
async def test_two_failed_drafts_skip_and_an_identical_redraft_reuses_its_check(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"))
    agent, checker = ScriptedAgent(texts=[UNSUPPORTED, UNSUPPORTED]), CountingChecker()

    records, _, _ = await run_harness(reviews[1:], SETTINGS, agent, checker, plans={}, max_attempts=2)

    (record,) = records
    assert record.outcome == "skipped"
    assert [a.reused_check for a in record.attempts] == [False, True]
    assert checker.calls == 1


@pytest.mark.asyncio
async def test_two_product_sentences_are_reported_as_too_many_claims(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"))
    two_claims = "Acme Relay delivers webhooks to your services. Relay can retry a failed delivery up to five times."
    agent = ScriptedAgent(texts=[two_claims, two_claims])

    records, _, _ = await run_harness(reviews[1:], SETTINGS, agent, CountingChecker(), plans={}, max_attempts=2)

    summary = summarize(records, harness.Counter(), None)
    assert summary["first_draft_failure_kinds"] == {"too_many_claims": 1}
    assert summary["product_claim_sentences_per_draft"] == {"2": 2}


@pytest.mark.asyncio
async def test_cached_plans_are_reused_and_samples_repeat_the_written_path(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"))
    plans = {"review:t:2001": dataclasses.asdict(PLAN)}
    agent = ScriptedAgent()

    records, _, _ = await run_harness(reviews[:1], SETTINGS, agent, CountingChecker(), plans=plans, max_attempts=2, samples=2)

    assert [(r.sample, r.plan_source) for r in records] == [(1, "cached"), (2, "cached")]
    assert agent.plans == 0


@pytest.mark.asyncio
async def test_a_review_without_watch_settings_is_left_out(tmp_path, no_x):
    reviews, excluded = load_stored_reviews(_make_db(tmp_path / "cards.db"))

    records, excluded, _ = await run_harness(reviews, {}, ScriptedAgent(), CountingChecker(), plans={}, max_attempts=2, excluded=excluded)

    assert records == []
    assert excluded["no_watch_settings"] == 2


@pytest.mark.asyncio
async def test_the_summary_and_its_markdown_carry_no_text(tmp_path, no_x):
    reviews, excluded = load_stored_reviews(_make_db(tmp_path / "cards.db"))
    records, excluded, stopped = await run_harness(
        reviews, SETTINGS, ScriptedAgent(texts=[UNSUPPORTED]), CountingChecker(),
        plans={}, max_attempts=2, excluded=excluded,
    )

    summary = summarize(records, excluded, None, stopped_by_cap=stopped)
    shown = json.dumps(summary) + summary_markdown("before", summary)

    assert set(summary) >= {
        "runs", "posts", "excluded", "outcomes", "pass_rate", "first_draft_pass_rate", "drafts",
        "checker_calls", "first_draft_failure_kinds", "redraft_failure_kinds",
        "product_claim_sentences_per_draft", "stopped_by_spend_cap",
    }
    assert summary["outcomes"] == {"passed_first": 1, "passed_redraft": 1}
    assert summary["pass_rate"] == 1.0 and summary["first_draft_pass_rate"] == 0.5
    for text in (POST_TEXT, UNSUPPORTED, "Relay", "review:t:", PLAN.target_point, "five times"):
        assert text not in shown
    # The full report has the drafts and checks per post, for local reading
    full = json.loads(report_json("before", records, summary))
    attempt = full["posts"][1]["attempts"][0]
    assert set(attempt) >= {"text", "steer", "passed", "sentences", "failure_kinds", "claim_sentences"}
    assert set(attempt["sentences"][0]) >= {"sentence", "kind", "verdict", "triggers", "reason"}


# ---------- the spend cap ----------


def _stub_client(prompt_tokens=1000, completion_tokens=100):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            model="synthetic-model",
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
        )

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


@pytest.mark.asyncio
async def test_the_spend_cap_records_usage_and_refuses_a_call_that_could_pass_it():
    client, calls = _stub_client()
    cap = SpendCap(cap_usd=0.01, input_cost_usd=1e-6, output_cost_usd=1e-5)
    wrapped = cap.wrap(client)
    messages = [{"role": "user", "content": "x" * 100}]

    await wrapped.chat.completions.create(model="m", messages=messages, max_completion_tokens=100)
    assert cap.spent_usd == pytest.approx(1000 * 1e-6 + 100 * 1e-5)

    # Worst case of the next call: a long prompt would pass the cap
    with pytest.raises(SpendCapReached):
        await wrapped.chat.completions.create(model="m", messages=[{"role": "user", "content": "x" * 10000}], max_completion_tokens=100)
    assert len(calls) == 1  # the refused request was never sent


@pytest.mark.asyncio
async def test_the_spend_cap_stops_the_run_and_keeps_what_finished(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"))

    class CappedAgent(ScriptedAgent):
        async def draft_written(self, request, idempotency_key=None):
            if self.requests:
                raise SpendCapReached("synthetic cap")
            return await super().draft_written(request, idempotency_key)

    records, _, stopped = await run_harness(reviews[1:] * 2, SETTINGS, CappedAgent(), CountingChecker(), plans={}, max_attempts=2)

    assert stopped
    assert [r.outcome for r in records] == ["passed_first", "error"]


# ---------- the command line ----------


def test_the_script_refuses_an_output_path_git_would_track(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("eval-out/\n")
    script = Path(__file__).resolve().parents[2] / "scripts" / "redraft_harness.py"
    sys.path.insert(0, str(script.parent))
    try:
        import redraft_harness
    finally:
        sys.path.remove(str(script.parent))

    redraft_harness.ensure_ignored(repo / "eval-out" / "run" / "report.json")  # ignored: allowed
    redraft_harness.ensure_ignored(tmp_path / "outside" / "report.json")  # not in a repo: allowed
    with pytest.raises(SystemExit):
        redraft_harness.ensure_ignored(repo / "reports" / "report.json")


@pytest.mark.asyncio
async def test_a_plan_invalid_twice_is_cached_so_later_runs_leave_the_post_out(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"))

    class BadPlanner(ScriptedAgent):
        async def plan(self, post, evidence, idempotency_key, feedback=None):
            self.plans += 1
            return dataclasses.replace(PLAN, target_point="not in the post")

    plans = {}
    agent = BadPlanner()
    first, _, _ = await run_harness(reviews[:1], SETTINGS, agent, CountingChecker(), plans=plans, max_attempts=2, samples=2)
    again, _, _ = await run_harness(reviews[:1], SETTINGS, agent, CountingChecker(), plans=plans, max_attempts=2)

    assert [r.outcome for r in first] == ["plan_invalid"]  # once, not per sample
    assert agent.plans == 2  # the request and its one re-request; the second run asks nothing
    assert list(plans["review:t:2001"]) == ["invalid"]
    assert [(r.plan_source, r.outcome) for r in again] == [("invalid", "plan_invalid")]
    assert agent.requests == []


@pytest.mark.asyncio
async def test_settings_are_matched_by_account_and_topic(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"))
    other_account = {f"someone-else:{TOPIC}": SETTINGS[f"t:{TOPIC}"]}

    records, excluded, _ = await run_harness(reviews, other_account, ScriptedAgent(), CountingChecker(), plans={}, max_attempts=2)

    assert records == [] and excluded["no_watch_settings"] == 2


@pytest.mark.asyncio
async def test_a_watch_without_a_product_name_is_left_out(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"))
    unnamed = {f"t:{TOPIC}": WatchSettings(RELAY_BRIEF, "  ")}
    agent = ScriptedAgent()

    records, excluded, _ = await run_harness(reviews, unnamed, agent, CountingChecker(), plans={}, max_attempts=2)

    assert records == [] and excluded["no_product_name"] == 2
    assert agent.requests == [] and agent.plans == 0


@pytest.mark.asyncio
async def test_a_removal_recorded_during_a_run_stops_that_posts_model_calls(tmp_path, no_x):
    db = _make_db(tmp_path / "cards.db")
    reviews, _ = load_stored_reviews(db)
    assert harness.removal_recorded(db, "2003") and not harness.removal_recorded(db, "2002")
    agent = ScriptedAgent()

    records, excluded, _ = await run_harness(
        reviews, SETTINGS, agent, CountingChecker(), plans={}, max_attempts=2, samples=2,
        is_removed=lambda post_id: post_id == "2002",
    )

    assert [r.workflow_id for r in records] == ["review:t:2001", "review:t:2001"]
    assert excluded["post_text_removed"] == 1
    assert all(r.post.id != "2002" for r in agent.requests)


@pytest.mark.asyncio
async def test_finished_attempts_are_kept_when_the_redraft_fails(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"))

    class FailingRedraft(ScriptedAgent):
        async def draft_written(self, request, idempotency_key=None):
            if request.steer:
                raise SpendCapReached("synthetic cap")
            return await super().draft_written(request, idempotency_key)

    records, _, stopped = await run_harness(
        reviews[1:], SETTINGS, FailingRedraft(texts=[UNSUPPORTED]), CountingChecker(), plans={}, max_attempts=2,
    )

    (record,) = records
    assert stopped and record.outcome == "error"
    assert len(record.attempts) == 1 and not record.attempts[0].passed
    summary = summarize(records, harness.Counter(), None, stopped_by_cap=True)
    # The finished first draft counts; the cut-off run is not a finished run
    assert summary["first_draft_pass_rate"] == 0.0
    assert summary["pass_rate"] is None
    assert summary["first_draft_failure_kinds"] == {"unsupported": 1}


def test_failure_kinds_ignore_the_checkers_own_words():
    from backend.models.written import WrittenCheckResult

    result = WrittenCheckResult(passed=False, failures=[[
        "Some sentence.",
        "Checked as a product claim (names the product). The brief does not support it: "
        "this may be more than one sentence and the reply must have at most 3 sentences (got 4)",
    ]])

    assert harness.failure_kinds(result) == ["unsupported"]


def test_the_summary_refuses_a_label_that_is_not_a_short_name():
    assert harness.safe_label("after-2") and not harness.safe_label("a post about x")
    with pytest.raises(ValueError):
        summary_markdown("review:t:2001 text", {})
    assert harness._model_label("gpt-4o-mini-2024-07-18") == "gpt-4o-mini-2024-07-18"
    assert harness._model_label("some text with spaces") == "(unrecognized model name)"
