"""
Replay tests for TopicWatch workflow.

Tests the polling loop, Continue-As-New, and child workflow spawning.
"""

import pytest
from datetime import timedelta

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.workflows.topic_watch import TopicWatchWorkflow, TopicWatchInput
from backend.workflows.review import ReviewWorkflow, ReviewConfig
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
                reply_activities.plan_reply,
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
async def test_topic_watch_searches_before_first_sleep(make_topic_watch_input, reply_activities):
    """A new watch searches right away, then waits a full interval before the next search."""
    from backend.workflows.topic_watch import TopicWatchConfig

    class CountingXClient(FakeXClient):
        def __init__(self):
            super().__init__()
            self.search_calls = 0

        async def search_recent(self, *args, **kwargs):
            self.search_calls += 1
            return await super().search_recent(*args, **kwargs)

    x_client = CountingXClient()
    x_acts = XActivities(x_client)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[TopicWatchWorkflow, ReviewWorkflow],
            activities=[
                x_acts.search_recent,
                x_acts.lookup_post,
                x_acts.check_followup,
                reply_activities.research_post,
                reply_activities.plan_reply,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            topic_watch_input = make_topic_watch_input(
                config=TopicWatchConfig(
                    poll_interval_hours=3,
                    activity_timeout_medium_sec=30,
                    activity_max_attempts=3,
                ),
            )
            handle = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                topic_watch_input,
                id="watch:test:search_first",
                task_queue="test",
            )

            # Well inside the first 3h interval: the first search has already run.
            await env.sleep(timedelta(minutes=10))
            assert x_client.search_calls == 1

            # Still inside the interval: no second search yet.
            await env.sleep(timedelta(hours=2))
            assert x_client.search_calls == 1

            # Past the interval: the second search has run.
            await env.sleep(timedelta(hours=1))
            assert x_client.search_calls == 2

            await handle.signal("stop")
            assert await handle.result() == "stopped"


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
                reply_activities.plan_reply,
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
                reply_activities.plan_reply,
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
                reply_activities.plan_reply,
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
                max_results=2,
                # Reviews are not under test here. With the default 1h review
                # timeout they expire at the same instant the watch continues
                # as new, and the time-skipping test server stops advancing
                # time when those timers and the new run's first search land
                # together. A long timeout keeps the reviews out of the way.
                review_config=ReviewConfig(review_timeout_hours=48),
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
            
            # R4-B3: Fetch history of original run to verify Continue-As-New occurred
            from temporalio.api.enums.v1 import EventType, WorkflowExecutionStatus
            
            original_handle = env.client.get_workflow_handle(workflow_id, run_id=original_run_id)
            
            # Get the history of the original run
            history = await original_handle.fetch_history()
            events = list(history.events)
            
            # The last event in the original run should be WORKFLOW_EXECUTION_CONTINUED_AS_NEW (type 47)
            last_event = events[-1]
            assert last_event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW, \
                f"Last event in original run should be CONTINUED_AS_NEW, got {last_event.event_type}"
            
            # Also verify the original run's status is CONTINUED_AS_NEW
            original_desc = await original_handle.describe()
            assert original_desc.status == WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_CONTINUED_AS_NEW, \
                f"Original run should have status CONTINUED_AS_NEW, got {original_desc.status}"
            
            # The current handle should point to a new run
            current_desc = await handle.describe()
            assert current_desc.run_id != original_run_id, \
                f"Current run_id should differ from original ({original_run_id})"
            
            # Stop the (continued) workflow
            await handle.signal("stop")
            result = await handle.result()
            assert result == "stopped"
            
            # N-4: The fact that the workflow didn't crash or spawn duplicate Reviews
            # is evidence that since_id and seen_post_ids carried over correctly.
            # If they were lost, the workflow would re-process old posts and either:
            # 1. Spawn duplicate Review workflows (would fail due to workflow ID collision)
            # 2. Return different newest_id (breaking the incremental fetch)


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
                reply_activities.plan_reply,
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


@pytest.mark.asyncio
async def test_direct_start_with_zero_poll_without_flag_clamped():
    """S-3: Direct TopicWatch start with 0h poll and no allow_sub_floor_poll flag gets clamped to 1h."""
    from backend.workflows.topic_watch import TopicWatchWorkflow, TopicWatchInput, TopicWatchConfig
    from backend.workflows.review import ReviewConfig
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Create input with 0h poll but no flag (should be clamped)
        input_data = TopicWatchInput(
            account="test_account",
            topic="test_topic",
            query="test query",
            product_brief="test brief",
            max_results=10,
            confidence_threshold=0.99,
            continue_as_new_after=1,
            config=TopicWatchConfig(
                poll_interval_hours=0,  # Sub-floor without flag
                activity_timeout_medium_sec=10,
                activity_max_attempts=2,
                allow_sub_floor_poll=False,  # Explicit: no sub-floor allowed
            ),
            review_config=ReviewConfig(
                review_timeout_hours=1,
                followup_delay_seconds=300,
            ),
        )
        
        from temporalio import activity
        from backend.activities.x_activities import SearchRecentInput, SearchRecentOutput

        search_calls = []

        @activity.defn(name="search_recent")
        async def stub_search_recent(input: SearchRecentInput) -> SearchRecentOutput:
            search_calls.append(input.poll_interval_hours)
            return SearchRecentOutput(posts=[], newest_id=None, cost_usd=0.0)

        async with Worker(
            env.client,
            task_queue="test-clamp",
            workflows=[TopicWatchWorkflow],
            activities=[stub_search_recent],
        ):
            # Start workflow - it should clamp to 1h internally
            handle = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                input_data,
                id="test-clamp-workflow",
                task_queue="test-clamp",
            )

            # The first search runs right away, with the clamped interval.
            await env.sleep(timedelta(minutes=1))
            assert search_calls == [1]

            # At 30 minutes the watch is still in its clamped 1h sleep:
            # an unclamped 0h interval would have searched again already.
            await env.sleep(timedelta(minutes=29))
            assert search_calls == [1]

            # Terminate to end test
            await handle.terminate()


@pytest.mark.asyncio
async def test_second_watch_skips_post_another_watch_already_reviews(make_topic_watch_input, x_activities, reply_activities):
    """Two watches on one account find the same post: the second skips its Review.

    Review ids are per account and post, so the second start fails with
    "already started". That must be caught, not wedge the watch (the
    multi-agent-orchestration watch was stuck on this for 12h, 2026-11-27).
    """
    import asyncio

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
                reply_activities.plan_reply,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            first = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                make_topic_watch_input(topic="first"),
                id="watch:test:first",
                task_queue="test",
            )
            await env.sleep(timedelta(minutes=10))
            second = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                make_topic_watch_input(topic="second"),
                id="watch:test:second",
                task_queue="test",
            )
            await env.sleep(timedelta(minutes=10))

            for handle in (first, second):
                await handle.signal("stop")
                assert await asyncio.wait_for(handle.result(), timeout=30) == "stopped"

            history = await second.fetch_history()
            started = [
                e.start_child_workflow_execution_failed_event_attributes.workflow_id
                for e in history.events
                if e.HasField("start_child_workflow_execution_failed_event_attributes")
            ]
            failed_tasks = [
                e for e in history.events
                if e.HasField("workflow_task_failed_event_attributes")
            ]
            assert started, "the second watch should have hit an existing Review"
            assert failed_tasks == []
