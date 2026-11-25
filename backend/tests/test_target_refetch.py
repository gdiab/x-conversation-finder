"""
Target post re-fetch and the removal duty (issue #9, ADR-0006 / Outpost ADR-0086).

- approve re-fetches the target post through the budgeted lookup_post activity;
- a 404 removes the stored post text and every model-written copy of it
  (why_it_fits, the problem span, the plan's target point), records the
  removal on the card and ends the Review as skipped ("post removed");
- a 200 leaves the card unchanged and approves;
- a reply-URL 404 still keeps the target post text (the #5 fix);
- Reviews approved before the re-fetch existed replay unchanged;
- the eval hook re-fetches a stored card before it is replayed.

Fake adapters and synthetic posts only; no X or OpenAI calls.
"""

import asyncio
import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory, WorkflowUpdateFailedError, WorkflowUpdateStage
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from backend.activities.claim_gate_activity import claim_gate, verify_edited_text
from backend.activities.reply_activities import ReplyActivities
from backend.activities.store_activities import (
    POST_REMOVED_SKIP_REASON,
    POST_TEXT_REMOVED,
    delete_post_text,
    get_all_cards,
    store_card,
    update_card,
)
from backend.activities.x_activities import XActivities
from backend.adapters import FakeReplyAgent, FakeXClient
from backend.budget import get_today_budget, set_kill_switch
from backend.workflows.review import TARGET_REFETCH_PATCH_ID, ReviewWorkflow

FIXTURES = Path(__file__).parent / "fixtures"
TARGET_ID = "1001"  # conftest sample_post
QUESTION = "How do you folks handle eventual consistency?"  # from the sample post
# Fragments of the sample post that must not survive a removal
POST_FRAGMENTS = ("Struggling with distributed", "eventual consistency")
APPROVE_URL = "https://x.com/test/status/999"


class QuotingAgent(FakeReplyAgent):
    """Fake research whose why_it_fits quotes the post, as a model's may.
    Records the evidence each draft() call received."""

    def __init__(self):
        super().__init__()
        self.drafted = []

    async def draft(self, evidence, steer=None, idempotency_key=None, plan=None):
        self.drafted.append(idempotency_key)
        return await super().draft(evidence, steer, idempotency_key, plan)

    async def research(self, post, topic, product_brief, idempotency_key):
        evidence = await super().research(post, topic, product_brief, idempotency_key)
        evidence.why_it_fits = f"The post asks '{QUESTION}'"
        return evidence


class TargetClient(FakeXClient):
    """Fake X client with a scripted outcome for lookups of the target post.

    outcomes: per target lookup, "found", "gone" (404), "error" (transient
    failure), "block" (wait for `release`, then found) or "block_gone" (wait,
    then 404). Past the end of the list, the last outcome repeats.
    """

    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = list(outcomes)
        self.target_lookups = 0
        self.release = asyncio.Event()
        self.blocked = asyncio.Event()

    async def lookup_post(self, post_id):
        if post_id != TARGET_ID:
            return await super().lookup_post(post_id)
        index = min(self.target_lookups, len(self.outcomes) - 1)
        self.target_lookups += 1
        outcome = self.outcomes[index]
        if outcome.startswith("block"):
            self.blocked.set()
            await self.release.wait()
            outcome = "gone" if outcome == "block_gone" else "found"
        if outcome == "gone":
            return None
        if outcome == "error":
            raise RuntimeError("synthetic transient X error")
        return await super().lookup_post(post_id)


def _worker(env, x_client, agent=None):
    x_acts = XActivities(x_client)
    reply_acts = ReplyActivities(agent or QuotingAgent())
    return Worker(
        env.client,
        task_queue="test",
        workflows=[ReviewWorkflow],
        activities=[
            x_acts.lookup_post,
            x_acts.check_followup,
            reply_acts.research_post,
            reply_acts.plan_reply,
            reply_acts.draft_reply,
            claim_gate,
            verify_edited_text,
            store_card,
            update_card,
            delete_post_text,
        ],
    )


def _x_reads():
    # The ledger can carry reads from earlier tests (cached settings), so
    # tests compare before and after.
    return get_today_budget()["x_reads"]


def _db_card(workflow_id):
    return next(c for c in get_all_cards() if c["workflow_id"] == workflow_id)


def _scheduled(history):
    return [
        e.activity_task_scheduled_event_attributes
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]


def _scheduled_names(history):
    return [a.activity_type.name for a in _scheduled(history)]


async def _start(env, make_review_input, suffix):
    review_input = make_review_input()
    handle = await env.client.start_workflow(
        ReviewWorkflow.run,
        review_input,
        id=f"review:test:{review_input.post.id}_{suffix}",
        task_queue="test",
    )
    await env.sleep(1)
    card = await handle.query("get_card")
    assert card["status"] == "pending", card
    return review_input, handle


# ---------- acceptance: 404 vs 200 on the re-fetch ----------


@pytest.mark.asyncio
async def test_404_on_refetch_redacts_text_and_spans_and_skips(make_review_input):
    client = TargetClient(["gone"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            review_input, handle = await _start(env, make_review_input, "refetch_404")
            before = await handle.query("get_card")
            # The card quotes the post before the removal (so the test means something)
            assert QUESTION in before["evidence"]["why_it_fits"]
            assert before["evidence"]["reply_plan"]["target_point"] == QUESTION
            assert before["evidence"]["reply_fit"]["brief_problem_span"]

            with pytest.raises(WorkflowUpdateFailedError) as excinfo:
                await handle.execute_update("approve", args=[APPROVE_URL, str(uuid.uuid4()), False])
            assert excinfo.value.cause.type == "PostRemoved"
            assert await handle.result() == "skipped"

            for card in (await handle.query("get_card"), _db_card(handle.id)):
                assert card["status"] == "skipped"
                assert card["skip_reason"] == POST_REMOVED_SKIP_REASON
                assert card["post"]["text"] == POST_TEXT_REMOVED
                assert card["post"]["text_removed"] is True
                assert card["evidence"]["why_it_fits"] == POST_TEXT_REMOVED
                assert card["evidence"]["reply_fit"]["brief_problem_span"] == POST_TEXT_REMOVED
                assert card["evidence"]["reply_plan"]["target_point"] == POST_TEXT_REMOVED
                dumped = json.dumps(card)
                for fragment in POST_FRAGMENTS:
                    assert fragment not in dumped
            assert client.target_lookups == 1

            history = await handle.fetch_history()
            names = _scheduled_names(history)
            assert names.count("lookup_post") == 1
            assert "delete_post_text" in names
            lookup = next(a for a in _scheduled(history) if a.activity_type.name == "lookup_post")
            assert TARGET_ID in str(lookup.input)
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_200_on_refetch_leaves_card_unchanged_and_approves(make_review_input):
    client = TargetClient(["found"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            review_input, handle = await _start(env, make_review_input, "refetch_200")
            before = await handle.query("get_card")
            reads_before = _x_reads()

            assert await handle.execute_update(
                "approve", args=[APPROVE_URL, str(uuid.uuid4()), False]
            ) == "Approved"
            await env.sleep(1)

            card = await handle.query("get_card")
            assert card["status"] == "approved"
            assert card["post"] == before["post"]
            assert "text_removed" not in card["post"]
            assert card["evidence"]["why_it_fits"] == before["evidence"]["why_it_fits"]
            assert card["evidence"]["reply_plan"] == before["evidence"]["reply_plan"]
            stored = _db_card(handle.id)
            assert stored["status"] == "approved"
            assert stored["post"]["text"] == review_input.post.text
            assert stored["evidence"]["reply_plan"]["target_point"] == QUESTION

            # One budgeted X read for the re-fetch (research and plan use no X reads)
            assert client.target_lookups == 1
            assert _x_reads() - reads_before == 1
            history = await handle.fetch_history()
            assert "delete_post_text" not in _scheduled_names(history)
            markers = [e for e in history.events if e.HasField("marker_recorded_event_attributes")]
            assert any(TARGET_REFETCH_PATCH_ID.encode() in m.SerializeToString() for m in markers)
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)


# ---------- budget and retries ----------


@pytest.mark.asyncio
async def test_refetch_retry_reuses_the_budget_reservation(make_review_input):
    """A lookup attempt that fails is retried by Temporal with the same
    activity id, so reserve_x_budget reuses the reservation: one read on the
    ledger. (Issue #8 may change how a failed attempt that reached X is
    counted; update this expectation with it.)"""
    client = TargetClient(["error", "found"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            _, handle = await _start(env, make_review_input, "refetch_retry")
            reads_before = _x_reads()
            assert await handle.execute_update(
                "approve", args=[APPROVE_URL, str(uuid.uuid4()), False]
            ) == "Approved"
            assert client.target_lookups == 2
            assert _x_reads() - reads_before == 1
            history = await handle.fetch_history()
            assert _scheduled_names(history).count("lookup_post") == 1


@pytest.mark.asyncio
async def test_failed_refetch_fails_approve_and_same_request_id_can_retry(make_review_input):
    """Retries exhausted: approve fails, the card stays pending with its text,
    and a retry with the same request id re-fetches instead of reporting
    "Approved" through the dedupe path."""
    client = TargetClient(["error", "error", "error", "found"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            review_input, handle = await _start(env, make_review_input, "refetch_fail")
            request_id = str(uuid.uuid4())
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("approve", args=[APPROVE_URL, request_id, False])
            card = await handle.query("get_card")
            assert card["status"] == "pending"
            assert card["post"]["text"] == review_input.post.text
            assert _db_card(handle.id)["status"] == "pending"

            assert await handle.execute_update("approve", args=[APPROVE_URL, request_id, False]) == "Approved"
            assert client.target_lookups == 4
            await env.sleep(1)
            assert (await handle.query("get_card"))["status"] == "approved"


@pytest.mark.asyncio
async def test_over_budget_refetch_fails_approve_without_removing_text(make_review_input):
    """Over budget, lookup_post raises BudgetExceeded and never reports a 404,
    so nothing is removed and no X call is made."""
    client = TargetClient(["gone"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            review_input, handle = await _start(env, make_review_input, "refetch_budget")
            set_kill_switch(True)
            with pytest.raises(WorkflowUpdateFailedError) as excinfo:
                await handle.execute_update("approve", args=[APPROVE_URL, str(uuid.uuid4()), False])
            assert "kill switch" in str(excinfo.value.cause.cause)
            assert client.target_lookups == 0
            for card in (await handle.query("get_card"), _db_card(handle.id)):
                assert card["status"] == "pending"
                assert card["post"]["text"] == review_input.post.text
                assert not card["post"].get("text_removed")
            set_kill_switch(False)


# ---------- concurrency during the re-fetch ----------


@pytest.mark.asyncio
async def test_edit_nudge_and_second_approve_rejected_while_refetch_in_flight(make_review_input):
    client = TargetClient(["block"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            _, handle = await _start(env, make_review_input, "refetch_overlap")
            approve = await handle.start_update(
                "approve",
                args=[APPROVE_URL, str(uuid.uuid4()), False],
                wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            )
            for name, args in (
                ("edit", ["Thanks for sharing! What stack are you on?", str(uuid.uuid4())]),
                ("nudge", ["Make it shorter", str(uuid.uuid4())]),
                ("approve", [APPROVE_URL, str(uuid.uuid4()), False]),
            ):
                with pytest.raises(WorkflowUpdateFailedError) as excinfo:
                    await handle.execute_update(name, args=args)
                assert "approval is in progress" in str(excinfo.value.cause), name

            client.release.set()
            assert await approve.result() == "Approved"
            await env.sleep(1)
            assert (await handle.query("get_card"))["status"] == "approved"


@pytest.mark.asyncio
async def test_skip_during_refetch_waits_for_the_removal(make_review_input):
    """A founder skip while the re-fetch is running must not let the
    workflow close before a 404's removal is done."""
    client = TargetClient(["block_gone"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            _, handle = await _start(env, make_review_input, "refetch_skip_race")
            approve = await handle.start_update(
                "approve",
                args=[APPROVE_URL, str(uuid.uuid4()), False],
                wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            )
            await client.blocked.wait()
            await handle.signal("skip", args=["Not relevant", str(uuid.uuid4())])
            await env.sleep(1)
            client.release.set()

            with pytest.raises(WorkflowUpdateFailedError) as excinfo:
                await approve.result()
            assert excinfo.value.cause.type == "PostRemoved"
            assert await handle.result() == "skipped"
            for card in (await handle.query("get_card"), _db_card(handle.id)):
                assert card["status"] == "skipped"
                assert card["skip_reason"] == POST_REMOVED_SKIP_REASON
                assert card["post"]["text_removed"] is True
                for fragment in POST_FRAGMENTS:
                    assert fragment not in json.dumps(card)
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(await handle.fetch_history())


# The expiry race (the 48h timer firing during approve's re-fetch) takes the
# same wait as the skip race above. It is not tested directly: the
# time-skipping test server cannot advance time while an activity runs.


@pytest.mark.asyncio
async def test_removal_reaches_every_stored_card_of_the_post(make_review_input):
    """Two accounts review the same post. A 404 at one account's approve
    removes the text from both stored cards; the other Review's query (its
    workflow state still holds the text) is redacted on read, a nudge there
    sends nothing to the model, and a Review that stores its card only after
    the removal stores it redacted."""
    from backend.activities.store_activities import apply_stored_removal

    client = TargetClient(["gone"])
    agent = QuotingAgent()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client, agent):
            review_input, first = await _start(env, make_review_input, "refetch_multi_a")
            other_input = make_review_input(account="other")
            other = await env.client.start_workflow(
                ReviewWorkflow.run, other_input,
                id=f"review:other:{other_input.post.id}_refetch_multi_b", task_queue="test",
            )
            await env.sleep(1)
            assert _db_card(other.id)["post"]["text"] == review_input.post.text

            with pytest.raises(WorkflowUpdateFailedError):
                await first.execute_update("approve", args=[APPROVE_URL, str(uuid.uuid4()), False])

            stored_other = _db_card(other.id)
            assert stored_other["post"]["text_removed"] is True
            assert stored_other["status"] == "pending"  # it ends at its own approve
            for fragment in POST_FRAGMENTS:
                assert fragment not in json.dumps(stored_other)

            queried = await other.query("get_card")
            assert queried["post"]["text"] == review_input.post.text  # workflow state
            redacted = apply_stored_removal(other.id, queried)
            assert redacted["post"]["text_removed"] is True
            assert redacted["status"] == "pending"
            for fragment in POST_FRAGMENTS:
                assert fragment not in json.dumps(redacted)

            # A nudge on the other Review must not send its stale evidence on
            drafts_before = len(agent.drafted)
            with pytest.raises(WorkflowUpdateFailedError):
                await other.execute_update("nudge", args=["Make it shorter", str(uuid.uuid4())])
            assert len(agent.drafted) == drafts_before

            # A Review whose card is stored after the removal stores it redacted
            late_input = make_review_input(account="late")
            late = await env.client.start_workflow(
                ReviewWorkflow.run, late_input,
                id=f"review:late:{late_input.post.id}_refetch_multi_c", task_queue="test",
            )
            await env.sleep(1)
            stored_late = _db_card(late.id)
            assert stored_late["post"]["text_removed"] is True
            for fragment in POST_FRAGMENTS:
                assert fragment not in json.dumps(stored_late)


# ---------- the #5 fix still holds ----------


@pytest.mark.asyncio
async def test_reply_url_404_keeps_target_text_after_refetch(make_review_input):
    """Approve's re-fetch finds the target (200); a later 404 on the founder's
    reply URL fails confirm_posted and removes nothing."""

    class GoneReply(TargetClient):
        async def lookup_post(self, post_id):
            if post_id == "404404":
                return None
            return await super().lookup_post(post_id)

    client = GoneReply(["found"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            review_input, handle = await _start(env, make_review_input, "refetch_reply_404")
            await handle.execute_update("approve", args=[APPROVE_URL, str(uuid.uuid4()), False])
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update(
                    "confirm_posted", args=["https://x.com/test/status/404404", str(uuid.uuid4())]
                )
            for card in (await handle.query("get_card"), _db_card(handle.id)):
                assert card["status"] == "approved"
                assert card["post"]["text"] == review_input.post.text
                assert not card["post"].get("text_removed")
                assert card["evidence"]["reply_plan"]["target_point"] == QUESTION
            assert "delete_post_text" not in _scheduled_names(await handle.fetch_history())


# ---------- replay safety ----------

# A Review approved on main 9f4df4a (before the re-fetch), waiting for the
# reply URL. Synthetic data; worker and client identities scrubbed.
APPROVED_HISTORY = ("review_history_main_9f4df4a_approved.json", "review:test:5090_main_9f4df4a_approved")


def _approved_history() -> WorkflowHistory:
    fixture, workflow_id = APPROVED_HISTORY
    return WorkflowHistory.from_json(workflow_id, (FIXTURES / fixture).read_text())


def test_approved_fixture_has_an_approve_without_a_refetch():
    events = json.loads((FIXTURES / APPROVED_HISTORY[0]).read_text())["events"]
    updates = [
        e["workflowExecutionUpdateAcceptedEventAttributes"]["acceptedRequest"]["input"]["name"]
        for e in events
        if e["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED"
    ]
    scheduled = [
        e["activityTaskScheduledEventAttributes"]["activityType"]["name"]
        for e in events
        if e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"
    ]
    assert updates == ["approve"]
    assert "lookup_post" not in scheduled
    assert scheduled[-1] == "update_card"


@pytest.mark.asyncio
async def test_review_approved_before_refetch_replays():
    await Replayer(workflows=[ReviewWorkflow]).replay_workflow(_approved_history())


@pytest.mark.asyncio
async def test_approved_history_fails_replay_without_patch_guard(monkeypatch):
    """Control: with the re-fetch unguarded, the old approval would not replay."""
    from temporalio import workflow as temporal_workflow

    real_patched = temporal_workflow.patched
    monkeypatch.setattr(
        temporal_workflow,
        "patched",
        lambda patch_id: True if patch_id == TARGET_REFETCH_PATCH_ID else real_patched(patch_id),
    )
    result = await Replayer(workflows=[ReviewWorkflow]).replay_workflow(
        _approved_history(), raise_on_replay_failure=False
    )
    assert result.replay_failure is not None
    assert "Nondeterminism" in str(result.replay_failure)


# ---------- the eval hook ----------


async def _stored_card(make_review_input, suffix):
    """Run a Review until it expires (a past card) and return its workflow id."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, TargetClient(["found"])):
            _, handle = await _start(env, make_review_input, suffix)
            await env.sleep(timedelta(hours=2))  # review_timeout_hours=1 in tests
            assert await handle.result() == "expired"
            return handle.id


@pytest.mark.asyncio
async def test_eval_refetch_leaves_a_live_review_alone(make_review_input):
    """A pending Review holds its own copy of the evidence, so the eval must
    not remove text behind its back: no X read, no change."""
    from backend.target_refetch import refetch_stored_card_target

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, TargetClient(["found"])):
            review_input, handle = await _start(env, make_review_input, "eval_live")
            client = TargetClient(["gone"])
            reads_before = _x_reads()
            assert await refetch_stored_card_target(client, handle.id, "run1") == "live"
            assert client.target_lookups == 0
            assert _x_reads() == reads_before
            assert _db_card(handle.id)["post"]["text"] == review_input.post.text
            assert (await handle.query("get_card"))["post"]["text"] == review_input.post.text


@pytest.mark.asyncio
async def test_eval_refetch_found_keeps_card(make_review_input):
    from backend.target_refetch import refetch_stored_card_target

    workflow_id = await _stored_card(make_review_input, "eval_found")
    client = TargetClient(["found"])
    reads_before = _x_reads()
    assert await refetch_stored_card_target(client, workflow_id, "run1") == "found"
    stored = _db_card(workflow_id)
    assert QUESTION in stored["post"]["text"]
    assert stored["evidence"]["reply_plan"]["target_point"] == QUESTION
    assert _x_reads() - reads_before == 1


@pytest.mark.asyncio
async def test_eval_refetch_404_removes_text_once_and_is_idempotent(make_review_input):
    from backend.target_refetch import refetch_stored_card_target

    workflow_id = await _stored_card(make_review_input, "eval_gone")
    client = TargetClient(["error", "gone"])
    reads_before = _x_reads()
    with pytest.raises(RuntimeError):
        await refetch_stored_card_target(client, workflow_id, "run1")
    # A retry of the same eval run reuses its budget reservation
    assert await refetch_stored_card_target(client, workflow_id, "run1") == "removed"
    assert _x_reads() - reads_before == 1
    stored = _db_card(workflow_id)
    assert stored["post"]["text_removed"] is True
    assert stored["status"] == "expired"  # status is left as it was
    dumped = json.dumps(stored)
    for fragment in POST_FRAGMENTS:
        assert fragment not in dumped
    # A query of the closed Review (workflow state still holds the text) is
    # redacted on read through the stored removal
    from backend.activities.store_activities import apply_stored_removal

    queried = {
        "post": {"id": TARGET_ID, "text": "Struggling with distributed systems reliability."},
        "evidence": {"why_it_fits": f"The post asks '{QUESTION}'", "reply_plan": {"target_point": QUESTION}},
        "status": "expired",
    }
    redacted = apply_stored_removal(workflow_id, queried)
    assert redacted["post"]["text_removed"] is True
    for fragment in POST_FRAGMENTS:
        assert fragment not in json.dumps(redacted)
    # Already removed: no further X read
    assert await refetch_stored_card_target(client, workflow_id, "run2") == "removed"
    assert client.target_lookups == 2
    assert _x_reads() - reads_before == 1


@pytest.mark.asyncio
async def test_eval_refetch_over_budget_raises_and_keeps_text(make_review_input):
    from backend.budget import BudgetExceeded
    from backend.target_refetch import refetch_stored_card_target

    workflow_id = await _stored_card(make_review_input, "eval_budget")
    client = TargetClient(["gone"])
    set_kill_switch(True)
    with pytest.raises(BudgetExceeded):
        await refetch_stored_card_target(client, workflow_id, "run1")
    set_kill_switch(False)
    assert client.target_lookups == 0
    assert QUESTION in _db_card(workflow_id)["post"]["text"]


@pytest.mark.asyncio
async def test_eval_refetch_unknown_card():
    from backend.target_refetch import refetch_stored_card_target

    client = TargetClient(["found"])
    assert await refetch_stored_card_target(client, "review:test:nope", "run1") == "no_card"
    assert client.target_lookups == 0
