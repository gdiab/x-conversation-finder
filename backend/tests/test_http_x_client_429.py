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
    
    # Mock httpx.AsyncClient to return 429
    mock_response = httpx.Response(
        status_code=429,
        headers={"Retry-After": "120"},
        request=httpx.Request("GET", "https://api.twitter.com/2/tweets/search/recent"),
    )
    
    async def mock_get(*args, **kwargs):
        raise httpx.HTTPStatusError("Rate limited", request=mock_response.request, response=mock_response)
    
    with patch('httpx.AsyncClient') as mock_async_client:
        mock_instance = AsyncMock()
        mock_instance.__aenter__.return_value = mock_instance
        mock_instance.__aexit__.return_value = None
        mock_instance.get = mock_get
        mock_async_client.return_value = mock_instance
        
        # Attempt a search_recent call
        with pytest.raises(ApplicationError) as exc_info:
            await client.search_recent("test query", max_results=10)
        
        # Verify error properties
        error = exc_info.value
        assert error.type == "RateLimitError"
        assert error.non_retryable is False
        assert error.next_retry_delay is not None
        assert error.next_retry_delay.total_seconds() == pytest.approx(120.0)
        assert "rate limit" in str(error).lower()


@pytest.mark.asyncio
async def test_429_lookup_post_raises_typed_retryable_error():
    """Test that 429 on lookup_post also raises ApplicationError properly."""
    client = HttpXClient(bearer_token="test_token")
    
    mock_response = httpx.Response(
        status_code=429,
        headers={"Retry-After": "60"},
        request=httpx.Request("GET", "https://api.twitter.com/2/tweets"),
    )
    
    async def mock_get(*args, **kwargs):
        raise httpx.HTTPStatusError("Rate limited", request=mock_response.request, response=mock_response)
    
    with patch('httpx.AsyncClient') as mock_async_client:
        mock_instance = AsyncMock()
        mock_instance.__aenter__.return_value = mock_instance
        mock_instance.__aexit__.return_value = None
        mock_instance.get = mock_get
        mock_async_client.return_value = mock_instance
        
        with pytest.raises(ApplicationError) as exc_info:
            await client.lookup_post("1234567890")
        
        error = exc_info.value
        assert error.type == "RateLimitError"
        assert error.non_retryable is False
        assert error.next_retry_delay is not None
        assert error.next_retry_delay.total_seconds() == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_429_without_retry_after_uses_default():
    """Test that 429 without Retry-After header uses default delay."""
    client = HttpXClient(bearer_token="test_token")
    
    mock_response = httpx.Response(
        status_code=429,
        headers={},
        request=httpx.Request("GET", "https://api.twitter.com/2/tweets/search/recent"),
    )
    
    async def mock_get(*args, **kwargs):
        raise httpx.HTTPStatusError("Rate limited", request=mock_response.request, response=mock_response)
    
    with patch('httpx.AsyncClient') as mock_async_client:
        mock_instance = AsyncMock()
        mock_instance.__aenter__.return_value = mock_instance
        mock_instance.__aexit__.return_value = None
        mock_instance.get = mock_get
        mock_async_client.return_value = mock_instance
        
        with pytest.raises(ApplicationError) as exc_info:
            await client.search_recent("test query", max_results=10)
        
        error = exc_info.value
        assert error.type == "RateLimitError"
        assert error.non_retryable is False
        assert error.next_retry_delay is not None
        assert error.next_retry_delay.total_seconds() == pytest.approx(60.0)
