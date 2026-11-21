"""
Replay tests for workflow determinism.

Uses recorded workflow histories to verify that current workflow code
produces the same commands when replayed.
"""

import pytest
from datetime import timedelta
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker, Replayer

from backend.workflows.review import ReviewWorkflow, ReviewInput
from backend.workflows.topic_watch import TopicWatchWorkflow, TopicWatchInput
from backend.activities import XActivities, ReplyActivities
from backend.activities import x_activity_funcs, reply_activity_funcs
from backend.activities.store_activities import store_card, update_card
from backend.activities.claim_gate_activity import claim_gate
from backend.adapters import FakeXClient, FakeReplyAgent


@pytest.mark.asyncio
async def test_review_workflow_replay(sample_post, product_brief):
    """Test Review workflow replay from recorded history."""
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
            workflow_id = f"review:test:{sample_post.id}_replay"
            
            # Run workflow and record history
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
            import uuid
            await handle.signal(
                "approve",
                args=["https://x.com/test/status/999", str(uuid.uuid4())]
            )
            
            await env.sleep(1)
            
            # Fetch history before workflow completes
            history = await handle.fetch_history()
            
            # Replay the history against current workflow code
            replayer = Replayer(
                workflows=[ReviewWorkflow],
            )
            await replayer.replay_workflow(history)


@pytest.mark.asyncio
async def test_topic_watch_workflow_replay(product_brief):
    """Test TopicWatch workflow replay from recorded history."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[TopicWatchWorkflow, ReviewWorkflow],
            activities=[
                x_activity_funcs.search_recent,
                x_activity_funcs.lookup_post,
                x_activity_funcs.check_followup,
                reply_activity_funcs.research_post,
                reply_activity_funcs.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            workflow_id = "watch:test:reliability_replay"
            
            # Run workflow and record history
            handle = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                TopicWatchInput(
                    account="test",
                    topic="reliability",
                    query="reliability",
                    product_brief=product_brief,
                    poll_interval_hours=1,
                    max_results=5,
                    confidence_threshold=0.5,
                    continue_as_new_after=100,
                ),
                id=workflow_id,
                task_queue="test",
            )
            
            # Let it run one iteration
            await env.sleep(timedelta(hours=2))
            
            # Stop it
            await handle.signal("stop")
            
            # Wait for completion
            await handle.result()
            
            # Fetch history
            history = await handle.fetch_history()
            
            # Replay the history against current workflow code
            replayer = Replayer(
                workflows=[TopicWatchWorkflow, ReviewWorkflow],
            )
            await replayer.replay_workflow(history)


@pytest.mark.asyncio
async def test_review_workflow_replay_with_skip(sample_post, product_brief):
    """Test Review workflow replay with skip signal."""
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
            workflow_id = f"review:test:{sample_post.id}_replay_skip"
            
            # Run workflow and record history
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
            
            # Skip
            import uuid
            await handle.signal(
                "skip",
                args=["Not relevant", str(uuid.uuid4())]
            )
            
            # Wait for completion
            result = await handle.result()
            assert result == "skipped"
            
            # Fetch history
            history = await handle.fetch_history()
            
            # Replay the history against current workflow code
            replayer = Replayer(
                workflows=[ReviewWorkflow],
            )
            await replayer.replay_workflow(history)
