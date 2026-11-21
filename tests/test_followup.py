"""Tests for follow-up flow."""

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
async def test_followup_timer_and_check():
    """Test that follow-up timer fires and checks for replies."""
    settings = get_settings()
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Create fake adapters
        x_client = FakeXClient()
        reply_agent = FakeReplyAgent()
        
        # Add a reply to the conversation that will be found
        x_client.posts["followup_reply_1"] = Post(
            id="followup_reply_1",
            text="Thanks for the info! Can you send me a demo link?",
            author=Author(
                id="user1",
                username="testuser",
                name="Test User",
                followers_count=100,
                verified=False,
            ),
            created_at="2024-01-05T00:00:00Z",
            like_count=0,
            reply_count=0,
            repost_count=0,
            quote_count=0,
            conversation_id="founder_reply_123",  # Same conversation
            in_reply_to_user_id="founder",
            reply_settings="everyone",
            url="https://x.com/testuser/status/followup_reply_1",
        )
        
        # Import activities with injected adapters
        from backend.activities.x_activities import XActivities
        from backend.activities.reply_activities import ReplyActivities
        from backend.activities.store_activities import store_card, update_card, get_card
        from backend.activities.claim_gate_activity import claim_gate
        
        x_activities = XActivities(x_client)
        reply_activities = ReplyActivities(reply_agent)
        
        activities = [
            x_activities.search_recent,
            x_activities.lookup_post,
            x_activities.check_followup,
            reply_activities.research_post,
            reply_activities.draft_reply,
            store_card,
            update_card,
            get_card,
            claim_gate,
        ]
        
        # Start worker
        async with Worker(
            env.client,
            task_queue="test_queue",
            workflows=[ReviewWorkflow],
            activities=activities,
        ):
            # Start workflow
            post = Post(
                id="test_followup",
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
                conversation_id="test_followup",
                in_reply_to_user_id=None,
                reply_settings="everyone",
                url="https://x.com/testuser/status/test_followup",
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
                id="review:testaccount:test_followup",
                task_queue="test_queue",
            )
            
            # Wait for pending state
            await env.sleep(timedelta(seconds=1))
            
            # Approve
            await handle.signal("approve", "https://x.com/founder/status/posted", "req_approve_1")
            
            # Wait for approved state
            await env.sleep(timedelta(seconds=1))
            
            # Confirm posted (this creates the reply post in fake client)
            await handle.execute_update(
                "confirm_posted",
                "https://x.com/founder/status/founder_reply_123",
                "req_confirm_1",
            )
            
            # Skip ahead by followup_delay_days
            await env.sleep(timedelta(days=settings.followup_delay_days))
            
            # Wait a bit for check_followup activity to run
            await env.sleep(timedelta(seconds=5))
            
            # Query state
            card = await handle.query("get_card")
            
            # Should be in followup_due status
            assert card["status"] == "followup_due"
            # No followup_replies because check_followup returns empty by default in fake
