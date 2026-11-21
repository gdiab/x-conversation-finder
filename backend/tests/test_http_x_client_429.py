"""
Test for HTTP 429 rate limit handling (R5-S2).

Verifies that 429 responses raise a typed retryable ApplicationError
with next_retry_delay from Retry-After header.
"""

import pytest
import httpx
from unittest.mock import AsyncMock, patch
from temporalio.exceptions import ApplicationError
from backend.adapters.http_x_client import HttpXClient


@pytest.mark.asyncio
async def test_429_raises_typed_retryable_error_with_retry_after():
    """Test that 429 response raises ApplicationError with Retry-After delay."""
    client = HttpXClient(bearer_token="test_token")
    
    # Mock a 429 response with Retry-After header
    mock_response = httpx.Response(
        status_code=429,
        headers={"Retry-After": "120"},  # 120 seconds
        json={"title": "Too Many Requests", "detail": "Rate limit exceeded"},
    )
    
    with patch.object(client._client, 'get', new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        
        # Attempt a search_recent call
        with pytest.raises(ApplicationError) as exc_info:
            await client.search_recent("test query", max_results=10)
        
        # Verify error properties
        error = exc_info.value
        assert error.type == "RateLimitExceeded"
        assert error.non_retryable is False  # Should be retryable
        assert error.next_retry_delay is not None
        # Retry-After of 120 seconds
        assert error.next_retry_delay.total_seconds() == pytest.approx(120.0)
        assert "rate limit" in str(error).lower()


@pytest.mark.asyncio
async def test_429_lookup_post_raises_typed_retryable_error():
    """Test that 429 on lookup_post also raises ApplicationError properly."""
    client = HttpXClient(bearer_token="test_token")
    
    # Mock a 429 response with Retry-After header
    mock_response = httpx.Response(
        status_code=429,
        headers={"Retry-After": "60"},  # 60 seconds
        json={"title": "Too Many Requests"},
    )
    
    with patch.object(client._client, 'get', new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        
        # Attempt a lookup_post call
        with pytest.raises(ApplicationError) as exc_info:
            await client.lookup_post("1234567890")
        
        # Verify error properties
        error = exc_info.value
        assert error.type == "RateLimitExceeded"
        assert error.non_retryable is False
        assert error.next_retry_delay is not None
        assert error.next_retry_delay.total_seconds() == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_429_without_retry_after_uses_default():
    """Test that 429 without Retry-After header uses default delay."""
    client = HttpXClient(bearer_token="test_token")
    
    # Mock a 429 response WITHOUT Retry-After header
    mock_response = httpx.Response(
        status_code=429,
        headers={},  # No Retry-After
        json={"title": "Too Many Requests"},
    )
    
    with patch.object(client._client, 'get', new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        
        # Attempt a search_recent call
        with pytest.raises(ApplicationError) as exc_info:
            await client.search_recent("test query", max_results=10)
        
        # Verify error properties
        error = exc_info.value
        assert error.type == "RateLimitExceeded"
        assert error.non_retryable is False
        assert error.next_retry_delay is not None
        # Should use default (60 seconds per code inspection)
        assert error.next_retry_delay.total_seconds() == pytest.approx(60.0)
