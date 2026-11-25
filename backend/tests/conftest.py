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


# The synthetic post of sample_post (tests compare stored text with it)
SAMPLE_POST_TEXT = "Struggling with distributed systems reliability. How do you folks handle eventual consistency?"


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
    
    # Point config at tmp DB. get_settings() is cached, so clear it too, or
    # every test after the first would share the first test's DB (issue #9:
    # post-level removal records made that sharing visible).
    monkeypatch.setenv("DATABASE_URL", db_url)
    get_settings.cache_clear()
    
    # Initialize fresh DB (reset_db_engine already cleared the engine)
    init_db()
    
    # Ensure kill switch is off (per S-2: global setting, not per-day)
    from backend.budget import set_kill_switch
    set_kill_switch(False)
    
    yield db_path
    get_settings.cache_clear()


@pytest.fixture(scope="function", autouse=True)
def default_draft_mode(monkeypatch):
    """Tests run with DRAFT_MODE=assembled unless they ask for written mode
    (issue #12), whatever the developer's .env says: an environment variable
    wins over the .env file. Tests of written mode pass draft_mode="written"
    to ReplyActivities."""
    monkeypatch.setenv("DRAFT_MODE", "assembled")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def founder_text_by_reference(monkeypatch):
    """Edits and nudges in tests reach a Review as the API sends them.

    issue #23: the API stores an edit's text or a nudge's instruction and
    sends a text-free Review only a reference to it, then answers with the
    stored draft (backend.api.send_founder_text). Tests call
    handle.execute_update("edit"/"nudge", args=[text, request_id]) directly;
    this wrapper does the same translation for them, so a test reads as
    before and the Review gets exactly what the API sends. Updates sent with
    a third argument, and Reviews whose input carries the post, are passed
    through unchanged.
    """
    from temporalio.client import WorkflowHandle

    original_execute = WorkflowHandle.execute_update
    original_start = WorkflowHandle.start_update

    async def translate(handle, update, args):
        name = update if isinstance(update, str) else getattr(update, "__name__", "")
        args = list(args or [])
        if name not in ("edit", "nudge") or len(args) != 2:
            return args, False
        state = await handle.query("get_card")
        if not state.get("text_free"):
            return args, False
        from backend import review_docs

        text, request_id = args
        ref = None
        if text and text.strip():
            slot = review_docs.store_founder_text(handle.id, state["post_id"], name, request_id, text)
            ref = {"slot": slot, "sha256": review_docs.text_digest(text)}
        return ["", request_id, ref], True

    async def execute_update(self, update, arg=None, *, args=(), **kwargs):
        if arg is not None:
            args = [arg]
        args, text_free = await translate(self, update, args)
        result = await original_execute(self, update, args=args, **kwargs)
        if text_free:
            from backend.api import stored_draft_text

            return stored_draft_text(self.id)
        return result

    async def start_update(self, update, arg=None, *, args=(), **kwargs):
        if arg is not None:
            args = [arg]
        args, text_free = await translate(self, update, args)
        update_handle = await original_start(self, update, args=args, **kwargs)
        if text_free:
            from backend.api import stored_draft_text

            digest_result = update_handle.result

            async def result(*a, **k):
                await digest_result(*a, **k)
                return stored_draft_text(self.id)

            update_handle.result = result
        return update_handle

    monkeypatch.setattr(WorkflowHandle, "execute_update", execute_update)
    monkeypatch.setattr(WorkflowHandle, "start_update", start_update)


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
        text=SAMPLE_POST_TEXT,
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
    
    def _make(legacy: bool = False, **overrides):
        """issue #23: by default the post is stored and the Review is given
        only its id, as a TopicWatch starts it. legacy=True gives the post
        in the input, as Reviews recorded before issue #23 had it."""
        post = overrides.pop('post', sample_post)
        if not legacy:
            from backend.review_docs import store_posts
            store_posts([post])
        defaults = {
            'post': post if legacy else None,
            'post_id': post.id,
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

Our product helps developers build reliable distributed systems. Our product provides automatic failover. Our product guarantees strong consistency.

Our platform is designed for reliability.

Key features:
- Automatic failover
- Strong consistency guarantees
- Real-time monitoring

We focus on reliability and ease of use.
    """.strip()
