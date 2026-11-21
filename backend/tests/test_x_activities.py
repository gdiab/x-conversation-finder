
"""Tests for X activities."""

import pytest


@pytest.mark.asyncio
async def test_search_recent_http_adapter_rejects_sub_floor_poll():
    """Item 3: HTTP adapter rejects sub-1h poll interval with non-retryable error."""
    from backend.activities.x_activities import XActivities, SearchRecentInput
    from backend.adapters.http_x_client import HttpXClient
    from temporalio.exceptions import ApplicationError
    
    # Create HTTP client (will fail if no bearer token, but validation happens first)
    http_client = HttpXClient("test_bearer_token")
    x_activities = XActivities(http_client)
    
    # Try to search with sub-1h poll interval
    input_data = SearchRecentInput(
        query="test",
        max_results=10,
        idempotency_key="test_sub_floor",
        poll_interval_hours=0,  # Sub-floor
    )
    
    # Should raise non-retryable ApplicationError
    with pytest.raises(ApplicationError) as exc_info:
        await x_activities.search_recent(input_data)
    
    assert exc_info.value.non_retryable
    assert "HTTP X adapter requires poll interval >= 1h" in str(exc_info.value)


@pytest.mark.asyncio
async def test_search_recent_negative_poll_interval_rejected():
    """Item 3: Negative poll interval rejected regardless of adapter."""
    from backend.activities.x_activities import XActivities, SearchRecentInput
    from backend.adapters.fake_x_client import FakeXClient
    from temporalio.exceptions import ApplicationError
    
    fake_client = FakeXClient()
    x_activities = XActivities(fake_client)
    
    # Try to search with negative poll interval
    input_data = SearchRecentInput(
        query="test",
        max_results=10,
        idempotency_key="test_negative",
        poll_interval_hours=-1,  # Negative
    )
    
    # Should raise non-retryable ApplicationError
    with pytest.raises(ApplicationError) as exc_info:
        await x_activities.search_recent(input_data)
    
    assert exc_info.value.non_retryable
    assert "Poll interval cannot be negative" in str(exc_info.value)


@pytest.mark.asyncio
async def test_search_recent_fake_adapter_allows_zero_poll():
    """Item 3: Fake adapter allows 0h poll interval (validation passes)."""
    from backend.activities.x_activities import XActivities, SearchRecentInput
    from backend.adapters.fake_x_client import FakeXClient
    from temporalio.exceptions import ApplicationError
    
    fake_client = FakeXClient()
    x_activities = XActivities(fake_client)
    
    # Search with 0h poll interval (allowed with fake)
    input_data = SearchRecentInput(
        query="reliability",
        max_results=10,
        idempotency_key="test_zero_poll",
        poll_interval_hours=0,  # Allowed with fake
    )
    
    # Validation should pass (won't execute full activity without proper context)
    # The validation logic checks adapter type before activity.info() is called
    try:
        # This will fail on activity.info() but we're testing validation passes first
        await x_activities.search_recent(input_data)
    except RuntimeError as e:
        # Expected: not in activity context (means validation passed)
        assert "Not in activity context" in str(e)
