"""Pytest configuration and fixtures."""

import os
import pytest
from datetime import datetime, timezone
from pathlib import Path

from backend.models import Post, Author
from backend.activities.x_activities import XActivities
from backend.activities.reply_activities import ReplyActivities
from backend.adapters import FakeXClient, FakeReplyAgent
from backend.database import init_db, get_db
from backend.config import get_settings


@pytest.fixture(scope="function", autouse=True)
def reset_db_engine():
    """
    QA R16: Universal DB engine reset for ALL tests.
    
    Resets backend.database._engine and _SessionLocal before and after EVERY test,
    including tests that use temp_db fixture. This prevents engine/session staleness
    and kill_switch leakage between tests.
    
    Runs before and after each test via autouse=True.
    """
    import backend.database
    
    # Reset before test
    if backend.database._engine is not None:
        backend.database._engine.dispose()
    backend.database._engine = None
    backend.database._SessionLocal = None
    
    yield
    
    # Reset after test
    if backend.database._engine is not None:
        backend.database._engine.dispose()
    backend.database._engine = None
    backend.database._SessionLocal = None


@pytest.fixture(scope="function", autouse=True)
def test_db_isolation(tmp_path, monkeypatch, request):
    """Test isolation: each test gets a fresh tmp DB with kill switch off.
    
    QA R16: No longer skips temp_db tests - reset_db_engine handles all cleanup.
    """
    # Skip if test has its own temp_db fixture (it will handle DB setup)
    if 'temp_db' in request.fixturenames:
        yield
        return
    
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    
    # Point config at tmp DB
    monkeypatch.setenv("DATABASE_URL", db_url)
    
    # Initialize fresh DB (reset_db_engine already cleared the engine)
    init_db()
    
    # Ensure kill switch is off (per S-2: global setting, not per-day)
    from backend.budget import set_kill_switch
    set_kill_switch(False)
    
    yield db_path


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
def make_review_input(sample_post, product_brief):
    """Factory fixture for creating ReviewInput with test config."""
    from backend.workflows.review import ReviewInput, ReviewConfig
    
    def _make(**overrides):
        defaults = {
            'post': sample_post,
            'topic': 'reliability',
            'product_brief': product_brief,
            'account': 'test',
            'confidence_threshold': 0.5,
            'config': ReviewConfig(
                activity_timeout_short_sec=10,
                activity_timeout_medium_sec=30,
                activity_timeout_long_sec=60,
                claim_gate_max_attempts=2,
                followup_delay_days=1,  # Short for tests
                activity_max_attempts=3,
                review_timeout_hours=1,  # Short for tests
            )
        }
        defaults.update(overrides)
        return ReviewInput(**defaults)
    
    return _make


@pytest.fixture
def make_topic_watch_input(product_brief):
    """Factory fixture for creating TopicWatchInput with test config."""
    from backend.workflows.topic_watch import TopicWatchInput, TopicWatchConfig
    from backend.workflows.review import ReviewConfig
    
    def _make(**overrides):
        defaults = {
            'account': 'test',
            'topic': 'reliability',
            'query': 'reliability',
            'product_brief': product_brief,
            'max_results': 5,
            'confidence_threshold': 0.5,
            'continue_as_new_after': 100,
            'config': TopicWatchConfig(
                poll_interval_hours=1,  # Short for tests
                activity_timeout_medium_sec=30,
                activity_max_attempts=3,
            ),
            'review_config': ReviewConfig(
                activity_timeout_short_sec=10,
                activity_timeout_medium_sec=30,
                activity_timeout_long_sec=60,
                claim_gate_max_attempts=2,
                followup_delay_days=1,
                activity_max_attempts=3,
                review_timeout_hours=1,
            )
        }
        defaults.update(overrides)
        return TopicWatchInput(**defaults)
    
    return _make


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
