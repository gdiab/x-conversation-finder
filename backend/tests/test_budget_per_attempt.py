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
    prompt_token_bound,
    get_today_budget,
    ReservationClosed,
    meter_inference,
    request_never_sent,
    reconcile_inference_tokens,
    reserve_inference_budget,
    reserve_x_budget,
    set_kill_switch,
)
from backend.config import get_settings
from backend.database import BudgetReconciliationDB, BudgetReservationDB, get_db
from backend.models import Evidence, FounderMaterial, Post, ReplyPlan
from backend.ports import XClientPort


USAGE_IN, USAGE_OUT = 120, 30


def _empty():
    from backend.database import BudgetLedgerDB
    db = get_db()
    try:
        for model in (BudgetLedgerDB, BudgetReservationDB, BudgetReconciliationDB):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def empty_budget_tables():
    """Start each test from an empty ledger, so totals are this test's own."""
    _empty()

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


def _completion(payload, usage=(USAGE_IN, USAGE_OUT), finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload)), finish_reason=finish_reason
        )],
        usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]) if usage else None,
    )


def _status_error(status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.invalid/v1/chat/completions")
    return openai.APIStatusError("synthetic error", response=httpx.Response(status, request=request), body=None)


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
@pytest.mark.parametrize("status", [400, 429, 408, 500, 503])
async def test_error_status_keeps_the_attempt_counted_and_retry_adds_a_call(status, sample_post):
    """OPENAI_BASE_URL may be a gateway, so no status proves the request was
    free: the failed attempt stays counted, and the retry is counted too."""
    agent, _ = _agent([_status_error(status), PLAN_JSON])
    fn, arg = _activity_call("plan_reply", ReplyActivities(agent), sample_post)

    with pytest.raises(openai.APIStatusError):
        await _env(attempt=1).run(fn, arg)
    (reservation,) = _reservations("inference")
    assert reservation.reconciled == 0
    assert get_today_budget()["inference_cost_usd"] == pytest.approx(reservation.cost_usd)

    await _env(attempt=2).run(fn, arg)
    budget = get_today_budget()
    assert budget["inference_calls"] == 2
    assert budget["inference_cost_usd"] == pytest.approx(reservation.cost_usd + _actual_cost())


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [None, (USAGE_IN, None), (None, USAGE_OUT), (0, 0)])
async def test_missing_or_partial_usage_keeps_the_reservation(usage, sample_post):
    payload = (PLAN_JSON, None) if usage is None else (PLAN_JSON, usage)
    agent, _ = _agent([payload])
    fn, arg = _activity_call("plan_reply", ReplyActivities(agent), sample_post)
    await _env(attempt=1).run(fn, arg)

    (reservation,) = _reservations("inference")
    assert reservation.reconciled == 0
    assert get_today_budget()["inference_cost_usd"] == pytest.approx(reservation.cost_usd)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["research_post", "plan_reply", "draft_reply"])
async def test_truncated_output_fails_without_retry_and_stays_counted(name, sample_post):
    agent, _ = _agent([(PAYLOADS[name], (USAGE_IN, 400), "length")])
    fn, arg = _activity_call(name, ReplyActivities(agent), sample_post)

    with pytest.raises(ApplicationError) as info:
        await _env(attempt=1).run(fn, arg)
    assert info.value.type == "InferenceOutputTruncated"
    assert info.value.non_retryable
    assert get_today_budget()["inference_cost_usd"] == pytest.approx(_actual_cost(USAGE_IN, 400))


@pytest.mark.asyncio
async def test_real_client_refuses_a_call_outside_a_metered_attempt(sample_post, monkeypatch):
    """The adapter that builds its own (paid) client fails closed without a meter."""
    agent = PlainReplyAgent()  # builds a real AsyncOpenAI client; no request is sent
    sent = []

    async def must_not_send(**kwargs):
        sent.append(kwargs)
        raise AssertionError("request sent")

    monkeypatch.setattr(agent.client.chat.completions, "create", must_not_send)
    with pytest.raises(ApplicationError) as info:
        await agent.plan(sample_post, _evidence(), "k")
    assert info.value.type == "UnmeteredInferenceCall"
    assert sent == []
    assert agent.client.max_retries == 0


def _openai_connection_error(cause: BaseException) -> openai.APIConnectionError:
    """Shaped as openai's client raises it: `raise APIConnectionError(...) from err`."""
    request = httpx.Request("POST", "https://api.openai.invalid/v1/chat/completions")
    cls = openai.APITimeoutError if isinstance(cause, httpx.TimeoutException) else openai.APIConnectionError
    err = cls(request=request)
    err.__cause__ = cause
    return err


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", [
    httpx.ConnectError("synthetic: connection refused"),
    httpx.ConnectTimeout("synthetic: connect timeout"),
])
async def test_connection_never_established_releases_the_attempt(cause, sample_post):
    agent, _ = _agent([_openai_connection_error(cause), PLAN_JSON])
    fn, arg = _activity_call("plan_reply", ReplyActivities(agent), sample_post)

    with pytest.raises(openai.APIConnectionError):
        await _env(attempt=1).run(fn, arg)
    budget = get_today_budget()
    assert budget["inference_calls"] == 0
    assert budget["inference_cost_usd"] == pytest.approx(0.0)

    await _env(attempt=2).run(fn, arg)
    budget = get_today_budget()
    assert budget["inference_calls"] == 1
    assert budget["inference_cost_usd"] == pytest.approx(_actual_cost())


@pytest.mark.asyncio
async def test_read_timeout_after_sending_keeps_the_attempt_counted(sample_post):
    agent, _ = _agent([_openai_connection_error(httpx.ReadTimeout("synthetic: no response"))])
    fn, arg = _activity_call("plan_reply", ReplyActivities(agent), sample_post)

    with pytest.raises(openai.APITimeoutError):
        await _env(attempt=1).run(fn, arg)
    (reservation,) = _reservations("inference")
    assert reservation.reconciled == 0
    assert get_today_budget()["inference_calls"] == 1


def test_request_never_sent_only_for_connection_failures():
    request = httpx.Request("GET", "https://api.x.invalid/2/tweets/search/recent")
    assert request_never_sent(httpx.ConnectError("x", request=request))
    assert request_never_sent(httpx.ConnectTimeout("x", request=request))
    assert request_never_sent(_openai_connection_error(httpx.ConnectError("x")))
    assert not request_never_sent(httpx.ReadTimeout("x", request=request))
    assert not request_never_sent(httpx.RemoteProtocolError("x", request=request))
    assert not request_never_sent(_openai_connection_error(httpx.ReadTimeout("x")))
    assert not request_never_sent(_status_error(503))
    assert not request_never_sent(RuntimeError("x"))


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
    assert reservation.reserved_amount == prompt_token_bound(sent["messages"]) + max_output
    s = get_settings()
    assert reservation.cost_usd == pytest.approx(
        prompt_token_bound(sent["messages"]) * s.inference_input_token_cost_usd
        + max_output * s.inference_output_token_cost_usd
    )


@pytest.mark.parametrize("content", [
    "word " * 400,
    "{}[]();:!?<>=+-*/&|^%$#@~`" * 20,   # punctuation: about one token per byte
    "x9Qz_7Lk" * 50,                      # random identifiers
    "你好世界" * 50,                       # CJK: 3 bytes per character
    "🙂👍🏽🧑‍💻" * 30,                    # emoji and ZWJ sequences
])
def test_prompt_bound_is_at_least_one_token_per_byte(content):
    messages = [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": content}]
    content_bytes = sum(len(m["content"].encode("utf-8")) for m in messages)
    # A byte-level BPE token covers at least one byte, so bytes bound the tokens.
    assert prompt_token_bound(messages) >= content_bytes + 2 * 4


@pytest.mark.asyncio
async def test_admission_near_the_cap_tracks_the_prompt_not_a_fixed_estimate(sample_post, monkeypatch):
    """Through draft_reply: the call is admitted exactly when the remaining budget
    covers its own prompt bound + max output, so a small call is not refused by a
    larger fixed estimate, and a larger prompt needs more room."""
    settings = get_settings()
    monkeypatch.setattr(settings, "draft_max_output_tokens", 60)

    async def reservation_cost(steer):
        agent, completions = _agent([(DRAFT_JSON, None)])
        await _env(attempt=1, activity_id=f"probe-{len(steer)}").run(
            ReplyActivities(agent).draft_reply,
            DraftReplyInput(evidence=_evidence(), idempotency_key="k", steer=steer),
        )
        return _reservations("inference")[-1].cost_usd

    small, large = await reservation_cost(""), await reservation_cost("Be brief. " * 200)
    assert small < large

    for limit, admitted in [(small * 1.0001, True), (small * 0.9999, False)]:
        _empty()
        monkeypatch.setattr(settings, "daily_inference_limit_usd", limit)
        agent, completions = _agent([(DRAFT_JSON, None)])
        call = ReplyActivities(agent).draft_reply, DraftReplyInput(evidence=_evidence(), idempotency_key="k")
        if admitted:
            await _env(attempt=1).run(*call)
            assert len(completions.calls) == 1
        else:
            with pytest.raises(BudgetExceeded):
                await _env(attempt=1).run(*call)
            assert completions.calls == []


def test_small_call_near_the_cap_is_not_refused_by_a_fixed_estimate(monkeypatch):
    """Issue #8 acceptance, at the meter: a call whose own bound is below the old
    fixed research estimate (500 in / 200 out) is admitted where that estimate is
    refused. No application activity can show this end to end: a true upper
    bound on its prompt (UTF-8 bytes) plus its configured max output exceeds
    the old fixed estimates, so the activity-level test above shows instead
    that admission tracks each call's own size."""
    settings = get_settings()
    old_fixed_cost = 500 * settings.inference_input_token_cost_usd + 200 * settings.inference_output_token_cost_usd
    small_messages = [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "Short."}]
    small_input, small_output = prompt_token_bound(small_messages), 50
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


def test_closed_reservation_refuses_to_authorize_another_request():
    ids = dict(workflow_id="wf", workflow_run_id="run", activity_id="3", attempt=2, call_index=1)
    cost, day = reserve_inference_budget(100, 50, **ids)
    reconcile_inference_tokens(100, 50, 80, 40, day, **ids)
    reconcile_inference_tokens(100, 50, 80, 40, day, **ids)  # idempotent

    with pytest.raises(ReservationClosed):
        reserve_inference_budget(100, 50, **ids)
    budget = get_today_budget()
    assert budget["inference_calls"] == 1
    assert budget["inference_cost_usd"] == pytest.approx(_actual_cost(80, 40))


@pytest.mark.asyncio
async def test_meters_do_not_nest():
    async def nested():
        with meter_inference():
            with meter_inference():
                pass

    with pytest.raises(RuntimeError, match="does not nest"):
        await _env(attempt=1).run(nested)


@pytest.mark.asyncio
async def test_injected_openai_client_is_metered_and_has_no_sdk_retries(sample_post):
    client = openai.AsyncOpenAI(api_key="synthetic", base_url="https://api.openai.invalid/v1", max_retries=5)
    agent = PlainReplyAgent(openai_client=client)
    assert agent.client.max_retries == 0

    async def must_not_send(**kwargs):
        raise AssertionError("request sent")

    agent.client.chat.completions.create = must_not_send
    with pytest.raises(ApplicationError) as info:
        await agent.plan(sample_post, _evidence(), "k")  # outside a metered attempt
    assert info.value.type == "UnmeteredInferenceCall"


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
@pytest.mark.parametrize("status", [408, 500, 503])
async def test_search_ambiguous_error_keeps_reads_counted(status):
    activities = XActivities(ScriptedXClient([_x_status_error(status)]))
    arg = SearchRecentInput(query="synthetic", max_results=10, idempotency_key="k")

    with pytest.raises(httpx.HTTPStatusError):
        await _env(attempt=1).run(activities.search_recent, arg)
    assert get_today_budget()["x_reads"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    _x_status_error(400),
    _x_status_error(401),
    ApplicationError("rate limited", type="RateLimitError"),
])
async def test_search_rejected_request_releases_reads(error):
    activities = XActivities(ScriptedXClient([error]))
    arg = SearchRecentInput(query="synthetic", max_results=10, idempotency_key="k")

    with pytest.raises(type(error)):
        await _env(attempt=1).run(activities.search_recent, arg)

    budget = get_today_budget()
    assert budget["x_reads"] == 0
    assert budget["x_cost_usd"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_lookup_rejected_request_releases_the_read_and_retry_counts_one():
    activities = XActivities(ScriptedXClient([_x_status_error(403), _post(1)]))
    arg = LookupPostInput(post_id="9001", idempotency_key="k")

    with pytest.raises(httpx.HTTPStatusError):
        await _env(attempt=1).run(activities.lookup_post, arg)
    assert get_today_budget()["x_reads"] == 0

    await _env(attempt=2).run(activities.lookup_post, arg)
    assert get_today_budget()["x_reads"] == 1


@pytest.mark.asyncio
async def test_lookup_returning_none_stays_counted(monkeypatch):
    """The real HTTP adapter turns a 404 naming the post as not found into
    None (issue #9), and None keeps 1 read. Post data without its author, which
    X billed, now raises MalformedLookupResponse and also stays counted."""
    from backend.adapters.http_x_client import HttpXClient

    def handler(request):
        return httpx.Response(404, json={"errors": [{
            "resource_id": "9001", "title": "Not Found Error",
            "type": "https://api.twitter.com/2/problems/resource-not-found",
        }]}, request=request)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: real_async_client(*a, transport=httpx.MockTransport(handler), **kw),
    )
    activities = XActivities(HttpXClient(bearer_token="synthetic-token"))
    result = await _env(attempt=1).run(
        activities.lookup_post, LookupPostInput(post_id="9001", idempotency_key="k")
    )

    assert result is None
    budget = get_today_budget()
    assert budget["x_reads"] == 1
    assert budget["x_cost_usd"] == pytest.approx(get_settings().x_post_read_cost)
    (recon,) = _reconciliations("x_reads")
    assert recon.actual_amount == 1


@pytest.mark.asyncio
async def test_http_search_reserves_what_the_request_can_be_billed_for(monkeypatch):
    """max_results=2 is sent to X as 10 (X's minimum), so 10 reads are reserved."""
    from backend.adapters.http_x_client import HttpXClient

    sent = []

    def handler(request):
        sent.append(int(request.url.params["max_results"]))
        return httpx.Response(429, headers={"Retry-After": "1"}, request=request)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: real_async_client(*a, transport=httpx.MockTransport(handler), **kw),
    )
    reserved = []
    import backend.activities.x_activities as xa
    real_reserve = xa.reserve_x_budget

    def spy(*args, **kwargs):
        reserved.append(kwargs["reads_needed"])
        return real_reserve(*args, **kwargs)

    monkeypatch.setattr(xa, "reserve_x_budget", spy)
    activities = XActivities(HttpXClient(bearer_token="synthetic-token"))
    with pytest.raises(ApplicationError):
        await _env(attempt=1).run(
            activities.search_recent, SearchRecentInput(query="synthetic", max_results=2, idempotency_key="k")
        )
    assert sent == [10]
    assert reserved == [10]


@pytest.mark.asyncio
async def test_search_connection_never_established_releases_reads():
    request = httpx.Request("GET", "https://api.x.invalid/2/tweets/search/recent")
    activities = XActivities(ScriptedXClient([httpx.ConnectError("synthetic", request=request)]))
    arg = SearchRecentInput(query="synthetic", max_results=10, idempotency_key="k")

    with pytest.raises(httpx.ConnectError):
        await _env(attempt=1).run(activities.search_recent, arg)
    assert get_today_budget()["x_reads"] == 0
