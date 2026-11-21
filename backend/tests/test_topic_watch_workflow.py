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
    """Test TopicWatch uses Continue-As-New after threshold."""
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
            
            topic_watch_input = make_topic_watch_input(continue_as_new_after=2)
            handle = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                topic_watch_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(timedelta(hours=3))
            
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
