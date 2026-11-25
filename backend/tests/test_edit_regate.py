"""
Tests for founder-edit re-gate feature (ADR-0006 Amendment E).

Tests with new quoted-claim architecture:
1. Edit that passes -> approve works
2. Edit adding uncovered claim -> failing sentence, approve requires post_anyway
3. Edit keeping gated sentence verbatim keeps verdict (B4: exact match)
4. Replay deterministic
5. S-6: Tests assert on stored card, not just workflow memory

Issue #14: no test here passes or fails on elapsed time. On the time-skipping
test server, env.sleep(1) returns early only while nothing is running; while
activities run, virtual time follows real time, so env.sleep(1) was a 1 s
real-time race against research -> plan -> draft -> claim_gate -> store_card.
Tests now wait on the card write they need (CardWrites), on an asyncio.Event
set inside a blocking activity, or (replay test) on recorded history events;
that last wait re-fetches every 50 ms, which sets only how soon it notices.
"""

import asyncio
import dataclasses
import pytest
import uuid

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.workflows.review import ReviewWorkflow, ReviewInput
from backend.activities.x_activities import XActivities
from backend.activities.reply_activities import ReplyActivities
from backend.activities.store_activities import (
    StoreCardInput,
    UpdateCardInput,
    store_card,
    update_card,
    get_all_cards,
)
from backend.activities.claim_gate_activity import claim_gate, verify_edited_text
from backend.adapters import FakeXClient, FakeReplyAgent
from backend.database import get_db, ReviewCardDB
from backend.tests.review_waits import query_card
from backend.tests.fake_gates import edited_text, gate_result

# Hang guard only: every wait below ends on an event, and this bound is hit
# only if the awaited event never happens (a real failure, not load).
WAIT_TIMEOUT_SEC = 60

# Start-to-close timeout for a verify_edited_text attempt the test holds open
# on purpose. With the fixture's 30 s, a long stall could expire the held
# attempt and turn the edit into "not_checked" while the test still treats it
# as in flight; an hour is never reached by a test.
HELD_GATE_TIMEOUT_SEC = 3600


def held_gate_input(make_review_input):
    """ReviewInput for tests whose verify_edited_text blocks on a test event."""
    review_input = make_review_input()
    return make_review_input(config=dataclasses.replace(
        review_input.config, activity_timeout_medium_sec=HELD_GATE_TIMEOUT_SEC,
    ))


class CardWrites:
    """Records each store_card/update_card call the worker has finished, by
    idempotency key, so a test can wait for a specific write.

    The key is recorded only after the real activity returns True; a write
    that found no row returns False, is not recorded, and the wait times out.
    True means the write committed, except that update_card also returns True
    when it ignores a stale edit_seq. The tests wait only on the pending store
    and the approval write, which carry no edit_seq. The pending card is stored after the
    workflow sets self.evidence, so once ":store:pending" is recorded the
    edit/approve validators see the draft.
    """

    def __init__(self) -> None:
        self._done: set[str] = set()
        self._changed = asyncio.Condition()

    async def _record(self, idempotency_key: str) -> None:
        async with self._changed:
            self._done.add(idempotency_key)
            self._changed.notify_all()

    async def wait_for(self, idempotency_key: str) -> None:
        async def _wait() -> None:
            async with self._changed:
                await self._changed.wait_for(lambda: idempotency_key in self._done)

        await asyncio.wait_for(_wait(), WAIT_TIMEOUT_SEC)

    async def wait_for_pending(self, workflow_id: str, client) -> None:
        """Wait until the pending card is stored and the workflow knows it.

        The recorder fires inside the store_card activity, before the
        workflow has processed the activity's completion. Approve is rejected
        until the workflow sets its card-ready flag (issue #9), which happens
        in the workflow task that handles that completion, so also wait for
        Temporal to record that workflow task (see _card_ready_recorded).
        """
        await self.wait_for(f"{workflow_id}:store:pending")
        handle = client.get_workflow_handle(workflow_id)

        async def _poll() -> None:
            while not _card_ready_recorded(await handle.fetch_history()):
                await asyncio.sleep(0.05)

        await asyncio.wait_for(_poll(), WAIT_TIMEOUT_SEC)

    async def wait_for_approved(self, workflow_id: str) -> None:
        """Wait until the workflow wrote status=approved to the stored card.

        The approve Update returns before run() writes that status, so a
        stored-card check right after approve needs this wait.
        """
        await self.wait_for(f"{workflow_id}:update:approve")

    def activities(self) -> list:
        @activity.defn(name="store_card")
        async def store_card_recorded(input: StoreCardInput) -> bool:
            result = await store_card(input)
            if result:
                await self._record(input.idempotency_key)
            return result

        @activity.defn(name="update_card")
        async def update_card_recorded(input: UpdateCardInput) -> bool:
            result = await update_card(input)
            if result:
                await self._record(input.idempotency_key)
            return result

        return [store_card_recorded, update_card_recorded]


@pytest.fixture
def card_writes() -> CardWrites:
    return CardWrites()


def _card_ready_recorded(history) -> bool:
    """True when the history shows the workflow task that handled the pending
    store_card completion: store_card scheduled, that activity completed, and
    a workflow task completed after it. run() sets the card-ready flag in that
    workflow task, right after the activity returns."""
    from temporalio.api.enums.v1 import EventType

    store_id = None
    store_done = False
    for event in history.events:
        kind = event.event_type
        if kind == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED and store_id is None:
            if event.activity_task_scheduled_event_attributes.activity_type.name == "store_card":
                store_id = event.event_id
        elif kind == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED and store_id is not None:
            if event.activity_task_completed_event_attributes.scheduled_event_id == store_id:
                store_done = True
        elif kind == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED and store_done:
            return True
    return False


def _past_approve_write(history) -> bool:
    """True when the history shows run() past the approval write: the approve
    Update was accepted, the update_card activity scheduled after it
    completed, and the workflow then started its confirm_posted timer."""
    from temporalio.api.enums.v1 import EventType

    approve_accepted = False
    approve_write_id = None
    approve_write_done = False
    for event in history.events:
        kind = event.event_type
        if kind == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED:
            name = event.workflow_execution_update_accepted_event_attributes.accepted_request.input.name
            approve_accepted = approve_accepted or name == "approve"
        elif kind == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED and approve_accepted:
            if event.activity_task_scheduled_event_attributes.activity_type.name == "update_card":
                approve_write_id = event.event_id
        elif kind == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED and approve_write_id is not None:
            if event.activity_task_completed_event_attributes.scheduled_event_id == approve_write_id:
                approve_write_done = True
        elif kind == EventType.EVENT_TYPE_TIMER_STARTED and approve_write_done:
            return True
    return False


async def fetch_history_past_approve(handle):
    """Return the workflow history once Temporal has recorded run() moving
    past the approval write (see _past_approve_write).

    Re-fetches until the recorded events show it; the result depends only on
    the history's content, never on how long the fetches take. (The test
    server's wait_new_event long-poll returns no events here, so it cannot be
    used as the barrier.)
    """
    async def _poll():
        while True:
            history = await handle.fetch_history()
            if _past_approve_write(history):
                return history
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(_poll(), WAIT_TIMEOUT_SEC)


@pytest.mark.asyncio
async def test_edit_passing_text_approve_works(make_review_input, x_activities, reply_activities, card_writes):
    """Edit with passing text -> approve works without post_anyway. S-6: Assert stored card."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.lookup_post,  # issue #9: approve re-fetches the target post
                reply_activities.research_post,
                reply_activities.plan_reply,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text,
                *card_writes.activities(),
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post_id}_edit_pass"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await card_writes.wait_for_pending(workflow_id, env.client)
            
            # Get original draft
            card = await query_card(handle)
            assert card["status"] == "pending"
            original_text = card["evidence"]["draft"]["text"]
            
            # Edit to valid text (closed-set non_claims + verbatim passage sentence)
            edited_text = "Thanks! Our product helps developers build reliable distributed systems. Happy to share details."
            client_request_id = str(uuid.uuid4())
            result_text = await handle.execute_update(
                "edit",
                args=[edited_text, client_request_id]
            )
            assert result_text == edited_text
            
            # Check card has edit gate results (all passing)
            card = await query_card(handle)
            assert card["edited_text_gate_results"] is not None
            assert all(r["passed"] for r in card["edited_text_gate_results"])
            
            # S-6: Assert stored card too
            db = get_db()
            try:
                card_db = db.query(ReviewCardDB).filter(
                    ReviewCardDB.workflow_id == workflow_id
                ).first()
                assert card_db is not None
                assert card_db.edited_text_gate_results is not None
                assert all(r["passed"] for r in card_db.edited_text_gate_results)
            finally:
                db.close()
            
            # Approve should work without post_anyway
            client_request_id = str(uuid.uuid4())
            approve_result = await handle.execute_update(
                "approve",
                args=["https://x.com/test/status/999", client_request_id, False]
            )
            assert "Approved" in approve_result
            
            await card_writes.wait_for_approved(workflow_id)
            
            # S-6: Verify stored card shows approved
            db = get_db()
            try:
                card_db = db.query(ReviewCardDB).filter(
                    ReviewCardDB.workflow_id == workflow_id
                ).first()
                assert card_db.status == "approved"
                assert card_db.post_anyway is None  # No post_anyway needed
            finally:
                db.close()


@pytest.mark.asyncio
async def test_edit_adding_uncovered_claim_requires_post_anyway(make_review_input, x_activities, reply_activities, card_writes):
    """Edit adding uncovered claim -> failing sentence, approve requires post_anyway. S-6: Assert stored card."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.lookup_post,  # issue #9: approve re-fetches the target post
                reply_activities.research_post,
                reply_activities.plan_reply,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text,
                *card_writes.activities(),
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post_id}_edit_fail"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await card_writes.wait_for_pending(workflow_id, env.client)
            
            # Edit to add uncovered claim (not a passage sentence, not a non_claim)
            edited_text = "Thanks for the question. Works on Kubernetes. Happy to help."
            client_request_id = str(uuid.uuid4())
            result_text = await handle.execute_update(
                "edit",
                args=[edited_text, client_request_id]
            )
            
            # Check card has edit gate results with failures
            card = await query_card(handle)
            assert card["edited_text_gate_results"] is not None
            gate_results = card["edited_text_gate_results"]
            
            # Should have at least one failure
            has_failure = any(not r["passed"] for r in gate_results)
            assert has_failure, "Expected at least one failing sentence"
            
            # Find the failing sentence
            failing = [r for r in gate_results if not r["passed"]]
            assert len(failing) > 0
            assert any("Kubernetes" in r["sentence"] for r in failing)
            
            # S-6: Assert stored card has failures
            db = get_db()
            try:
                card_db = db.query(ReviewCardDB).filter(
                    ReviewCardDB.workflow_id == workflow_id
                ).first()
                assert card_db.edited_text_gate_results is not None
                assert any(not r["passed"] for r in card_db.edited_text_gate_results)
            finally:
                db.close()
            
            # Approve without post_anyway should be rejected
            client_request_id = str(uuid.uuid4())
            approve_failed = False
            try:
                await handle.execute_update(
                    "approve",
                    args=["https://x.com/test/status/999", client_request_id, False]
                )
            except Exception as e:
                # Validator should reject it
                approve_failed = True
            assert approve_failed, "Expected approve without post_anyway to fail"
            
            # Approve WITH post_anyway should succeed (B1: test this path)
            client_request_id = str(uuid.uuid4())
            approve_result = await handle.execute_update(
                "approve",
                args=["https://x.com/test/status/999", client_request_id, True]
            )
            assert "Approved" in approve_result
            
            await card_writes.wait_for_approved(workflow_id)
            
            # Check card recorded post_anyway
            card = await query_card(handle)
            assert card["post_anyway"] is True
            assert card["post_anyway_failing_sentences"] is not None
            assert len(card["post_anyway_failing_sentences"]) > 0
            
            # S-6 + B1: Assert stored card has post_anyway (this is the B1 test)
            db = get_db()
            try:
                card_db = db.query(ReviewCardDB).filter(
                    ReviewCardDB.workflow_id == workflow_id
                ).first()
                assert card_db is not None
                assert card_db.status == "approved"
                assert card_db.post_anyway is True  # B1: This must be stored correctly
                assert card_db.post_anyway_failing_sentences is not None
                assert len(card_db.post_anyway_failing_sentences) > 0
            finally:
                db.close()


@pytest.mark.asyncio
async def test_b4_edit_exact_match_keeps_verdict(make_review_input, x_activities, reply_activities, card_writes):
    """B4: Edit keeping exact sentence (after normalization) reuses verdict."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.lookup_post,  # issue #9: approve re-fetches the target post
                reply_activities.research_post,
                reply_activities.plan_reply,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text,
                *card_writes.activities(),
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post_id}_edit_exact"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await card_writes.wait_for_pending(workflow_id, env.client)
            
            # Get original draft text
            card = await query_card(handle)
            original_text = card["evidence"]["draft"]["text"]
            
            # Edit but keep the exact same text (no changes)
            # This should result in kept_segment=True for all sentences
            client_request_id = str(uuid.uuid4())
            await handle.execute_update(
                "edit",
                args=[original_text, client_request_id]
            )
            
            # Check card has edit gate results
            card = await query_card(handle)
            gate_results = card["edited_text_gate_results"]
            
            # All sentences should pass (reusing gate)
            assert all(r["passed"] for r in gate_results), "Expected all sentences to pass"
            
            # B4: Should have kept segments (exact match after normalization)
            kept_segments = [r for r in gate_results if r["kept_segment"]]
            assert len(kept_segments) > 0, "Expected at least one kept segment"


@pytest.mark.asyncio
async def test_s1_edit_after_approve_rejected(make_review_input, x_activities, reply_activities, card_writes):
    """S-1: Edit after approve must be rejected."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.lookup_post,  # issue #9: approve re-fetches the target post
                reply_activities.research_post,
                reply_activities.plan_reply,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text,
                *card_writes.activities(),
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post_id}_edit_after_approve"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await card_writes.wait_for_pending(workflow_id, env.client)
            
            # Approve first
            client_request_id = str(uuid.uuid4())
            await handle.execute_update(
                "approve",
                args=["https://x.com/test/status/999", client_request_id, False]
            )
            
            # Try to edit after approval - should be rejected
            client_request_id = str(uuid.uuid4())
            edit_failed = False
            try:
                await handle.execute_update(
                    "edit",
                    args=["New text after approval.", client_request_id]
                )
            except Exception as e:
                edit_failed = True
                # The validator raises ValueError, which gets wrapped by Temporal
                # Just verify the exception was raised - the specific message is internal
            
            assert edit_failed, "Expected edit after approval to be rejected"


@pytest.mark.asyncio
async def test_s3_edit_in_flight_prevents_concurrent_approve(make_review_input, x_activities, reply_activities, card_writes):
    """
    S-3: Test that _edit_in_flight flag prevents concurrent approve (REAL concurrency test).
    
    Uses a blocking gate activity to create actual concurrency:
    1. Register verify_edited_text activity that blocks on asyncio.Event
    2. Start edit Update (wait for ACCEPTED, not completed)
    3. While gate activity is blocked, send approve
    4. Approve validator should see _edit_in_flight=True and reject with WorkflowUpdateFailedError
    5. Release the block, edit completes
    6. Later approve should work
    """
    import asyncio
    from temporalio import activity
    from temporalio.client import WorkflowUpdateStage
    
    # Create blocking event for the gate activity
    gate_block_event = asyncio.Event()
    gate_called_event = asyncio.Event()
    
    # Custom verify_edited_text that blocks on the event
    @activity.defn(name="verify_edited_text")
    async def verify_edited_text_blocking(input):
        """Blocking version of verify_edited_text for concurrency test."""
        activity.logger.info("Gate activity started, will block on event")
        gate_called_event.set()
        # Block until event is set
        await gate_block_event.wait()
        activity.logger.info("Gate activity unblocked, proceeding")
        # Return empty list (all sentences passed) - we're testing concurrency, not gate logic
        return gate_result(input)
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.lookup_post,  # issue #9: approve re-fetches the target post
                reply_activities.research_post,
                reply_activities.plan_reply,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text_blocking,  # Use blocking version
                *card_writes.activities(),
            ],
        ):
            review_input = held_gate_input(make_review_input)
            workflow_id = f"review:test:{review_input.post_id}_s3_concurrent"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await card_writes.wait_for_pending(workflow_id, env.client)
            
            # Start edit Update, wait only for ACCEPTED (not completed)
            client_request_id = str(uuid.uuid4())
            edit_text = "Thanks! Our product helps developers build reliable distributed systems. Happy to help."
            edit_handle = await handle.start_update(
                "edit",
                args=[edit_text, client_request_id],
                wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            )
            
            # Wait for the gate activity to start. The edit handler raises
            # _edit_in_flight_count before it schedules this activity and
            # lowers it only after the activity returns, so from here until
            # gate_block_event is set the edit is in flight: no sleep needed.
            await asyncio.wait_for(gate_called_event.wait(), timeout=WAIT_TIMEOUT_SEC)

            # The workflow query shows the edit has not been committed yet
            # (B2: text and gate state change only after the gate returns)
            card = await query_card(handle)
            assert card["edited_text_gate_state"] is None
            assert card["evidence"]["draft"]["text"] != edit_text

            # NOW try to approve while edit gate is in flight
            # S-3: This should raise WorkflowUpdateFailedError because _edit_in_flight_count > 0
            # The validator checks and raises ValueError with specific message
            from temporalio.client import WorkflowUpdateFailedError
            client_request_id2 = str(uuid.uuid4())
            with pytest.raises(WorkflowUpdateFailedError) as exc_info:
                await handle.execute_update(
                    "approve",
                    args=["https://x.com/test/status/999", client_request_id2, False]
                )
            # Verify the error message (robust for Python 3.11 and 3.13)
            error_str = str(exc_info.value)
            cause = exc_info.value.cause
            if cause:
                error_str += " " + str(cause)
            assert "edit gate is in progress" in error_str.lower()
            
            # Release the block so edit can complete
            gate_block_event.set()
            
            # Wait for edit to complete
            await edit_handle.result()
            
            # Now approve should work (no failures in the edit)
            client_request_id3 = str(uuid.uuid4())
            approve_result = await handle.execute_update(
                "approve",
                args=["https://x.com/test/status/999", client_request_id3, False]
            )
            assert "Approved" in approve_result


@pytest.mark.asyncio
async def test_edit_regate_replay_deterministic(make_review_input, x_activities, reply_activities, card_writes):
    """Test that edit re-gate replay is deterministic."""
    from temporalio.worker import Replayer
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.lookup_post,  # issue #9: approve re-fetches the target post
                reply_activities.research_post,
                reply_activities.plan_reply,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text,
                *card_writes.activities(),
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post_id}_replay"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await card_writes.wait_for_pending(workflow_id, env.client)
            
            # Edit
            edited_text = "Thanks! Our product helps developers build reliable distributed systems. Happy to help."
            client_request_id = str(uuid.uuid4())
            await handle.execute_update(
                "edit",
                args=[edited_text, client_request_id]
            )
            
            # Approve with post_anyway=False (should work since edit passes)
            client_request_id = str(uuid.uuid4())
            await handle.execute_update(
                "approve",
                args=["https://x.com/test/status/999", client_request_id, False]
            )
            
            # Fetch history only after Temporal recorded run() moving past the
            # approval write (update_card completed, confirm timer started), so
            # the replay covers the same events on every run
            history = await fetch_history_past_approve(handle)
            accepted_updates = [
                e.workflow_execution_update_accepted_event_attributes.accepted_request.input.name
                for e in history.events
                if e.HasField("workflow_execution_update_accepted_event_attributes")
            ]
            assert accepted_updates == ["edit", "approve"]
            assert any(e.HasField("timer_started_event_attributes") for e in history.events)
            
            # Replay the history
            replayer = Replayer(workflows=[ReviewWorkflow])
            await replayer.replay_workflow(history)


@pytest.mark.asyncio
async def test_b2_edit_regate_fails_open_fix(make_review_input, x_activities, reply_activities, card_writes):
    """
    QA R11 B2: Edit must not mutate draft.text until gate completes.
    
    Repro: edit to "Our product is SOC2 certified and 10x faster.", then approve(post_anyway=False).
    Without fix: edit mutates text before gate runs, gate fails, but approve still returns Approved.
    With fix: text committed only after gate, failing state recorded, approve requires post_anyway=True.
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.lookup_post,  # issue #9: approve re-fetches the target post
                reply_activities.research_post,
                reply_activities.plan_reply,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text,
                *card_writes.activities(),
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post_id}_b2_fix"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await card_writes.wait_for_pending(workflow_id, env.client)
            
            # Edit to text that will fail gate (numbers + compliance terms)
            edited_text = "Our product is SOC2 certified and 10x faster."
            client_request_id = str(uuid.uuid4())
            
            result_text = await handle.execute_update(
                "edit",
                args=[edited_text, client_request_id]
            )
            assert result_text == edited_text
            
            # Check stored card has gate_state = "failed"
            db = get_db()
            try:
                card_db = db.query(ReviewCardDB).filter(
                    ReviewCardDB.workflow_id == workflow_id
                ).first()
                assert card_db is not None
                assert card_db.edited_text_gate_state == "failed"
                assert card_db.edited_text_gate_results is not None
                # Verify some sentences failed
                assert any(not r["passed"] for r in card_db.edited_text_gate_results)
            finally:
                db.close()
            
            # Approve without post_anyway should FAIL
            from temporalio.client import WorkflowUpdateFailedError
            client_request_id = str(uuid.uuid4())
            with pytest.raises(WorkflowUpdateFailedError) as exc_info:
                await handle.execute_update(
                    "approve",
                    args=["https://x.com/test/status/999", client_request_id, False]
                )
            # Verify the error message (robust for Python 3.11 and 3.13)
            error_str = str(exc_info.value)
            cause = exc_info.value.cause
            if cause:
                error_str += " " + str(cause)
            assert "post_anyway" in error_str.lower()
            
            # Approve WITH post_anyway=True should succeed
            client_request_id = str(uuid.uuid4())
            approve_result = await handle.execute_update(
                "approve",
                args=["https://x.com/test/status/999", client_request_id, True]
            )
            assert "Approved" in approve_result


@pytest.mark.asyncio
async def test_b3_concurrent_edits_with_counter(make_review_input, x_activities, reply_activities, card_writes):
    """
    QA R11 B3: _edit_in_flight must be a counter, not bool.
    
    Two concurrent edits would clear bool early. Use counter to properly track in-flight edits.
    Also tests that pytest.raises uses WorkflowUpdateFailedError (not generic Exception).
    """
    import asyncio
    from temporalio.client import WorkflowUpdateFailedError
    from temporalio.exceptions import ApplicationError
    
    # One gate start/release event pair per edit, keyed by a marker in the
    # edit text, so the test controls which edit finishes first.
    gate_started = {"FIRST": asyncio.Event(), "SECOND": asyncio.Event()}
    gate_release = {"FIRST": asyncio.Event(), "SECOND": asyncio.Event()}
    
    from temporalio import activity as activity_module
    
    @activity_module.defn(name="verify_edited_text")
    async def verify_edited_text_blocking(input):
        """Gate that blocks each edit until the test releases it."""
        marker = "FIRST" if "FIRST" in edited_text(input) else "SECOND"
        gate_started[marker].set()
        await gate_release[marker].wait()
        return gate_result(input)  # passing results
    
    async def assert_approve_rejected_in_flight():
        with pytest.raises(WorkflowUpdateFailedError) as exc_info:
            await handle.execute_update(
                "approve",
                args=["https://x.com/test/status/999", str(uuid.uuid4()), False]
            )
        # Verify the error message (robust for Python 3.11 and 3.13)
        error_str = str(exc_info.value)
        cause = exc_info.value.cause
        if cause:
            error_str += " " + str(cause)
        assert "edit gate is in progress" in error_str.lower()
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.lookup_post,  # issue #9: approve re-fetches the target post
                reply_activities.research_post,
                reply_activities.plan_reply,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text_blocking,
                *card_writes.activities(),
            ],
        ):
            review_input = held_gate_input(make_review_input)
            workflow_id = f"review:test:{review_input.post_id}_b3_concurrent"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await card_writes.wait_for_pending(workflow_id, env.client)
            
            # Start two edits; both block in their gate activity
            edit1_task = asyncio.create_task(
                handle.execute_update("edit", args=["FIRST edit text. Thanks!", str(uuid.uuid4())])
            )
            await asyncio.wait_for(gate_started["FIRST"].wait(), timeout=WAIT_TIMEOUT_SEC)
            edit2_task = asyncio.create_task(
                handle.execute_update("edit", args=["SECOND edit text. Thanks!", str(uuid.uuid4())])
            )
            await asyncio.wait_for(gate_started["SECOND"].wait(), timeout=WAIT_TIMEOUT_SEC)
            
            # Two edits in flight: approve is rejected
            await assert_approve_rejected_in_flight()
            
            # Finish FIRST; SECOND is still gating. A bool flag would now read
            # False and let approve through; the counter is still 1.
            gate_release["FIRST"].set()
            await edit1_task
            await assert_approve_rejected_in_flight()
            
            # Finish SECOND: counter is 0, approve works
            gate_release["SECOND"].set()
            await edit2_task
            approve_result = await handle.execute_update(
                "approve",
                args=["https://x.com/test/status/999", str(uuid.uuid4()), False]
            )
            assert "Approved" in approve_result


@pytest.mark.asyncio
async def test_c_edit_regate_activity_error_not_checked(make_review_input, x_activities, reply_activities, card_writes):
    """
    QA R11 C: If edit re-gate activity ERRORS (retries exhausted), record state as "not_checked".
    
    Approve requires post_anyway=True for both failed and not_checked states.
    """
    import asyncio
    
    # Setup failing gate activity
    gate_call_count = 0
    max_attempts = 3
    
    from temporalio import activity as activity_module
    
    @activity_module.defn(name="verify_edited_text")
    async def verify_edited_text_failing(input):
        """Failing version of verify_edited_text that exhausts retries."""
        from temporalio import activity
        nonlocal gate_call_count
        gate_call_count += 1
        activity.logger.info(f"Gate activity call {gate_call_count}/{max_attempts} - raising error")
        raise RuntimeError("Simulated gate activity failure")
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.lookup_post,  # issue #9: approve re-fetches the target post
                reply_activities.research_post,
                reply_activities.plan_reply,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text_failing,
                *card_writes.activities(),
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post_id}_c_error"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await card_writes.wait_for_pending(workflow_id, env.client)
            
            # Edit will fail in gate activity
            gate_call_count = 0
            edited_text = "Edited text that can't be gated."
            client_request_id = str(uuid.uuid4())
            
            # Edit should succeed (doesn't raise), but state is "not_checked"
            result_text = await handle.execute_update(
                "edit",
                args=[edited_text, client_request_id]
            )
            assert result_text == edited_text
            
            # Verify gate was retried
            assert gate_call_count == max_attempts
            
            # Check stored card has gate_state = "not_checked"
            db = get_db()
            try:
                card_db = db.query(ReviewCardDB).filter(
                    ReviewCardDB.workflow_id == workflow_id
                ).first()
                assert card_db is not None
                assert card_db.edited_text_gate_state == "not_checked"
            finally:
                db.close()
            
            # Approve without post_anyway should FAIL
            from temporalio.client import WorkflowUpdateFailedError
            client_request_id = str(uuid.uuid4())
            with pytest.raises(WorkflowUpdateFailedError) as exc_info:
                await handle.execute_update(
                    "approve",
                    args=["https://x.com/test/status/999", client_request_id, False]
                )
            # Verify the error message (robust for Python 3.11 and 3.13)
            error_str = str(exc_info.value)
            cause = exc_info.value.cause
            if cause:
                error_str += " " + str(cause)
            assert "retries exhausted" in error_str.lower()
            
            # Approve WITH post_anyway=True should succeed
            client_request_id = str(uuid.uuid4())
            approve_result = await handle.execute_update(
                "approve",
                args=["https://x.com/test/status/999", client_request_id, True]
            )
            assert "Approved" in approve_result


@pytest.mark.asyncio
async def test_qa_r12_concurrent_edits_last_writer_wins(make_review_input, x_activities, reply_activities, card_writes):
    """
    QA R12: Concurrent edits resolve as last-writer-wins by finish order.
    
    Start two edits with different texts. Control gate timing so they finish in
    a known order. Verify the last to complete sets the final draft.text.
    """
    import asyncio
    from temporalio import activity as activity_module
    
    # State to control gate timing
    edit1_gate_event = asyncio.Event()
    edit2_gate_event = asyncio.Event()
    edit1_release_event = asyncio.Event()
    edit2_release_event = asyncio.Event()
    
    @activity_module.defn(name="verify_edited_text")
    async def verify_edited_text_controlled(input):
        """Gate activity that blocks per-edit to control finish order."""
        from temporalio import activity
        text = edited_text(input)
        activity.logger.info(f"Gate for: {text[:20]}")
        
        if "FIRST" in text:
            activity.logger.info("FIRST edit gate, signaling and blocking")
            edit1_gate_event.set()
            await edit1_release_event.wait()
            activity.logger.info("FIRST edit gate released")
        elif "SECOND" in text:
            activity.logger.info("SECOND edit gate, signaling and blocking")
            edit2_gate_event.set()
            await edit2_release_event.wait()
            activity.logger.info("SECOND edit gate released")
        
        # Both pass
        return gate_result(input)
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.lookup_post,  # issue #9: approve re-fetches the target post
                reply_activities.research_post,
                reply_activities.plan_reply,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text_controlled,
                *card_writes.activities(),
            ],
        ):
            review_input = held_gate_input(make_review_input)
            workflow_id = f"review:test:{review_input.post_id}_r12_lww"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await card_writes.wait_for_pending(workflow_id, env.client)
            
            # Start FIRST edit (will block in gate)
            edit1_task = asyncio.create_task(
                handle.execute_update(
                    "edit",
                    args=["FIRST edit text. Thanks!", str(uuid.uuid4())]
                )
            )
            
            # Wait for first gate to be called
            await asyncio.wait_for(edit1_gate_event.wait(), timeout=WAIT_TIMEOUT_SEC)
            
            # Start SECOND edit (will block in gate)
            edit2_task = asyncio.create_task(
                handle.execute_update(
                    "edit",
                    args=["SECOND edit text. Thanks!", str(uuid.uuid4())]
                )
            )
            
            # Wait for second gate to be called
            await asyncio.wait_for(edit2_gate_event.wait(), timeout=WAIT_TIMEOUT_SEC)
            
            # Now both edits are blocked in their gates
            # Release SECOND first (so FIRST finishes last and wins)
            edit2_release_event.set()
            await edit2_task
            
            # Then release FIRST (finishes last)
            edit1_release_event.set()
            await edit1_task
            
            # Check the card - last to finish (FIRST) should be the final text
            db = get_db()
            try:
                card_db = db.query(ReviewCardDB).filter(
                    ReviewCardDB.workflow_id == workflow_id
                ).first()
                assert card_db is not None
                
                final_text = card_db.evidence_data["draft"]["text"]
                assert "FIRST" in final_text, f"Expected FIRST to win (finished last), got: {final_text}"
            finally:
                db.close()


# ==================== QA R14: S5 EDIT_SEQ TESTS ====================

@pytest.mark.asyncio
async def test_r14_s5_store_ignores_lower_edit_seq():
    """S5 (QA R14): Store activity ignores writes with lower edit_seq than stored."""
    from backend.activities.store_activities import UpdateCardInput, update_card
    from backend.database import ReviewCardDB, get_db
    from datetime import datetime, timezone
    
    # First, create a card with edit_seq=2 and skip_reason="Version 2"
    db = get_db()
    card_db = None
    try:
        card_db = ReviewCardDB(
            workflow_id="test_s5_store",
            post_data={"id": "12345", "text": "test post"},
            status="pending",
            evidence_data={"draft": {"text": "test"}},
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            skip_reason="Version 2",
            edit_seq=2  # Current seq is 2
        )
        db.add(card_db)
        db.commit()
        db.refresh(card_db)
        
        # Try to update with edit_seq=1 (lower than stored 2)
        input_stale = UpdateCardInput(
            workflow_id="test_s5_store",
            idempotency_key="stale_write",
            skip_reason="Stale version 1",
            edit_seq=1  # Lower than stored seq
        )
        
        await update_card(input_stale)
        
        # Verify card still has version 2 (stale write was ignored)
        db.refresh(card_db)
        assert card_db.skip_reason == "Version 2"
        assert card_db.edit_seq == 2
        
        # Now update with edit_seq=3 (higher than stored 2)
        input_new = UpdateCardInput(
            workflow_id="test_s5_store",
            idempotency_key="new_write",
            skip_reason="Version 3",
            edit_seq=3
        )
        
        await update_card(input_new)
        
        # Verify card now has version 3
        db.refresh(card_db)
        assert card_db.skip_reason == "Version 3"
        assert card_db.edit_seq == 3
        
    finally:
        # Clean up
        if card_db:
            db.delete(card_db)
            db.commit()
        db.close()


@pytest.mark.asyncio
async def test_qa_r15_edit_four_segments_rejected(make_review_input, x_activities, reply_activities, card_writes):
    """
    QA R15: Edit path must reject 4+ segments (same 1-3 cap as model path).
    
    Example: 'Hi! Our product helps developers build reliable systems. Happy to help. What stack are you on?'
    This has 4 sentences and should fail with specific reason.
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.lookup_post,  # issue #9: approve re-fetches the target post
                reply_activities.research_post,
                reply_activities.plan_reply,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text,
                *card_writes.activities(),
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post_id}_r15_four_segments"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await card_writes.wait_for_pending(workflow_id, env.client)
            
            # Edit to exactly the example text with 4 sentences
            edited_text = "Hi! Our product helps developers build reliable systems. Happy to help. What stack are you on?"
            client_request_id = str(uuid.uuid4())
            
            result_text = await handle.execute_update(
                "edit",
                args=[edited_text, client_request_id]
            )
            assert result_text == edited_text
            
            # QA R15: Verify gate results show all 4 sentences failed with specific reason
            card = await query_card(handle)
            gate_results = card["edited_text_gate_results"]
            
            # Should have 4 results (one per sentence)
            assert len(gate_results) == 4, f"Expected 4 results, got {len(gate_results)}"
            
            # All should fail with the segment cap reason
            for result in gate_results:
                assert not result["passed"], f"Expected all sentences to fail, but {result['sentence']} passed"
                assert "at most 3 sentences" in result["reason"], f"Expected segment cap reason, got: {result['reason']}"
                assert "(got 4)" in result["reason"], f"Expected count in reason, got: {result['reason']}"
            
            # Verify stored card has failures
            db = get_db()
            try:
                card_db = db.query(ReviewCardDB).filter(
                    ReviewCardDB.workflow_id == workflow_id
                ).first()
                assert card_db is not None
                assert card_db.edited_text_gate_results is not None
                assert len(card_db.edited_text_gate_results) == 4
                assert all(not r["passed"] for r in card_db.edited_text_gate_results)
            finally:
                db.close()
            
            # Approve without post_anyway should fail
            from temporalio.client import WorkflowUpdateFailedError
            client_request_id = str(uuid.uuid4())
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update(
                    "approve",
                    args=["https://x.com/test/status/999", client_request_id, False]
                )
            
            # Approve WITH post_anyway should succeed
            client_request_id = str(uuid.uuid4())
            approve_result = await handle.execute_update(
                "approve",
                args=["https://x.com/test/status/999", client_request_id, True]
            )
            assert "Approved" in approve_result


@pytest.mark.asyncio
async def test_r14_s5_workflow_race_edit_seq(make_review_input, x_activities, reply_activities, card_writes):
    """
    S5 (QA R14): Workflow-level race with edit_seq.
    
    Scenario:
    - Edit A finishes and gets seq 1
    - Edit B finishes and gets seq 2
    - A late retried write carrying seq 1 arrives
    
    Assert that the card ends at B's text and seq 2.
    """
    import asyncio
    from temporalio import activity as activity_module
    from backend.activities.store_activities import UpdateCardInput, update_card
    
    # State to control timing
    edit_a_gate_event = asyncio.Event()
    edit_b_gate_event = asyncio.Event()
    edit_a_release_event = asyncio.Event()
    edit_b_release_event = asyncio.Event()
    
    @activity_module.defn(name="verify_edited_text")
    async def verify_edited_text_controlled(input):
        """Gate activity that blocks per-edit to control finish order."""
        from temporalio import activity
        text = edited_text(input)
        activity.logger.info(f"Gate for: {text[:20]}")
        
        if "EDIT_A" in text:
            activity.logger.info("EDIT_A gate, signaling and blocking")
            edit_a_gate_event.set()
            await edit_a_release_event.wait()
            activity.logger.info("EDIT_A gate released")
        elif "EDIT_B" in text:
            activity.logger.info("EDIT_B gate, signaling and blocking")
            edit_b_gate_event.set()
            await edit_b_release_event.wait()
            activity.logger.info("EDIT_B gate released")
        
        # Both pass
        return gate_result(input)
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.lookup_post,  # issue #9: approve re-fetches the target post
                reply_activities.research_post,
                reply_activities.plan_reply,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text_controlled,
                *card_writes.activities(),
            ],
        ):
            review_input = held_gate_input(make_review_input)
            workflow_id = f"review:test:{review_input.post_id}_s5_race"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await card_writes.wait_for_pending(workflow_id, env.client)
            
            # Start EDIT_A (will block in gate)
            edit_a_task = asyncio.create_task(
                handle.execute_update(
                    "edit",
                    args=["EDIT_A text. Thanks!", str(uuid.uuid4())]
                )
            )
            
            # Wait for edit A gate to be called
            await asyncio.wait_for(edit_a_gate_event.wait(), timeout=WAIT_TIMEOUT_SEC)
            
            # Start EDIT_B (will block in gate)
            edit_b_task = asyncio.create_task(
                handle.execute_update(
                    "edit",
                    args=["EDIT_B text. Thanks!", str(uuid.uuid4())]
                )
            )
            
            # Wait for edit B gate to be called
            await asyncio.wait_for(edit_b_gate_event.wait(), timeout=WAIT_TIMEOUT_SEC)
            
            # Release EDIT_A first (finishes first, gets seq 1)
            edit_a_release_event.set()
            await edit_a_task
            
            # Release EDIT_B second (finishes second, gets seq 2)
            edit_b_release_event.set()
            await edit_b_task
            
            # Simulate a late retried write carrying seq 1 (from EDIT_A)
            # This would happen if the update_card activity was retried after EDIT_B already completed
            # Note: We can't easily simulate this via UpdateCardInput which requires full Evidence objects
            # The actual edit_seq prevention logic is tested in test_r14_s5_store_ignores_lower_edit_seq
            
            # Check the card - should still have EDIT_B's text and seq 2
            db = get_db()
            try:
                card_db = db.query(ReviewCardDB).filter(
                    ReviewCardDB.workflow_id == workflow_id
                ).first()
                assert card_db is not None
                
                final_text = card_db.evidence_data["draft"]["text"]
                assert "EDIT_B" in final_text, f"Expected EDIT_B to win (higher seq), got: {final_text}"
                assert card_db.edit_seq == 2, f"Expected edit_seq=2, got: {card_db.edit_seq}"
                
            finally:
                db.close()
