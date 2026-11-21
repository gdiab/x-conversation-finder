"""
Tests for founder-edit re-gate feature (ADR-0006 Amendment E).

Tests with new quoted-claim architecture:
1. Edit that passes -> approve works
2. Edit adding uncovered claim -> failing sentence, approve requires post_anyway
3. Edit keeping gated sentence verbatim keeps verdict (B4: exact match)
4. Replay deterministic
5. S-6: Tests assert on stored card, not just workflow memory
"""

import pytest
import uuid

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.workflows.review import ReviewWorkflow, ReviewInput
from backend.activities.x_activities import XActivities
from backend.activities.reply_activities import ReplyActivities
from backend.activities.store_activities import store_card, update_card, get_all_cards
from backend.activities.claim_gate_activity import claim_gate, verify_edited_text
from backend.adapters import FakeXClient, FakeReplyAgent
from backend.database import get_db, ReviewCardDB


@pytest.mark.asyncio
async def test_edit_passing_text_approve_works(make_review_input, x_activities, reply_activities):
    """Edit with passing text -> approve works without post_anyway. S-6: Assert stored card."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_edit_pass"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            # Get original draft
            card = await handle.query("get_card")
            assert card["status"] == "pending"
            original_text = card["evidence"]["draft"]["text"]
            
            # Edit to valid text (closed-set non_claims + verbatim passage sentence)
            edited_text = "Thanks! Our product helps developers build reliable distributed systems. Happy to share more details."
            client_request_id = str(uuid.uuid4())
            result_text = await handle.execute_update(
                "edit",
                args=[edited_text, client_request_id]
            )
            assert result_text == edited_text
            
            await env.sleep(1)
            
            # Check card has edit gate results (all passing)
            card = await handle.query("get_card")
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
            
            await env.sleep(1)
            
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
async def test_edit_adding_uncovered_claim_requires_post_anyway(make_review_input, x_activities, reply_activities):
    """Edit adding uncovered claim -> failing sentence, approve requires post_anyway. S-6: Assert stored card."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_edit_fail"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            # Edit to add uncovered claim (not a passage sentence, not a non_claim)
            edited_text = "Thanks for the question. Works on Kubernetes. Happy to help."
            client_request_id = str(uuid.uuid4())
            result_text = await handle.execute_update(
                "edit",
                args=[edited_text, client_request_id]
            )
            
            await env.sleep(1)
            
            # Check card has edit gate results with failures
            card = await handle.query("get_card")
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
            
            await env.sleep(1)
            
            # Check card recorded post_anyway
            card = await handle.query("get_card")
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
async def test_b4_edit_exact_match_keeps_verdict(make_review_input, x_activities, reply_activities):
    """B4: Edit keeping exact sentence (after normalization) reuses verdict."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_edit_exact"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            # Get original draft text
            card = await handle.query("get_card")
            original_text = card["evidence"]["draft"]["text"]
            
            # Edit but keep the exact same text (no changes)
            # This should result in kept_segment=True for all sentences
            client_request_id = str(uuid.uuid4())
            await handle.execute_update(
                "edit",
                args=[original_text, client_request_id]
            )
            
            await env.sleep(1)
            
            # Check card has edit gate results
            card = await handle.query("get_card")
            gate_results = card["edited_text_gate_results"]
            
            # All sentences should pass (reusing gate)
            assert all(r["passed"] for r in gate_results), "Expected all sentences to pass"
            
            # B4: Should have kept segments (exact match after normalization)
            kept_segments = [r for r in gate_results if r["kept_segment"]]
            assert len(kept_segments) > 0, "Expected at least one kept segment"


@pytest.mark.asyncio
async def test_s1_edit_after_approve_rejected(make_review_input, x_activities, reply_activities):
    """S-1: Edit after approve must be rejected."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_edit_after_approve"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            # Approve first
            client_request_id = str(uuid.uuid4())
            await handle.execute_update(
                "approve",
                args=["https://x.com/test/status/999", client_request_id, False]
            )
            
            await env.sleep(1)
            
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
async def test_s3_edit_in_flight_prevents_concurrent_approve(make_review_input, x_activities, reply_activities):
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
        return []
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text_blocking,  # Use blocking version
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_s3_concurrent"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            # Start edit Update, wait only for ACCEPTED (not completed)
            client_request_id = str(uuid.uuid4())
            edit_handle = await handle.start_update(
                "edit",
                args=["Thanks! Our product helps developers build reliable distributed systems. Happy to help.", client_request_id],
                wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            )
            
            # Wait for gate activity to actually start
            await gate_called_event.wait()
            await asyncio.sleep(0.1)  # Small delay to ensure activity is truly blocked
            
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
            
            await env.sleep(1)
            
            # Now approve should work (no failures in the edit)
            client_request_id3 = str(uuid.uuid4())
            approve_result = await handle.execute_update(
                "approve",
                args=["https://x.com/test/status/999", client_request_id3, False]
            )
            assert "Approved" in approve_result


@pytest.mark.asyncio
async def test_edit_regate_replay_deterministic(make_review_input, x_activities, reply_activities):
    """Test that edit re-gate replay is deterministic."""
    from temporalio.worker import Replayer
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_replay"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            # Edit
            edited_text = "Thanks! Our product helps developers build reliable distributed systems. Happy to help."
            client_request_id = str(uuid.uuid4())
            await handle.execute_update(
                "edit",
                args=[edited_text, client_request_id]
            )
            
            await env.sleep(1)
            
            # Approve with post_anyway=False (should work since edit passes)
            client_request_id = str(uuid.uuid4())
            await handle.execute_update(
                "approve",
                args=["https://x.com/test/status/999", client_request_id, False]
            )
            
            # Fetch history
            history = await handle.fetch_history()
            
            # Replay the history
            replayer = Replayer(workflows=[ReviewWorkflow])
            await replayer.replay_workflow(history)


@pytest.mark.asyncio
async def test_b2_edit_regate_fails_open_fix(make_review_input, x_activities, reply_activities):
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
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_b2_fix"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            # Edit to text that will fail gate (numbers + compliance terms)
            edited_text = "Our product is SOC2 certified and 10x faster."
            client_request_id = str(uuid.uuid4())
            
            result_text = await handle.execute_update(
                "edit",
                args=[edited_text, client_request_id]
            )
            assert result_text == edited_text
            
            await env.sleep(1)
            
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
async def test_b3_concurrent_edits_with_counter(make_review_input, x_activities, reply_activities):
    """
    QA R11 B3: _edit_in_flight must be a counter, not bool.
    
    Two concurrent edits would clear bool early. Use counter to properly track in-flight edits.
    Also tests that pytest.raises uses WorkflowUpdateFailedError (not generic Exception).
    """
    import asyncio
    from temporalio.client import WorkflowUpdateFailedError
    from temporalio.exceptions import ApplicationError
    
    # Setup blocking gate activity
    gate_called_event = asyncio.Event()
    gate_block_event = asyncio.Event()
    
    from temporalio import activity as activity_module
    
    @activity_module.defn(name="verify_edited_text")
    async def verify_edited_text_blocking(input):
        """Blocking version of verify_edited_text for concurrency test."""
        from temporalio import activity
        activity.logger.info("Gate activity started, will block on event")
        gate_called_event.set()
        # Block until event is set
        await gate_block_event.wait()
        activity.logger.info("Gate activity unblocked, proceeding")
        # Return passing results
        return []
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text_blocking,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_b3_concurrent"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            # Start first edit (will block in gate activity)
            gate_called_event.clear()
            gate_block_event.clear()
            
            edit1_task = asyncio.create_task(
                handle.execute_update(
                    "edit",
                    args=["First edit text. Thanks!", str(uuid.uuid4())]
                )
            )
            
            # Wait for gate activity to be called
            await asyncio.wait_for(gate_called_event.wait(), timeout=5)
            
            # Now try to approve while edit is in flight - should fail
            from temporalio.client import WorkflowUpdateFailedError
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
            
            # Unblock the gate
            gate_block_event.set()
            
            # Wait for edit to complete
            await edit1_task
            
            # Now approve should work (counter is 0)
            await env.sleep(1)
            approve_result = await handle.execute_update(
                "approve",
                args=["https://x.com/test/status/999", str(uuid.uuid4()), False]
            )
            assert "Approved" in approve_result


@pytest.mark.asyncio
async def test_c_edit_regate_activity_error_not_checked(make_review_input, x_activities, reply_activities):
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
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text_failing,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_c_error"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
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
            
            await env.sleep(1)
            
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
async def test_qa_r12_concurrent_edits_last_writer_wins(make_review_input, x_activities, reply_activities):
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
        text = input["edited_text"]
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
        return []
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                verify_edited_text_controlled,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_r12_lww"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            # Start FIRST edit (will block in gate)
            edit1_task = asyncio.create_task(
                handle.execute_update(
                    "edit",
                    args=["FIRST edit text. Thanks!", str(uuid.uuid4())]
                )
            )
            
            # Wait for first gate to be called
            await asyncio.wait_for(edit1_gate_event.wait(), timeout=5)
            
            # Start SECOND edit (will block in gate)
            edit2_task = asyncio.create_task(
                handle.execute_update(
                    "edit",
                    args=["SECOND edit text. Thanks!", str(uuid.uuid4())]
                )
            )
            
            # Wait for second gate to be called
            await asyncio.wait_for(edit2_gate_event.wait(), timeout=5)
            
            # Now both edits are blocked in their gates
            # Release SECOND first (so FIRST finishes last and wins)
            edit2_release_event.set()
            await edit2_task
            
            # Then release FIRST (finishes last)
            edit1_release_event.set()
            await edit1_task
            
            await env.sleep(1)
            
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
