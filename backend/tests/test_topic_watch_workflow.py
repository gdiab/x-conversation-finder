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
from backend.activities import x_activity_funcs, reply_activity_funcs
from backend.activities.store_activities import store_card, update_card
from backend.activities.claim_gate_activity import claim_gate
from backend.adapters import FakeXClient, FakeReplyAgent


@pytest.mark.asyncio
async def test_topic_watch_workflow_single_iteration(product_brief):
    """Test TopicWatch completes one iteration."""
    x_activities = XActivities(FakeXClient())
    reply_activities = ReplyActivities(FakeReplyAgent())
    
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
            workflow_id = "watch:test:reliability_single"
            
            handle = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                TopicWatchInput(
                    account="test",
                    topic="reliability",
                    query="reliability distributed systems",
                    product_brief=product_brief,
                    poll_interval_hours=1,
                    max_results=5,
                    confidence_threshold=0.5,
                    continue_as_new_after=100,
                ),
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(timedelta(hours=2))
            
            await handle.signal("stop")
            
            result = await handle.result()
            assert result == "stopped"


@pytest.mark.asyncio
async def test_topic_watch_workflow_spawns_reviews(product_brief):
    """Test TopicWatch spawns Review child workflows."""
    x_activities = XActivities(FakeXClient())
    reply_activities = ReplyActivities(FakeReplyAgent())
    
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
            workflow_id = "watch:test:reliability_reviews"
            
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
            
            await env.sleep(timedelta(hours=2))
            
            await handle.signal("stop")
            
            result = await handle.result()
            assert result == "stopped"


@pytest.mark.asyncio
async def test_topic_watch_workflow_continue_as_new(product_brief):
    """Test TopicWatch uses Continue-As-New after threshold."""
    x_activities = XActivities(FakeXClient())
    reply_activities = ReplyActivities(FakeReplyAgent())
    
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
            workflow_id = "watch:test:reliability_can"
            
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
                    continue_as_new_after=2,
                ),
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(timedelta(hours=3))
            
            await handle.signal("stop")
            
            result = await handle.result()
            assert result == "stopped"
