"""Tests for signal/update handler idempotency."""

import pytest
from datetime import timedelta

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.workflows.review import ReviewWorkflow, ReviewInput
from backend.models import Post, Author
from backend.config import get_settings
from backend.adapters.fake_x_client import FakeXClient
from backend.adapters.fake_reply_agent import FakeReplyAgent


@pytest.mark.asyncio
async def test_duplicate_approve_signal():
    """Test that duplicate approve signals are ignored."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Create fake adapters
        x_client = FakeXClient()
        reply_agent = FakeReplyAgent()
        
        # Import activities with injected adapters
        from backend.activities import get_activities
        activities = get_activities(x_client, reply_agent)
        
        # Start worker
        async with Worker(
            env.client,
            task_queue="test_queue",
            workflows=[ReviewWorkflow],
            activities=activities,
        ):
            # Start workflow
            post = Post(
                id="test123",
                text="Looking for AI content tools",
                author=Author(
                    id="user1",
                    username="testuser",
                    name="Test User",
                    followers_count=100,
                    verified=False,
                ),
                created_at="2024-01-01T00:00:00Z",
                like_count=10,
                reply_count=5,
                repost_count=2,
                quote_count=1,
                conversation_id="test123",
                in_reply_to_user_id=None,
                reply_settings="everyone",
                url="https://x.com/testuser/status/test123",
            )
            
            review_input = ReviewInput(
                post=post,
                topic="AI tools",
                product_brief="We build AI content generation tools.",
                account="testaccount",
                confidence_threshold=0.6,
            )
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id="review:testaccount:test123",
                task_queue="test_queue",
            )
            
            # Wait for pending state
            await env.sleep(timedelta(seconds=1))
            
            # Send approve signal twice with same request ID
            await handle.signal("approve", "https://x.com/founder/status/posted", "req_123")
            await handle.signal("approve", "https://x.com/founder/status/posted", "req_123")
            
            # Wait a bit
            await env.sleep(timedelta(seconds=1))
            
            # Query state
            card = await handle.query("get_card")
            
            # Should be approved only once
            assert card["status"] == "approved"
            assert card["approved"] is True


@pytest.mark.asyncio
async def test_duplicate_edit_update():
    """Test that duplicate edit updates are ignored."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Create fake adapters
        x_client = FakeXClient()
        reply_agent = FakeReplyAgent()
        
        # Import activities with injected adapters
        from backend.activities import get_activities
        activities = get_activities(x_client, reply_agent)
        
        # Start worker
        async with Worker(
            env.client,
            task_queue="test_queue",
            workflows=[ReviewWorkflow],
            activities=activities,
        ):
            # Start workflow
            post = Post(
                id="test456",
                text="Looking for AI content tools",
                author=Author(
                    id="user1",
                    username="testuser",
                    name="Test User",
                    followers_count=100,
                    verified=False,
                ),
                created_at="2024-01-01T00:00:00Z",
                like_count=10,
                reply_count=5,
                repost_count=2,
                quote_count=1,
                conversation_id="test456",
                in_reply_to_user_id=None,
                reply_settings="everyone",
                url="https://x.com/testuser/status/test456",
            )
            
            review_input = ReviewInput(
                post=post,
                topic="AI tools",
                product_brief="We build AI content generation tools.",
                account="testaccount",
                confidence_threshold=0.6,
            )
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id="review:testaccount:test456",
                task_queue="test_queue",
            )
            
            # Wait for pending state
            await env.sleep(timedelta(seconds=1))
            
            # Send edit update twice with same request ID
            result1 = await handle.execute_update("edit", "New draft text v1", "req_edit_123")
            result2 = await handle.execute_update("edit", "New draft text v2", "req_edit_123")
            
            # Second edit should be ignored (returns first result)
            assert result1 == result2
            
            # Query state
            card = await handle.query("get_card")
            
            # Should only have first edit applied
            assert card["evidence"]["draft"]["text"] == "New draft text v1"
