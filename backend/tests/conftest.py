"""Pytest configuration and fixtures."""

import pytest
from datetime import datetime, timezone

from backend.models import Post, Author
from backend.activities.x_activities import XActivities
from backend.activities.reply_activities import ReplyActivities
from backend.adapters import FakeXClient, FakeReplyAgent


@pytest.fixture
def x_activities():
    """Create XActivities instance with FakeXClient."""
    return XActivities(FakeXClient())


@pytest.fixture
def reply_activities():
    """Create ReplyActivities instance with FakeReplyAgent."""
    return ReplyActivities(FakeReplyAgent())


@pytest.fixture
def sample_post():
    """Sample post for testing."""
    return Post(
        id="1001",
        text="Struggling with distributed systems reliability. How do you folks handle eventual consistency?",
        author=Author(
            id="author_001",
            username="alice_dev",
            name="Alice Developer",
            followers_count=5000,
            verified=False,
        ),
        created_at=datetime.now(timezone.utc).isoformat(),
        like_count=15,
        reply_count=3,
        repost_count=2,
        quote_count=0,
        conversation_id="1001",
        reply_settings="everyone",
    )


@pytest.fixture
def product_brief():
    """Sample product brief."""
    return """
# Product Brief

Our product helps developers build reliable distributed systems.

Key features:
- Automatic failover
- Strong consistency guarantees
- Real-time monitoring

We focus on reliability and ease of use.
    """.strip()
