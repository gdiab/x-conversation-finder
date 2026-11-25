"""The offline redraft harness (issue #29) with fake adapters.

The harness reads stored Reviews, runs the written path (draft, check, one
redraft) and reports. These tests prove it makes no X call, writes nothing to
the database it reads, follows the workflow's redraft steps, keeps to its
spend cap, and that its summary carries no text. Synthetic data only.
"""

import dataclasses
import hashlib
import json
import os
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
# The watch topic of each Review, as export-settings reads it from Temporal
TOPICS = {f"review:t:{n}": TOPIC for n in range(2001, 2006)}
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


def test_settings_follow_the_reviews_watch_topic_not_the_model_written_one(tmp_path):
    db = _make_db(tmp_path / "cards.db")

    reviews, excluded = load_stored_reviews(db, {"review:t:2001": "deliveries"})

    assert [(r.workflow_id, r.topic) for r in reviews] == [("review:t:2001", "deliveries")]
    assert reviews[0].evidence.matched_topic == TOPIC  # what research wrote; not used
    assert excluded["no_review_topic"] == 1  # review:t:2002 is not in the export


def test_only_stored_reviews_that_reached_drafting_are_loaded(tmp_path):
    db = _make_db(tmp_path / "cards.db")

    reviews, excluded = load_stored_reviews(db, TOPICS)

    assert [r.workflow_id for r in reviews] == ["review:t:2001", "review:t:2002"]
    assert dict(excluded) == {"post_text_removed": 1, "not_drafted": 1, "plan_skipped": 1}
    assert reviews[1].evidence.reply_plan == PLAN
    assert reviews[0].settings_key == "t:webhooks"


@pytest.mark.asyncio
async def test_a_run_makes_no_x_call_and_leaves_the_database_unchanged(tmp_path, no_x):
    db = _make_db(tmp_path / "cards.db")
    before = _digest(db)
    reviews, excluded = load_stored_reviews(db, TOPICS)
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
    load_stored_reviews(db, TOPICS)

    assert opened and all(uri.endswith("?mode=ro") and flag for uri, flag in opened)


@pytest.mark.asyncio
async def test_a_failed_draft_is_redrafted_once_with_the_workflows_steer(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)
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
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)
    agent, checker = ScriptedAgent(texts=[UNSUPPORTED, UNSUPPORTED]), CountingChecker()

    records, _, _ = await run_harness(reviews[1:], SETTINGS, agent, checker, plans={}, max_attempts=2)

    (record,) = records
    assert record.outcome == "skipped"
    assert [a.reused_check for a in record.attempts] == [False, True]
    assert checker.calls == 1


@pytest.mark.asyncio
async def test_two_product_sentences_are_reported_as_too_many_claims(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)
    two_claims = "Acme Relay delivers webhooks to your services. Relay can retry a failed delivery up to five times."
    agent = ScriptedAgent(texts=[two_claims, two_claims])

    records, _, _ = await run_harness(reviews[1:], SETTINGS, agent, CountingChecker(), plans={}, max_attempts=2)

    summary = summarize(records, harness.Counter(), None)
    assert summary["first_draft_failure_kinds"] == {"too_many_claims": 1}
    assert summary["product_claim_sentences_per_draft"] == {"2": 2}


@pytest.mark.asyncio
async def test_cached_plans_are_reused_and_samples_repeat_the_written_path(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)
    plans = {"review:t:2001": dataclasses.asdict(PLAN)}
    agent = ScriptedAgent()

    records, _, _ = await run_harness(reviews[:1], SETTINGS, agent, CountingChecker(), plans=plans, max_attempts=2, samples=2)

    assert [(r.sample, r.plan_source) for r in records] == [(1, "cached"), (2, "cached")]
    assert agent.plans == 0


@pytest.mark.asyncio
async def test_a_review_without_watch_settings_is_left_out(tmp_path, no_x):
    reviews, excluded = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)

    records, excluded, _ = await run_harness(reviews, {}, ScriptedAgent(), CountingChecker(), plans={}, max_attempts=2, excluded=excluded)

    assert records == []
    assert excluded["no_watch_settings"] == 2


@pytest.mark.asyncio
async def test_the_summary_and_its_markdown_carry_no_text(tmp_path, no_x):
    reviews, excluded = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)
    records, excluded, stopped = await run_harness(
        reviews, SETTINGS, ScriptedAgent(texts=[UNSUPPORTED]), CountingChecker(),
        plans={}, max_attempts=2, excluded=excluded,
    )

    summary = summarize(records, excluded, None, stopped_by_cap=stopped)
    shown = json.dumps(summary) + summary_markdown(summary)

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
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)

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
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)

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
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)
    other_account = {f"someone-else:{TOPIC}": SETTINGS[f"t:{TOPIC}"]}

    records, excluded, _ = await run_harness(reviews, other_account, ScriptedAgent(), CountingChecker(), plans={}, max_attempts=2)

    assert records == [] and excluded["no_watch_settings"] == 2


@pytest.mark.asyncio
async def test_a_watch_without_a_product_name_is_left_out(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)
    unnamed = {f"t:{TOPIC}": WatchSettings(RELAY_BRIEF, "  ")}
    agent = ScriptedAgent()

    records, excluded, _ = await run_harness(reviews, unnamed, agent, CountingChecker(), plans={}, max_attempts=2)

    assert records == [] and excluded["no_product_name"] == 2
    assert agent.requests == [] and agent.plans == 0


@pytest.mark.asyncio
async def test_a_removal_recorded_during_a_run_stops_that_posts_model_calls(tmp_path, no_x):
    db = _make_db(tmp_path / "cards.db")
    reviews, _ = load_stored_reviews(db, TOPICS)
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
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)

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
    assert summary_markdown({}).startswith("### Redraft harness run\n")


# ---------- review round 2 ----------


@pytest.mark.asyncio
async def test_a_removal_recorded_mid_run_stops_the_next_model_call(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)
    agent, checker = ScriptedAgent(), CountingChecker()
    removed = {"now": False}

    class RemovedWhileDrafting(ScriptedAgent):
        async def draft_written(self, request, idempotency_key=None):
            evidence = await agent.draft_written(request, idempotency_key)
            removed["now"] = True  # the app records the removal during the draft call
            return evidence

    records, excluded, _ = await run_harness(
        reviews[1:], SETTINGS, RemovedWhileDrafting(), checker, plans={}, max_attempts=2, samples=2,
        is_removed=lambda post_id: removed["now"],
    )

    assert checker.calls == 0  # the draft quoting the post is not sent to the checker
    assert len(agent.requests) == 1  # nor drafted again for the second sample
    assert [r.outcome for r in records] == [] and excluded["post_text_removed"] == 1


@pytest.mark.asyncio
async def test_the_watchs_saved_attempt_limit_is_used(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)
    one_try = {f"t:{TOPIC}": dataclasses.replace(SETTINGS[f"t:{TOPIC}"], claim_gate_max_attempts=1)}

    records, _, _ = await run_harness(
        reviews[1:], one_try, ScriptedAgent(texts=[UNSUPPORTED]), CountingChecker(), plans={}, max_attempts=2,
    )

    (record,) = records
    assert record.outcome == "skipped" and len(record.attempts) == 1


def test_too_many_claims_is_counted_from_the_sentences_not_the_reason():
    from backend.models.written import SentenceCheck, WrittenCheckResult

    reason = (
        "Checked as a product claim (the checker read it as a statement about the product). "
        "The brief does not support it: synthetic. The reply must have at most 1 product claim, and 2 sentences "
        "are checked as product claims."
    )
    result = WrittenCheckResult(
        passed=False,
        sentences=[SentenceCheck("A.", False, "claim", reason), SentenceCheck("B.", False, "claim", reason)],
        failures=[["A.", reason], ["B.", reason]],
    )

    assert harness.failure_kinds(result) == ["too_many_claims", "unsupported"]


@pytest.mark.asyncio
async def test_a_call_without_usable_usage_counts_at_its_worst_case():
    client, _ = _stub_client(prompt_tokens=0, completion_tokens=0)
    cap = SpendCap(cap_usd=1.0, input_cost_usd=1e-6, output_cost_usd=1e-5)

    await cap.wrap(client).chat.completions.create(
        model="m", messages=[{"role": "user", "content": "x" * 100}], max_completion_tokens=100,
    )

    (call,) = cap.calls
    assert call.input_tokens > 100 and call.output_tokens == 100


@pytest.mark.parametrize("cap_usd", [float("nan"), float("inf"), -0.01])
def test_a_spend_cap_that_cannot_limit_is_refused(cap_usd):
    with pytest.raises(ValueError):
        SpendCap(cap_usd, 1e-6, 1e-6)


def test_provider_model_names_stay_out_of_the_pasteable_summary():
    client, _ = _stub_client()
    cap = SpendCap(cap_usd=1.0, input_cost_usd=1e-6, output_cost_usd=1e-5)
    cap.calls.append(harness.CallRecord("draft", "review:account:123", 1, 1, 0.0))

    summary = summarize([], harness.Counter(), cap)

    assert "review:account:123" not in json.dumps(summary) + summary_markdown(summary)
    assert json.loads(report_json("x1", [], summary, ["review:account:123"]))["models"] == ["review:account:123"]


def test_the_script_refuses_to_write_when_git_fails(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[2] / "scripts" / "redraft_harness.py"
    sys.path.insert(0, str(script.parent))
    try:
        import redraft_harness
    finally:
        sys.path.remove(str(script.parent))

    def unsafe(*args, **kwargs):
        return subprocess.CompletedProcess(args, 128, "", "fatal: detected dubious ownership in repository")

    monkeypatch.setattr(redraft_harness.subprocess, "run", unsafe)
    with pytest.raises(SystemExit):
        redraft_harness.ensure_ignored(tmp_path / "report.json")



# ---------- review round 3 ----------


@pytest.mark.asyncio
async def test_each_run_records_its_calls_and_cost(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)
    client, _ = _stub_client(prompt_tokens=1000, completion_tokens=100)
    cap = SpendCap(cap_usd=1.0, input_cost_usd=1e-6, output_cost_usd=1e-5)
    wrapped = cap.wrap(client)

    class PayingAgent(ScriptedAgent):
        async def draft_written(self, request, idempotency_key=None):
            await wrapped.chat.completions.create(model="m", messages=[], max_completion_tokens=100)
            return await super().draft_written(request, idempotency_key)

    records, _, _ = await run_harness(
        reviews[1:], SETTINGS, PayingAgent(texts=[UNSUPPORTED]), CountingChecker(),
        plans={}, max_attempts=2, cap=cap,
    )

    (record,) = records
    assert record.model_calls == 2  # the draft and the redraft (the fake checker is free)
    assert record.cost_usd == pytest.approx(2 * (1000 * 1e-6 + 100 * 1e-5))
    full = json.loads(report_json("before", records, summarize(records, harness.Counter(), cap)))
    assert full["posts"][0]["cost_usd"] == record.cost_usd


@pytest.mark.asyncio
async def test_a_saved_attempt_limit_of_zero_is_kept(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)
    none_allowed = {f"t:{TOPIC}": dataclasses.replace(SETTINGS[f"t:{TOPIC}"], claim_gate_max_attempts=0)}
    agent = ScriptedAgent()

    records, _, _ = await run_harness(reviews[1:], none_allowed, agent, CountingChecker(), plans={}, max_attempts=2)

    assert [r.outcome for r in records] == ["skipped"] and agent.requests == []


@pytest.mark.asyncio
async def test_an_interrupted_run_keeps_its_plan(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)

    class FailingRedraft(ScriptedAgent):
        async def draft_written(self, request, idempotency_key=None):
            if request.steer:
                raise RuntimeError("synthetic provider error")
            return await super().draft_written(request, idempotency_key)

    records, _, _ = await run_harness(
        reviews[1:], SETTINGS, FailingRedraft(texts=[UNSUPPORTED]), CountingChecker(), plans={}, max_attempts=2,
    )

    (record,) = records
    assert (record.outcome, record.plan_source, record.mention_product) == ("error", "stored", True)


def test_labels_and_model_names_with_ids_are_refused():
    assert harness.safe_label("after2")
    for label in ("1000000000000000001", "run-1000000000", "a/b", "x" * 41):
        assert not harness.safe_label(label)



# ---------- review round 4: what the harness keeps of a removed post ----------


@pytest.mark.asyncio
async def test_a_post_removed_after_a_finished_attempt_leaves_no_text_and_no_cached_plan(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)
    checker = CountingChecker()
    removed = {"now": False}

    class RemovedAfterTheCheck(CountingChecker):
        async def check(self, request, idempotency_key):
            response = await checker.check(request, idempotency_key)
            removed["now"] = True
            return response

    plans = {}
    records, excluded, _ = await run_harness(
        reviews[:1], SETTINGS, ScriptedAgent(texts=[UNSUPPORTED]), RemovedAfterTheCheck(),
        plans=plans, max_attempts=2, is_removed=lambda post_id: removed["now"],
    )

    (record,) = records
    assert record.outcome == "removed" and excluded["post_text_removed"] == 1
    (attempt,) = record.attempts
    assert attempt.text == "" and attempt.reasoning == ""
    assert all(s.sentence == "" and s.reason == "" for s in attempt.sentences)
    assert attempt.failure_kinds == ["unsupported"]  # counts stay
    assert plans == {}  # the cached plan quoted the post
    shown = report_json("x1", records, summarize(records, excluded, None))
    assert POST_TEXT not in shown and UNSUPPORTED not in shown and PLAN.target_point not in shown
    assert summarize(records, excluded, None)["first_draft_pass_rate"] is None


@pytest.mark.asyncio
async def test_a_removal_recorded_after_a_posts_runs_is_applied_before_the_report(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)
    finished = []

    def removed(post_id):
        # 2001 is removed once the harness has moved on to the next post
        return post_id == "2001" and len(finished) > 0

    class Tracking(ScriptedAgent):
        async def draft_written(self, request, idempotency_key=None):
            if request.post.id == "2002":
                finished.append(True)
            return await super().draft_written(request, idempotency_key)

    records, excluded, _ = await run_harness(
        reviews, SETTINGS, Tracking(), CountingChecker(), plans={}, max_attempts=2, samples=2, is_removed=removed,
    )

    assert [(r.post_id, r.outcome) for r in records] == [
        ("2001", "removed"), ("2001", "removed"), ("2002", "passed_first"), ("2002", "passed_first"),
    ]
    assert excluded["post_text_removed"] == 1  # 2001, once for its two runs
    assert all(a.text == "" for r in records[:2] for a in r.attempts)


@pytest.mark.asyncio
async def test_a_post_removed_before_its_first_check_still_reports_its_cost(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)
    client, _ = _stub_client()
    cap = SpendCap(cap_usd=1.0, input_cost_usd=1e-6, output_cost_usd=1e-5)
    wrapped = cap.wrap(client)
    removed = {"now": False}

    class PaidDraft(ScriptedAgent):
        async def draft_written(self, request, idempotency_key=None):
            await wrapped.chat.completions.create(model="m", messages=[], max_completion_tokens=10)
            removed["now"] = True
            return await super().draft_written(request, idempotency_key)

    records, _, _ = await run_harness(
        reviews[1:], SETTINGS, PaidDraft(), CountingChecker(), plans={}, max_attempts=2, cap=cap,
        is_removed=lambda post_id: removed["now"],
    )

    (record,) = records
    assert record.outcome == "removed" and record.attempts == []
    assert record.model_calls == 1 and record.cost_usd > 0



# ---------- review round 5: removals recorded between runs ----------


def test_cached_plans_of_removed_posts_are_pruned_also_when_no_run_touched_them():
    plans = {"review:t:2001": {"reply_type": "x"}, "review:t:2003": {"reply_type": "x"}, "odd-key": {}}

    pruned = harness.prune_removed_plans(plans, lambda post_id: post_id == "2003")

    assert pruned == 1 and set(plans) == {"review:t:2001", "odd-key"}


@pytest.mark.asyncio
async def test_a_removal_recorded_between_runs_prunes_the_plan_the_first_run_cached(tmp_path, no_x):
    db = _make_db(tmp_path / "cards.db")
    reviews, _ = load_stored_reviews(db, TOPICS)
    plans = {}
    await run_harness(reviews[:1], SETTINGS, ScriptedAgent(), CountingChecker(), plans=plans, max_attempts=2)
    assert "review:t:2001" in plans

    # The app records the removal; the next run leaves the card out at load time
    await run_harness(
        [r for r in reviews if r.post.id != "2001"], SETTINGS, ScriptedAgent(), CountingChecker(),
        plans=plans, max_attempts=2, is_removed=lambda post_id: post_id == "2001",
    )

    assert "review:t:2001" not in plans


@pytest.mark.asyncio
async def test_an_earlier_report_is_redacted_for_a_removal_recorded_since(tmp_path, no_x):
    reviews, _ = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)
    records, excluded, _ = await run_harness(
        reviews, SETTINGS, ScriptedAgent(texts=[UNSUPPORTED]), CountingChecker(), plans={}, max_attempts=2,
    )
    report = json.loads(report_json("before", records, summarize(records, excluded, None)))
    for post in report["posts"]:
        del post["post_id"]  # a report from before the field existed: the id comes from the workflow id

    changed = harness.redact_removed_report(report, lambda post_id: post_id == "2001")
    again = harness.redact_removed_report(report, lambda post_id: post_id == "2001")

    assert changed and not again
    first, second = report["posts"]
    assert first["outcome"] == "removed" and all(a["text"] == "" for a in first["attempts"])
    assert POST_TEXT not in json.dumps(first)
    assert second["outcome"] != "removed" and second["attempts"][0]["text"]



# ---------- review round 6 ----------


def test_a_plan_skipped_only_by_needs_written_drafts_stays_in_the_sample(tmp_path):
    db = tmp_path / "cards.db"
    _make_db(db)
    engine = create_engine(f"sqlite:///{db}")
    evidence = _evidence("2006", draft=False, plan=True)
    evidence["reply_plan_skip_rule"] = "needs_written_drafts"
    now = datetime.now(timezone.utc).isoformat()
    with sessionmaker(bind=engine)() as session:
        session.add(ReviewCardDB(
            workflow_id="review:t:2006", post_data=_post("2006"), evidence_data=evidence, status="skipped",
            created_at=now, updated_at=now, edit_seq=0,
        ))
        session.commit()
    engine.dispose()

    reviews, excluded = load_stored_reviews(db, {**TOPICS, "review:t:2006": TOPIC})

    assert "review:t:2006" in [r.workflow_id for r in reviews]
    assert excluded["plan_skipped"] == 1  # only the invalid plan (2005)


def test_a_key_feature_quoted_in_a_reason_is_not_read_as_a_failure_kind():
    from backend.models.written import SentenceCheck, WrittenCheckResult

    reason = (
        'Checked as a product claim (uses a key feature ("The reply is empty")). '
        "The brief does not support it: synthetic."
    )
    result = WrittenCheckResult(
        passed=False, sentences=[SentenceCheck("S.", False, "claim", reason)], failures=[["S.", reason]],
    )

    assert harness.failure_kinds(result) == ["unsupported"]


@pytest.mark.asyncio
async def test_redacting_an_earlier_report_recounts_its_summary(tmp_path, no_x):
    reviews, excluded = load_stored_reviews(_make_db(tmp_path / "cards.db"), TOPICS)
    records, excluded, _ = await run_harness(
        reviews, SETTINGS, ScriptedAgent(), CountingChecker(), plans={}, max_attempts=2, excluded=excluded,
    )
    summary = summarize(records, excluded, None)
    summary["cost_usd"] = 0.5  # paid, kept
    report = json.loads(report_json("before", records, summary))
    assert report["summary"]["outcomes"] == {"passed_first": 2}

    harness.redact_removed_report(report, lambda post_id: post_id == "2001")

    assert report["summary"]["outcomes"] == {"passed_first": 1, "removed": 1}
    assert report["summary"]["pass_rate"] == 1.0 and report["summary"]["drafts"] == 1
    assert report["summary"]["cost_usd"] == 0.5
    assert report["summary"]["checker_calls"] == summary["checker_calls"]  # paid calls stay counted
    assert report["summary"]["removals_applied_after_run"] is True
    removed_before = summary["excluded"].get("post_text_removed", 0)
    assert report["summary"]["excluded"]["post_text_removed"] == removed_before + 1
    harness.redact_removed_report(report, lambda post_id: post_id == "2001")  # a later sweep
    assert report["summary"]["excluded"]["post_text_removed"] == removed_before + 1  # not counted again


def test_the_sweep_goes_on_past_a_report_it_cannot_read(tmp_path, capsys):
    script = Path(__file__).resolve().parents[2] / "scripts" / "redraft_harness.py"
    sys.path.insert(0, str(script.parent))
    try:
        import redraft_harness
    finally:
        sys.path.remove(str(script.parent))
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "report.json").write_text("{not json")
    (tmp_path / "b").mkdir()
    good = {"summary": {}, "posts": [{"workflow_id": "review:t:2001", "outcome": "passed_first",
                                      "attempts": [{"text": "synthetic draft", "sentences": []}]}]}
    (tmp_path / "b" / "report.json").write_text(json.dumps(good))

    complete = redraft_harness.sweep_earlier_reports(tmp_path, lambda post_id: post_id == "2001")

    assert complete is False
    swept = json.loads((tmp_path / "b" / "report.json").read_text())
    assert swept["posts"][0]["attempts"][0]["text"] == ""
    assert swept["summary"]["summary_stale"] is True  # too old a shape to recount
    assert "could not apply recorded removals" in capsys.readouterr().err



# ---------- review round 7 ----------


def test_the_script_never_writes_through_a_symlink_or_over_the_database(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "redraft_harness.py"
    sys.path.insert(0, str(script.parent))
    try:
        import redraft_harness
    finally:
        sys.path.remove(str(script.parent))
    db = tmp_path / "cards.db"
    db.write_text("synthetic database")
    link = tmp_path / "out" / "report.json"
    link.parent.mkdir()
    link.symlink_to(db)

    with pytest.raises(SystemExit):
        redraft_harness.safe_write(link, "{}")
    redraft_harness.PROTECTED.append(db.resolve())
    try:
        with pytest.raises(SystemExit):
            redraft_harness.safe_write(db, "{}")
    finally:
        redraft_harness.PROTECTED.remove(db.resolve())
    hard = tmp_path / "out" / "hard.json"
    os.link(db, hard)
    redraft_harness.PROTECTED.append(db.resolve())
    try:
        with pytest.raises(SystemExit):
            redraft_harness.safe_write(hard, "{}")
    finally:
        redraft_harness.PROTECTED.remove(db.resolve())
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("synthetic tracked file")
    os.link(tracked, tmp_path / "out" / "linked.json")
    with pytest.raises(SystemExit):
        redraft_harness.safe_write(tmp_path / "out" / "linked.json", "{}")
    assert tracked.read_text() == "synthetic tracked file"
    assert db.read_text() == "synthetic database"
    redraft_harness.safe_write(tmp_path / "out" / "summary.md", "ok")  # outside a repo: allowed
    assert (tmp_path / "out" / "summary.md").read_text() == "ok"



def test_per_call_kind_costs_are_rounded_once_after_summing():
    cap = SpendCap(cap_usd=1.0, input_cost_usd=1e-6, output_cost_usd=1e-6)
    cap.calls.extend(harness.CallRecord("check", "m", 0, 0, 4e-7) for _ in range(100))

    summary = summarize([], harness.Counter(), cap)

    assert summary["calls"]["check"]["cost_usd"] == pytest.approx(4e-5)
