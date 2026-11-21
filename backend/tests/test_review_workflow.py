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
from backend.activities import x_activity_funcs, reply_activity_funcs
from backend.activities.store_activities import store_card, update_card
from backend.activities.claim_gate_activity import claim_gate
from backend.adapters import FakeXClient, FakeReplyAgent


@pytest.mark.asyncio
async def test_review_workflow_approve(sample_post, product_brief):
    """Test Review workflow with approval."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                reply_activity_funcs.research_post,
                reply_activity_funcs.draft_reply,
                claim_gate,
                store_card,
                update_card,
                x_activity_funcs.lookup_post,
                x_activity_funcs.check_followup,
            ],
        ):
            workflow_id = f"review:test:{sample_post.id}"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                ReviewInput(
                    post=sample_post,
                    topic="reliability",
                    product_brief=product_brief,
                    account="test",
                    confidence_threshold=0.5,
                ),
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            card = await handle.query("get_card")
            assert card["status"] == "pending"
            assert card["evidence"]["post_id"] == sample_post.id
            
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
async def test_review_workflow_skip(sample_post, product_brief):
    """Test Review workflow with skip."""
    x_activities = XActivities(FakeXClient())
    reply_activities = ReplyActivities(FakeReplyAgent())
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                reply_activity_funcs.research_post,
                reply_activity_funcs.draft_reply,
                claim_gate,
                store_card,
                update_card,
                x_activity_funcs.lookup_post,
                x_activity_funcs.check_followup,
            ],
        ):
            workflow_id = f"review:test:{sample_post.id}_skip"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                ReviewInput(
                    post=sample_post,
                    topic="reliability",
                    product_brief=product_brief,
                    account="test",
                    confidence_threshold=0.5,
                ),
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
async def test_review_workflow_nudge(sample_post, product_brief):
    """Test Review workflow with nudge (redraft)."""
    x_activities = XActivities(FakeXClient())
    reply_activities = ReplyActivities(FakeReplyAgent())
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                reply_activity_funcs.research_post,
                reply_activity_funcs.draft_reply,
                claim_gate,
                store_card,
                update_card,
                x_activity_funcs.lookup_post,
                x_activity_funcs.check_followup,
            ],
        ):
            workflow_id = f"review:test:{sample_post.id}_nudge"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                ReviewInput(
                    post=sample_post,
                    topic="reliability",
                    product_brief=product_brief,
                    account="test",
                    confidence_threshold=0.5,
                ),
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
async def test_review_workflow_timeout(sample_post, product_brief):
    """Test Review workflow expires after 48h."""
    x_activities = XActivities(FakeXClient())
    reply_activities = ReplyActivities(FakeReplyAgent())
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                reply_activity_funcs.research_post,
                reply_activity_funcs.draft_reply,
                claim_gate,
                store_card,
                update_card,
                x_activity_funcs.lookup_post,
                x_activity_funcs.check_followup,
            ],
        ):
            workflow_id = f"review:test:{sample_post.id}_timeout"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                ReviewInput(
                    post=sample_post,
                    topic="reliability",
                    product_brief=product_brief,
                    account="test",
                    confidence_threshold=0.5,
                ),
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(timedelta(hours=49))
            
            result = await handle.result()
            assert result == "expired"


@pytest.mark.asyncio
async def test_review_workflow_low_confidence(sample_post, product_brief):
    """Test Review workflow skips on low confidence."""
    x_activities = XActivities(FakeXClient())
    reply_activities = ReplyActivities(FakeReplyAgent())
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                reply_activity_funcs.research_post,
                reply_activity_funcs.draft_reply,
                claim_gate,
                store_card,
                update_card,
                x_activity_funcs.lookup_post,
                x_activity_funcs.check_followup,
            ],
        ):
            workflow_id = f"review:test:{sample_post.id}_low_conf"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                ReviewInput(
                    post=sample_post,
                    topic="reliability",
                    product_brief=product_brief,
                    account="test",
                    confidence_threshold=0.95,
                ),
                id=workflow_id,
                task_queue="test",
            )
            
            result = await handle.result()
            assert result == "skipped"


@pytest.mark.asyncio
async def test_review_workflow_approved_not_confirmed(sample_post, product_brief):
    """Test Review workflow expires to approved_not_confirmed if no confirmation."""
    x_activities = XActivities(FakeXClient())
    reply_activities = ReplyActivities(FakeReplyAgent())
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                reply_activity_funcs.research_post,
                reply_activity_funcs.draft_reply,
                claim_gate,
                store_card,
                update_card,
                x_activity_funcs.lookup_post,
                x_activity_funcs.check_followup,
            ],
        ):
            workflow_id = f"review:test:{sample_post.id}_not_confirmed"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                ReviewInput(
                    post=sample_post,
                    topic="reliability",
                    product_brief=product_brief,
                    account="test",
                    confidence_threshold=0.5,
                ),
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
async def test_review_workflow_confirm_posted_invalid_url(sample_post, product_brief):
    """Test confirm_posted rejects invalid URL."""
    x_activities = XActivities(FakeXClient())
    reply_activities = ReplyActivities(FakeReplyAgent())
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                reply_activity_funcs.research_post,
                reply_activity_funcs.draft_reply,
                claim_gate,
                store_card,
                update_card,
                x_activity_funcs.lookup_post,
                x_activity_funcs.check_followup,
            ],
        ):
            workflow_id = f"review:test:{sample_post.id}_invalid_url"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                ReviewInput(
                    post=sample_post,
                    topic="reliability",
                    product_brief=product_brief,
                    account="test",
                    confidence_threshold=0.5,
                ),
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
            try:
                await handle.execute_update(
                    "confirm_posted",
                    args=["https://invalid-url.com/foo", client_request_id]
                )
                assert False, "Should have raised ValueError"
            except Exception as e:
                assert "Invalid X post URL" in str(e)
