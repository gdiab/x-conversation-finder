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
            
            # Edit to valid text (just rearrange but keep valid sentences)
            edited_text = "Thanks for raising this point. Our product helps developers build reliable systems. Happy to share more."
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
                assert "after approval" in str(e).lower()
            
            assert edit_failed, "Expected edit after approval to be rejected"


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
            edited_text = "Thanks! Our product helps developers build reliable systems. Happy to help."
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
