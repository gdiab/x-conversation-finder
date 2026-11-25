"""
Issue #8: count every paid attempt; size inference reservations from the prompt.

All calls are stubbed: the OpenAI client returns a bare JSON object string in
choices[0].message.content (JSON mode), as test_plain_reply_agent.py does, and
the X client is a scripted port implementation. Post text is synthetic.
"""

import dataclasses
import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from backend.activities.reply_activities import (
    DraftReplyInput,
    PlanReplyInput,
    ReplyActivities,
    ResearchPostInput,
)
from backend.activities.x_activities import (
    LookupPostInput,
    SearchRecentInput,
    XActivities,
)
from backend.adapters.plain_reply_agent import PlainReplyAgent
from backend.budget import (
    BudgetExceeded,
    InferenceMeter,
    estimate_prompt_tokens,
    get_today_budget,
    reconcile_inference_tokens,
    release_inference_reservation,
    reserve_inference_budget,
    reserve_x_budget,
    set_kill_switch,
)
from backend.config import get_settings
from backend.database import BudgetReconciliationDB, BudgetReservationDB, get_db
from backend.models import Evidence, FounderMaterial, Post, ReplyPlan
from backend.ports import XClientPort


USAGE_IN, USAGE_OUT = 120, 30


@pytest.fixture(autouse=True)
def empty_budget_tables():
    """Start each test from an empty ledger, so totals are this test's own."""
    from backend.database import BudgetLedgerDB
    db = get_db()
    try:
        for model in (BudgetLedgerDB, BudgetReservationDB, BudgetReconciliationDB):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()

RESEARCH_JSON = {
    "post_kind": "question",
    "brief_problem_span": None,
    "in_others_thread": False,
    "is_vendor_post": False,
    "matched_topic": "reliability",
    "why_it_fits": "The post asks how to handle eventual consistency.",
    "founder_materials": [],
}
PLAN_JSON = {
    "reply_type": "add_observation",
    "target_point": "How do you folks handle eventual consistency?",
    "mention_product": False,
    "mention_product_reason": "Synthetic reason.",
}
DRAFT_JSON = {"segments": [{"kind": "non_claim", "phrase_id": "q.current_tool"}]}


def _completion(payload, usage=(USAGE_IN, USAGE_OUT)):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))],
        usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]) if usage else None,
    )


def _status_error(status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.invalid/v1/chat/completions")
    cls = openai.RateLimitError if status == 429 else openai.InternalServerError
    return cls("synthetic error", response=httpx.Response(status, request=request), body=None)


class ScriptedCompletions:
    """chat.completions stub: each step is a payload dict or an exception to raise."""

    def __init__(self, steps):
        self._steps = list(steps)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        step = self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, tuple):
            return _completion(*step)
        return _completion(step)


def _agent(steps):
    completions = ScriptedCompletions(steps)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return PlainReplyAgent(openai_client=client), completions


class CrashOnceAfterCall(PlainReplyAgent):
    """The paid call returns, then the attempt fails before completing
    (a worker crash, or a timeout after the call returned)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.crashed = False

    async def _complete(self, *args, **kwargs):
        response = await super()._complete(*args, **kwargs)
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("synthetic: attempt lost after the paid call returned")
        return response


def _env(attempt: int, activity_id: str = "7") -> ActivityEnvironment:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info,
        attempt=attempt,
        activity_id=activity_id,
        workflow_id="review:synthetic",
        workflow_run_id="run-1",
    )
    return env


def _evidence() -> Evidence:
    return Evidence(
        post_id="1001",
        matched_topic="reliability",
        why_it_fits="The post asks how to handle eventual consistency.",
        confidence_score=0.8,
        confidence_inputs={},
        founder_materials=[
            FounderMaterial(
                source="product_brief",
                passage="Our product provides automatic failover.",
                relevance="consistency",
                passage_id="passage_0",
            )
        ],
    )


def _activity_call(name, activities, post):
    if name == "research_post":
        return activities.research_post, ResearchPostInput(
            post=post, topic="reliability", product_brief="Synthetic brief.", idempotency_key="k"
        )
    if name == "plan_reply":
        return activities.plan_reply, PlanReplyInput(post=post, evidence=_evidence(), idempotency_key="k")
    return activities.draft_reply, DraftReplyInput(evidence=_evidence(), idempotency_key="k")


PAYLOADS = {"research_post": RESEARCH_JSON, "plan_reply": PLAN_JSON, "draft_reply": DRAFT_JSON}


def _actual_cost(n_in=USAGE_IN, n_out=USAGE_OUT) -> float:
    s = get_settings()
    return n_in * s.inference_input_token_cost_usd + n_out * s.inference_output_token_cost_usd


def _reservations(budget_type: str):
    db = get_db()
    try:
        return db.query(BudgetReservationDB).filter(BudgetReservationDB.budget_type == budget_type).all()
    finally:
        db.close()


def _reconciliations(budget_type: str):
    db = get_db()
    try:
        return db.query(BudgetReconciliationDB).filter(BudgetReconciliationDB.budget_type == budget_type).all()
    finally:
        db.close()


# ---------- acceptance 1: a retry after a lost completion is counted ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["research_post", "plan_reply", "draft_reply"])
async def test_retry_after_lost_completion_records_both_paid_calls(name, sample_post):
    agent = CrashOnceAfterCall(openai_client=SimpleNamespace(
        chat=SimpleNamespace(completions=ScriptedCompletions([PAYLOADS[name], PAYLOADS[name]]))
    ))
    activities = ReplyActivities(agent)

    fn, arg = _activity_call(name, activities, sample_post)
    with pytest.raises(RuntimeError, match="attempt lost"):
        await _env(attempt=1).run(fn, arg)
    await _env(attempt=2).run(fn, arg)

    budget = get_today_budget()
    assert budget["inference_calls"] == 2
    assert budget["inference_cost_usd"] == pytest.approx(2 * _actual_cost())
    keys = sorted(r.reservation_key for r in _reservations("inference"))
    assert [":a1:c1:" in keys[0], ":a2:c1:" in keys[1]] == [True, True]
    recons = _reconciliations("inference")
    assert sorted(r.attempt for r in recons) == ["1", "2"]


@pytest.mark.asyncio
async def test_timeout_keeps_the_attempt_counted_and_retry_adds_a_call(sample_post):
    """A timeout may have been billed: attempt 1 stays counted at its reservation."""
    request = httpx.Request("POST", "https://api.openai.invalid/v1/chat/completions")
    agent, _ = _agent([openai.APITimeoutError(request=request), PLAN_JSON])
    activities = ReplyActivities(agent)
    fn, arg = _activity_call("plan_reply", activities, sample_post)

    with pytest.raises(openai.APITimeoutError):
        await _env(attempt=1).run(fn, arg)
    await _env(attempt=2).run(fn, arg)

    first = next(r for r in _reservations("inference") if ":a1:" in r.reservation_key)
    assert first.reconciled == 0  # left open: counted at the reserved amount
    budget = get_today_budget()
    assert budget["inference_calls"] == 2
    assert budget["inference_cost_usd"] == pytest.approx(first.cost_usd + _actual_cost())


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500])
async def test_error_status_releases_the_attempt(status, sample_post):
    agent, _ = _agent([_status_error(status), PLAN_JSON])
    activities = ReplyActivities(agent)
    fn, arg = _activity_call("plan_reply", activities, sample_post)

    with pytest.raises(openai.APIStatusError):
        await _env(attempt=1).run(fn, arg)
    budget = get_today_budget()
    assert budget["inference_calls"] == 0
    assert budget["inference_cost_usd"] == pytest.approx(0.0)

    await _env(attempt=2).run(fn, arg)
    budget = get_today_budget()
    assert budget["inference_calls"] == 1
    assert budget["inference_cost_usd"] == pytest.approx(_actual_cost())


@pytest.mark.asyncio
async def test_missing_usage_keeps_the_reservation(sample_post):
    agent, _ = _agent([(PLAN_JSON, None)])
    fn, arg = _activity_call("plan_reply", ReplyActivities(agent), sample_post)
    await _env(attempt=1).run(fn, arg)

    (reservation,) = _reservations("inference")
    assert reservation.reconciled == 0
    assert get_today_budget()["inference_cost_usd"] == pytest.approx(reservation.cost_usd)


@pytest.mark.asyncio
async def test_kill_switch_stops_the_call_before_it_is_sent(sample_post):
    set_kill_switch(True)
    agent, completions = _agent([PLAN_JSON])
    fn, arg = _activity_call("plan_reply", ReplyActivities(agent), sample_post)

    with pytest.raises(BudgetExceeded):
        await _env(attempt=1).run(fn, arg)
    assert completions.calls == []


# ---------- acceptance 2: reservations are sized from the prompt ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("name,setting", [
    ("research_post", "research_max_output_tokens"),
    ("plan_reply", "plan_max_output_tokens"),
    ("draft_reply", "draft_max_output_tokens"),
])
async def test_reservation_is_sized_from_the_prompt_and_max_output(name, setting, sample_post):
    agent, completions = _agent([(PAYLOADS[name], None)])  # no usage: keeps the reservation as sized
    fn, arg = _activity_call(name, ReplyActivities(agent), sample_post)
    await _env(attempt=1).run(fn, arg)

    sent = completions.calls[0]
    max_output = getattr(get_settings(), setting)
    assert sent["max_completion_tokens"] == max_output
    (reservation,) = _reservations("inference")
    assert reservation.reserved_amount == estimate_prompt_tokens(sent["messages"]) + max_output
    s = get_settings()
    assert reservation.cost_usd == pytest.approx(
        estimate_prompt_tokens(sent["messages"]) * s.inference_input_token_cost_usd
        + max_output * s.inference_output_token_cost_usd
    )


def test_prompt_estimate_grows_with_the_prompt_and_overestimates_english():
    short = [{"role": "user", "content": "Hi."}]
    long = [{"role": "user", "content": "word " * 400}]  # about 400 tokens in GPT tokenizers
    assert estimate_prompt_tokens(short) < 20
    assert estimate_prompt_tokens(long) >= 400
    # non-ASCII text counts more per character
    assert estimate_prompt_tokens([{"role": "user", "content": "é" * 30}]) > estimate_prompt_tokens(
        [{"role": "user", "content": "e" * 30}]
    )


def test_small_call_near_the_cap_is_not_refused_by_a_fixed_estimate(monkeypatch):
    settings = get_settings()
    old_fixed_cost = 500 * settings.inference_input_token_cost_usd + 200 * settings.inference_output_token_cost_usd
    small_messages = [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "Short."}]
    small_input, small_output = estimate_prompt_tokens(small_messages), 50
    small_cost = (
        small_input * settings.inference_input_token_cost_usd
        + small_output * settings.inference_output_token_cost_usd
    )
    assert small_cost < old_fixed_cost
    # Leave room for the small call but not for the old fixed research estimate.
    monkeypatch.setattr(settings, "daily_inference_limit_usd", (small_cost + old_fixed_cost) / 2)

    with pytest.raises(BudgetExceeded):
        reserve_inference_budget(500, 200)

    meter = InferenceMeter("review:synthetic", "run-1", "7", attempt=1)
    call = meter.reserve(small_input, small_output)
    assert call.reserved_cost == pytest.approx(small_cost)
    assert get_today_budget()["inference_calls"] == 1


# ---------- budget.py: per-attempt keys and release ----------


def test_inference_reservations_are_per_attempt_and_call():
    ids = dict(workflow_id="wf", workflow_run_id="run", activity_id="3")
    reserve_inference_budget(100, 50, **ids, attempt=1, call_index=1)
    reserve_inference_budget(100, 50, **ids, attempt=1, call_index=1)  # same key: reused
    reserve_inference_budget(100, 50, **ids, attempt=1, call_index=2)
    reserve_inference_budget(100, 50, **ids, attempt=2, call_index=1)
    assert get_today_budget()["inference_calls"] == 3
    assert len(_reservations("inference")) == 3


def test_release_refunds_the_call_and_is_idempotent():
    ids = dict(workflow_id="wf", workflow_run_id="run", activity_id="3", attempt=2, call_index=1)
    cost, day = reserve_inference_budget(100, 50, **ids)
    assert get_today_budget()["inference_cost_usd"] == pytest.approx(cost)

    release_inference_reservation(100, 50, day, **ids)
    release_inference_reservation(100, 50, day, **ids)
    reconcile_inference_tokens(100, 50, 100, 50, day, **ids)  # same key: no-op after release

    budget = get_today_budget()
    assert budget["inference_calls"] == 0
    assert budget["inference_cost_usd"] == pytest.approx(0.0)
    (reservation,) = _reservations("inference")
    assert reservation.reconciled == 1


@pytest.mark.asyncio
async def test_reserve_x_budget_defaults_to_the_activity_attempt():
    """Callers that pass activity.info() ids get per-attempt keys without passing attempt."""

    async def reserve_in_activity():
        from temporalio import activity
        info = activity.info()
        return reserve_x_budget(1, None, info.workflow_id, info.workflow_run_id, info.activity_id)

    await _env(attempt=3).run(reserve_in_activity)
    (reservation,) = _reservations("x_reads")
    assert ":a3:" in reservation.reservation_key


# ---------- X reads ----------


def _post(i: int) -> Post:
    from backend.models import Author
    return Post(
        id=f"90{i:02d}",
        text=f"Synthetic post number {i}.",
        author=Author(id="a1", username="synthetic_user", name="Synthetic", followers_count=10, verified=False),
        created_at="2026-11-24T00:00:00+00:00",
        like_count=0, reply_count=0, repost_count=0, quote_count=0,
        conversation_id=f"90{i:02d}",
        reply_settings="everyone",
    )


class ScriptedXClient(XClientPort):
    def __init__(self, steps):
        self._steps = list(steps)

    async def _next(self):
        step = self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    async def search_recent(self, query, since_id=None, max_results=10):
        return await self._next()

    async def lookup_post(self, post_id):
        return await self._next()


def _x_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.x.invalid/2/tweets/search/recent")
    return httpx.HTTPStatusError("synthetic", request=request, response=httpx.Response(status, request=request))


@pytest.mark.asyncio
async def test_search_retry_after_lost_completion_records_both_reads(monkeypatch):
    """Attempt 1's search returns and is reconciled, then the attempt is lost."""
    import backend.activities.x_activities as xa

    real_reconcile = xa.reconcile_x_reads
    crashed = []

    def reconcile_then_crash(*args, **kwargs):
        cost = real_reconcile(*args, **kwargs)
        if not crashed:
            crashed.append(True)
            raise RuntimeError("synthetic: attempt lost after the paid call returned")
        return cost

    monkeypatch.setattr(xa, "reconcile_x_reads", reconcile_then_crash)
    posts = [_post(1), _post(2), _post(3)]
    activities = XActivities(ScriptedXClient([(posts, "9003"), (posts, "9003")]))
    arg = SearchRecentInput(query="synthetic", max_results=10, idempotency_key="k")

    with pytest.raises(RuntimeError, match="attempt lost"):
        await _env(attempt=1).run(activities.search_recent, arg)
    await _env(attempt=2).run(activities.search_recent, arg)

    budget = get_today_budget()
    assert budget["x_reads"] == 6
    assert budget["x_cost_usd"] == pytest.approx(6 * get_settings().x_post_read_cost)
    assert sorted(r.attempt for r in _reconciliations("x_reads")) == ["1", "2"]


@pytest.mark.asyncio
async def test_search_timeout_keeps_reads_counted():
    request = httpx.Request("GET", "https://api.x.invalid/2/tweets/search/recent")
    activities = XActivities(ScriptedXClient([httpx.ReadTimeout("synthetic", request=request), ([_post(1)], "9001")]))
    arg = SearchRecentInput(query="synthetic", max_results=10, idempotency_key="k")

    with pytest.raises(httpx.ReadTimeout):
        await _env(attempt=1).run(activities.search_recent, arg)
    await _env(attempt=2).run(activities.search_recent, arg)

    assert get_today_budget()["x_reads"] == 10 + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    _x_status_error(503),
    ApplicationError("rate limited", type="RateLimitError"),
])
async def test_search_error_status_releases_reads(error):
    activities = XActivities(ScriptedXClient([error]))
    arg = SearchRecentInput(query="synthetic", max_results=10, idempotency_key="k")

    with pytest.raises(type(error)):
        await _env(attempt=1).run(activities.search_recent, arg)

    budget = get_today_budget()
    assert budget["x_reads"] == 0
    assert budget["x_cost_usd"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_lookup_error_status_releases_the_read_and_retry_counts_one():
    activities = XActivities(ScriptedXClient([_x_status_error(503), _post(1)]))
    arg = LookupPostInput(post_id="9001", idempotency_key="k")

    with pytest.raises(httpx.HTTPStatusError):
        await _env(attempt=1).run(activities.lookup_post, arg)
    assert get_today_budget()["x_reads"] == 0

    await _env(attempt=2).run(activities.lookup_post, arg)
    assert get_today_budget()["x_reads"] == 1
