"""
Replay tests for Review workflow.

Per ADR-0003: Temporal replay tests catch non-determinism.
Uses fake adapters so no API keys needed.
"""

import pytest
import uuid
from datetime import datetime, timedelta

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.workflows.review import ReviewWorkflow, ReviewInput
from backend.activities.x_activities import XActivities
from backend.activities.reply_activities import ReplyActivities
from backend.activities.store_activities import store_card, update_card
from backend.activities.claim_gate_activity import claim_gate
from backend.adapters import FakeXClient, FakeReplyAgent


@pytest.mark.asyncio
async def test_review_workflow_approve(make_review_input, x_activities, reply_activities):
    """Test Review workflow with approval."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            card = await handle.query("get_card")
            assert card["status"] == "pending"
            assert card["evidence"]["post_id"] == review_input.post.id
            
            # Approve
            client_request_id = str(uuid.uuid4())
            await handle.signal(
                "approve",
                args=["https://x.com/test/status/999", client_request_id]
            )
            
            await env.sleep(1)
            
            # Confirm posted with reply URL (different post ID, same conversation)
            client_request_id = str(uuid.uuid4())
            reply_msg = await handle.execute_update(
                "confirm_posted",
                args=["https://x.com/founder/status/9999", client_request_id]
            )
            assert "Confirmed" in reply_msg
            
            # Skip ahead 3 days for follow-up
            await env.sleep(timedelta(days=3, hours=1))
            
            result = await handle.result()
            assert result == "followup_due"


@pytest.mark.asyncio
async def test_review_workflow_skip(make_review_input, x_activities, reply_activities):
    """Test Review workflow with skip."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_skip"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            client_request_id = str(uuid.uuid4())
            await handle.signal(
                "skip",
                args=["Not relevant", client_request_id]
            )
            
            result = await handle.result()
            assert result == "skipped"


@pytest.mark.asyncio
async def test_review_workflow_nudge(make_review_input, x_activities, reply_activities):
    """Test Review workflow with nudge (redraft)."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_nudge"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            client_request_id = str(uuid.uuid4())
            new_draft = await handle.execute_update(
                "nudge",
                args=["Make it shorter", client_request_id]
            )
            
            assert len(new_draft) > 0
            
            # Approve
            client_request_id = str(uuid.uuid4())
            await handle.signal(
                "approve",
                args=["https://x.com/test/status/999", client_request_id]
            )
            
            await env.sleep(1)
            
            # Confirm posted
            client_request_id = str(uuid.uuid4())
            await handle.execute_update(
                "confirm_posted",
                args=["https://x.com/test/status/1001", client_request_id]
            )
            
            # Skip ahead for follow-up
            await env.sleep(timedelta(days=3, hours=1))
            
            result = await handle.result()
            assert result == "followup_due"


@pytest.mark.asyncio
async def test_review_workflow_timeout(make_review_input, x_activities, reply_activities):
    """Test Review workflow expires after 48h."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_timeout"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(timedelta(hours=49))
            
            result = await handle.result()
            assert result == "expired"


@pytest.mark.asyncio
async def test_review_workflow_low_confidence(make_review_input, x_activities, reply_activities):
    """Test Review workflow skips on low confidence."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input(confidence_threshold=0.95)
            workflow_id = f"review:test:{review_input.post.id}_low_conf"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            result = await handle.result()
            assert result == "skipped"


@pytest.mark.asyncio
async def test_review_workflow_approved_not_confirmed(make_review_input, x_activities, reply_activities):
    """Test Review workflow expires to approved_not_confirmed if no confirmation."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_not_confirmed"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            # Approve but don't confirm
            client_request_id = str(uuid.uuid4())
            await handle.signal(
                "approve",
                args=["https://x.com/test/status/999", client_request_id]
            )
            
            # Skip ahead 48h without confirming
            await env.sleep(timedelta(hours=49))
            
            result = await handle.result()
            assert result == "approved_not_confirmed"


@pytest.mark.asyncio
async def test_review_workflow_confirm_posted_invalid_url(make_review_input, x_activities, reply_activities):
    """Test confirm_posted rejects invalid URL."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_invalid_url"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            # Approve
            client_request_id = str(uuid.uuid4())
            await handle.signal(
                "approve",
                args=["https://x.com/test/status/999", client_request_id]
            )
            
            await env.sleep(1)
            
            # Try to confirm with invalid URL
            client_request_id = str(uuid.uuid4())
            from temporalio.client import WorkflowUpdateFailedError
            try:
                await handle.execute_update(
                    "confirm_posted",
                    args=["https://invalid-url.com/foo", client_request_id]
                )
                assert False, "Should have raised WorkflowUpdateFailedError"
            except WorkflowUpdateFailedError as e:
                # Validator should reject bad URL
                error_msg = str(e)
                if e.__cause__:
                    error_msg += " " + str(e.__cause__)
                assert "Invalid X post URL" in error_msg


@pytest.mark.asyncio
async def test_review_workflow_duplicate_approve_ignored(make_review_input, x_activities, reply_activities):
    """Test that duplicate approve requests with same request_id are ignored."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_dup_approve"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            # Approve with first request ID
            request_id = str(uuid.uuid4())
            await handle.signal(
                "approve",
                args=["https://x.com/test/status/999", request_id]
            )
            
            await env.sleep(1)
            
            # Try to approve again with same request ID (should be idempotent)
            await handle.signal(
                "approve",
                args=["https://x.com/test/status/999", request_id]
            )
            
            await env.sleep(1)
            
            # Query the card to verify it's approved
            card = await handle.query("get_card")
            assert card["status"] == "approved"


@pytest.mark.asyncio
async def test_review_workflow_claim_gate_regenerate_then_skip(make_review_input, x_activities, reply_activities):
    """Test claim gate failure triggers regenerate, then skip after max attempts."""
    from backend.activities.claim_gate_activity import ClaimGateInput, ClaimGateResult
    from temporalio import activity
    
    # Test-only claim gate activity that always fails
    fail_count = {"count": 0}
    
    @activity.defn(name="claim_gate")
    async def failing_claim_gate(input: ClaimGateInput) -> ClaimGateResult:
        """Claim gate that always fails for testing."""
        from backend.models.evidence import Claim
        
        fail_count["count"] += 1
        activity.logger.info(f"Claim gate attempt {fail_count['count']} - returning FAIL")
        
        test_claim = Claim(
            text="Test claim about our product",
            cited_passage_ids=[],
            quoted_spans=[]
        )
        
        return ClaimGateResult(
            passed=False,
            claims=[test_claim],
            failed_claims=[(test_claim, "No founder materials support this claim")],
            reasoning="No founder materials support this claim",
        )
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                reply_activities.research_post,
                reply_activities.draft_reply,
                failing_claim_gate,  # Use test-only failing claim gate
                store_card,
                update_card,
                x_activities.lookup_post,
                x_activities.check_followup,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_claim_fail"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            # Wait for workflow to complete (should skip after 2 failed attempts)
            result = await handle.result()
            assert result == "skipped"
            
            # Verify claim gate was called 2 times (max attempts)
            assert fail_count["count"] == 2
            
            # Query the card to verify final status
            card = await handle.query("get_card")
            assert card["status"] == "skipped"
            assert "claim gate failed" in card["skip_reason"].lower()
