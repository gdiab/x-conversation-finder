"""
Tests for status field serialization across Temporal boundary (R4-B1).

Verify that ReviewStatus enum is converted to plain string when crossing
the workflow/activity boundary to avoid temporalio serialization issues.
"""

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.workflows.review import ReviewWorkflow, ReviewInput, ReviewConfig
from backend.models import Post, Author
from backend.activities.store_activities import get_all_cards
from backend.config import Settings
from backend.database import init_db


@pytest.fixture
def temp_db_for_status(tmp_path):
    """Create a temporary database for status serialization test."""
    db_path = tmp_path / "status_test.db"
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
    )
    
    from unittest.mock import patch
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.database.get_settings", return_value=settings):
        
        # Clear engine cache
        import backend.database
        backend.database._engine = None
        backend.database._SessionLocal = None
        
        init_db()
        yield settings


@pytest.mark.asyncio
async def test_stored_status_is_plain_string_not_char_list(temp_db_for_status, make_review_input, x_activities, reply_activities):
    """
    R4-B1: Verify stored card status is 'pending' string, not ['p','e','n','d','i','n','g'] list.
    
    Tests the full path: workflow → activity → database → API read
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        from backend.activities.claim_gate_activity import claim_gate
        from backend.activities.store_activities import store_card, update_card
        
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
            post = Post(
                id="status_test_1",
                text="How do you test status serialization for reliable systems?",
                author=Author(
                    id="999",
                    username="test_status",
                    name="Status Test",
                    followers_count=1000,
                    verified=False
                ),
                created_at="2026-11-21T08:00:00Z",
                like_count=10,
                reply_count=5,
                repost_count=2,
                quote_count=1,
                conversation_id="status_test_1"
            )
            
            workflow_input = make_review_input(post=post)
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                workflow_input,
                id=f"review:test:status_test_1",
                task_queue="test",
            )
            
            # Wait for workflow to create card
            from datetime import timedelta
            await env.sleep(timedelta(seconds=1))
            
            # Read card from database
            cards = get_all_cards()
            assert len(cards) == 1, f"Expected 1 card, got {len(cards)}"
            
            card = cards[0]
            status = card["status"]
            
            # R4-B1: Must be plain string 'pending', not ['p','e','n','d','i','n','g']
            assert isinstance(status, str), f"Status must be str, got {type(status)}"
            assert status == "pending", f"Status must be 'pending', got {status!r}"
            assert not isinstance(status, list), "Status must not be a list"
            
            # Verify it's a simple string, not enum representation
            assert len(status) == 7, f"Status 'pending' should be 7 chars, got {len(status)}"
            assert status[0] == 'p', "First character should be 'p'"
            assert status[-1] == 'g', "Last character should be 'g'"
