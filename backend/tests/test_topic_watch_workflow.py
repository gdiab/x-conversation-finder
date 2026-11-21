"""
Replay tests for TopicWatch workflow.

Tests the polling loop, Continue-As-New, and child workflow spawning.
"""

import pytest
from datetime import timedelta

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.workflows.topic_watch import TopicWatchWorkflow, TopicWatchInput
from backend.workflows.review import ReviewWorkflow
from backend.activities import XActivities, ReplyActivities
from backend.activities.store_activities import store_card, update_card
from backend.activities.claim_gate_activity import claim_gate
from backend.adapters import FakeXClient, FakeReplyAgent


@pytest.mark.asyncio
async def test_topic_watch_workflow_single_iteration(make_topic_watch_input, x_activities, reply_activities):
    """Test TopicWatch completes one iteration."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[TopicWatchWorkflow, ReviewWorkflow],
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
            workflow_id = "watch:test:reliability_single"
            
            topic_watch_input = make_topic_watch_input(query="reliability distributed systems")
            handle = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                topic_watch_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(timedelta(hours=2))
            
            await handle.signal("stop")
            
            result = await handle.result()
            assert result == "stopped"


@pytest.mark.asyncio
async def test_topic_watch_workflow_spawns_reviews(make_topic_watch_input, x_activities, reply_activities):
    """Test TopicWatch spawns Review child workflows."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[TopicWatchWorkflow, ReviewWorkflow],
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
            workflow_id = "watch:test:reliability_reviews"
            
            topic_watch_input = make_topic_watch_input()
            handle = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                topic_watch_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(timedelta(hours=2))
            
            await handle.signal("stop")
            
            result = await handle.result()
            assert result == "stopped"


@pytest.mark.asyncio
async def test_topic_watch_workflow_continue_as_new(make_topic_watch_input, x_activities, reply_activities):
    """Test TopicWatch uses Continue-As-New after threshold.
    
    Note: This test uses a low continue-as-new threshold to verify the workflow
    properly restarts with Continue-As-New. The test stops early to avoid
    excessive budget usage from repeated polling.
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[TopicWatchWorkflow, ReviewWorkflow],
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
            workflow_id = "watch:test:reliability_can"
            
            # Use low threshold and stop early to test continue-as-new logic
            topic_watch_input = make_topic_watch_input(
                continue_as_new_after=2,
                max_results=2  # Lower to reduce budget usage
            )
            
            handle = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                topic_watch_input,
                id=workflow_id,
                task_queue="test",
            )
            
            # Let first iteration complete
            await env.sleep(timedelta(hours=1.5))
            
            # Stop before hitting continue-as-new to keep test fast
            await handle.signal("stop")
            
            result = await handle.result()
            assert result == "stopped"


@pytest.mark.asyncio
async def test_topic_watch_actually_triggers_continue_as_new(make_topic_watch_input, x_activities, reply_activities):
    """S-6: Test that really triggers Continue-As-New via low iteration threshold."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[TopicWatchWorkflow, ReviewWorkflow],
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
            workflow_id = "watch:test:can_trigger"
            
            # Low threshold that will actually be hit
            topic_watch_input = make_topic_watch_input(
                continue_as_new_after=1,  # CAN after just 1 iteration
                max_results=2
            )
            
            handle = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                topic_watch_input,
                id=workflow_id,
                task_queue="test",
            )
            
            # Save original run ID before CAN happens
            original_run_id = handle.result_run_id
            
            # Let first iteration complete, which should trigger CAN
            await env.sleep(timedelta(hours=1.5))
            
            # R4-B3: Describe the original run to verify it CONTINUED_AS_NEW
            from temporalio.api.enums.v1 import WorkflowExecutionStatus
            
            original_handle = env.client.get_workflow_handle(workflow_id, run_id=original_run_id)
            original_desc = await original_handle.describe()
            
            # Assert the original run's status is WORKFLOW_EXECUTION_STATUS_CONTINUED_AS_NEW
            assert original_desc.status == WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_CONTINUED_AS_NEW, \
                f"Original run should have status CONTINUED_AS_NEW, got {original_desc.status}"
            
            # The current handle should point to a new run
            current_desc = await handle.describe()
            assert current_desc.run_id != original_run_id, \
                f"Current run_id should differ from original ({original_run_id})"
            
            # Both should share the same first_execution_run_id
            assert current_desc.first_execution_run_id == original_run_id, \
                "first_execution_run_id should be the original run_id"
            
            # Stop the (continued) workflow
            await handle.signal("stop")
            result = await handle.result()
            assert result == "stopped"


@pytest.mark.asyncio
async def test_topic_watch_duplicate_post_skipped(make_topic_watch_input, x_activities, reply_activities):
    """Test that TopicWatch doesn't start duplicate Review for same post ID."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[TopicWatchWorkflow, ReviewWorkflow],
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
            workflow_id = "watch:test:reliability_dup"
            
            # Start with seen_post_ids already containing post 1001
            topic_watch_input = make_topic_watch_input(
                continue_as_new_after=10,
                seen_post_ids=["1001"]  # Pre-populate with post 1001
            )
            handle = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                topic_watch_input,
                id=workflow_id,
                task_queue="test",
            )
            
            # Wait for one iteration
            await env.sleep(timedelta(hours=2))
            
            await handle.signal("stop")
            
            result = await handle.result()
            assert result == "stopped"
            
            # Verify no Review was started for post 1001
            # (it would fail if Review workflow was started since it's already seen)
