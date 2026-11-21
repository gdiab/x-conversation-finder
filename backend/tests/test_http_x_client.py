"""
Tests for HttpXClient using HTTP mocks.

No real X API calls are made; all responses are mocked.
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch
import httpx

from backend.adapters.http_x_client import HttpXClient


@pytest.mark.asyncio
async def test_http_x_client_init_requires_token():
    """Test that HttpXClient requires a non-empty bearer token."""
    with pytest.raises(ValueError, match="bearer token cannot be empty"):
        HttpXClient("")
    
    with pytest.raises(ValueError, match="bearer token cannot be empty"):
        HttpXClient("   ")


@pytest.mark.asyncio
async def test_search_recent_success():
    """Test successful search_recent call with mocked HTTP response."""
    client = HttpXClient("test_bearer_token_123")
    
    mock_response = {
        "data": [
            {
                "id": "1234567890",
                "text": "Test post about distributed systems",
                "author_id": "author_123",
                "created_at": "2026-11-21T10:00:00.000Z",
                "conversation_id": "1234567890",
                "reply_settings": "everyone",
                "public_metrics": {
                    "like_count": 10,
                    "reply_count": 2,
                    "retweet_count": 5,
                    "quote_count": 1,
                },
            }
        ],
        "includes": {
            "users": [
                {
                    "id": "author_123",
                    "username": "alice_dev",
                    "name": "Alice Developer",
                    "verified": False,
                    "public_metrics": {
                        "followers_count": 1000,
                    },
                }
            ]
        },
    }
    
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        mock_response_obj = Mock()
        mock_response_obj.raise_for_status.return_value = None
        mock_response_obj.json.return_value = mock_response
        mock_client.get.return_value = mock_response_obj
        
        posts, newest_id = await client.search_recent("distributed systems", max_results=10)
    
    assert len(posts) == 1
    assert posts[0].id == "1234567890"
    assert posts[0].text == "Test post about distributed systems"
    assert posts[0].author.username == "alice_dev"
    assert posts[0].like_count == 10
    assert newest_id == "1234567890"


@pytest.mark.asyncio
async def test_search_recent_with_since_id():
    """Test search_recent with since_id parameter."""
    client = HttpXClient("test_bearer_token_123")
    
    mock_response = {
        "data": [
            {
                "id": "9999999999",
                "text": "Newer post",
                "author_id": "author_456",
                "created_at": "2026-11-21T11:00:00.000Z",
                "conversation_id": "9999999999",
                "reply_settings": "everyone",
                "public_metrics": {
                    "like_count": 5,
                    "reply_count": 1,
                    "retweet_count": 2,
                    "quote_count": 0,
                },
            }
        ],
        "includes": {
            "users": [
                {
                    "id": "author_456",
                    "username": "bob_dev",
                    "name": "Bob Developer",
                    "verified": True,
                    "public_metrics": {
                        "followers_count": 5000,
                    },
                }
            ]
        },
    }
    
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        mock_response_obj = Mock()
        mock_response_obj.raise_for_status.return_value = None
        mock_response_obj.json.return_value = mock_response
        mock_client.get.return_value = mock_response_obj
        
        posts, newest_id = await client.search_recent(
            "test query",
            since_id="1234567890",
            max_results=10
        )
    
    # Verify since_id was passed in params
    call_args = mock_client.get.call_args
    assert call_args[1]["params"]["since_id"] == "1234567890"
    
    assert len(posts) == 1
    assert posts[0].id == "9999999999"
    assert newest_id == "9999999999"


@pytest.mark.asyncio
async def test_search_recent_no_results():
    """Test search_recent when no results are returned."""
    client = HttpXClient("test_bearer_token_123")
    
    mock_response = {
        "meta": {
            "result_count": 0
        }
    }
    
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        mock_response_obj = Mock()
        mock_response_obj.raise_for_status.return_value = None
        mock_response_obj.json.return_value = mock_response
        mock_client.get.return_value = mock_response_obj
        
        posts, newest_id = await client.search_recent("no results query")
    
    assert posts == []
    assert newest_id is None


@pytest.mark.asyncio
async def test_lookup_post_success():
    """Test successful lookup_post call."""
    client = HttpXClient("test_bearer_token_123")
    
    mock_response = {
        "data": {
            "id": "1234567890",
            "text": "Test post content",
            "author_id": "author_123",
            "created_at": "2026-11-21T10:00:00.000Z",
            "conversation_id": "1234567890",
            "reply_settings": "everyone",
            "public_metrics": {
                "like_count": 10,
                "reply_count": 2,
                "retweet_count": 5,
                "quote_count": 1,
            },
        },
        "includes": {
            "users": [
                {
                    "id": "author_123",
                    "username": "alice_dev",
                    "name": "Alice Developer",
                    "verified": False,
                    "public_metrics": {
                        "followers_count": 1000,
                    },
                }
            ]
        },
    }
    
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        mock_response_obj = Mock()
        mock_response_obj.raise_for_status.return_value = None
        mock_response_obj.json.return_value = mock_response
        mock_client.get.return_value = mock_response_obj
        
        post = await client.lookup_post("1234567890")
    
    assert post is not None
    assert post.id == "1234567890"
    assert post.text == "Test post content"
    assert post.author.username == "alice_dev"


@pytest.mark.asyncio
async def test_lookup_post_not_found():
    """Test lookup_post returns None for 404."""
    client = HttpXClient("test_bearer_token_123")
    
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        # Mock 404 response
        mock_response_obj = Mock()
        mock_response_obj.status_code = 404
        mock_request = Mock()
        error = httpx.HTTPStatusError("Not Found", request=mock_request, response=mock_response_obj)
        mock_response_obj.raise_for_status.side_effect = error
        mock_client.get.return_value = mock_response_obj
        
        post = await client.lookup_post("nonexistent_id")
    
    assert post is None


@pytest.mark.asyncio
async def test_lookup_post_api_error():
    """Test lookup_post raises on non-404 errors."""
    client = HttpXClient("test_bearer_token_123")
    
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        # Mock 401 unauthorized response
        mock_response_obj = Mock()
        mock_response_obj.status_code = 401
        mock_request = Mock()
        error = httpx.HTTPStatusError("Unauthorized", request=mock_request, response=mock_response_obj)
        mock_response_obj.raise_for_status.side_effect = error
        mock_client.get.return_value = mock_response_obj
        
        with pytest.raises(httpx.HTTPStatusError):
            await client.lookup_post("some_id")


@pytest.mark.asyncio
async def test_search_recent_tracks_newest_id():
    """Test that search_recent correctly identifies the newest post ID."""
    client = HttpXClient("test_bearer_token_123")
    
    mock_response = {
        "data": [
            {
                "id": "1000",
                "text": "Old post",
                "author_id": "author_123",
                "created_at": "2026-11-21T10:00:00.000Z",
                "conversation_id": "1000",
                "reply_settings": "everyone",
                "public_metrics": {
                    "like_count": 1,
                    "reply_count": 0,
                    "retweet_count": 0,
                    "quote_count": 0,
                },
            },
            {
                "id": "2000",
                "text": "Newer post",
                "author_id": "author_123",
                "created_at": "2026-11-21T11:00:00.000Z",
                "conversation_id": "2000",
                "reply_settings": "everyone",
                "public_metrics": {
                    "like_count": 2,
                    "reply_count": 0,
                    "retweet_count": 0,
                    "quote_count": 0,
                },
            },
            {
                "id": "1500",
                "text": "Middle post",
                "author_id": "author_123",
                "created_at": "2026-11-21T10:30:00.000Z",
                "conversation_id": "1500",
                "reply_settings": "everyone",
                "public_metrics": {
                    "like_count": 1,
                    "reply_count": 0,
                    "retweet_count": 0,
                    "quote_count": 0,
                },
            },
        ],
        "includes": {
            "users": [
                {
                    "id": "author_123",
                    "username": "test_user",
                    "name": "Test User",
                    "verified": False,
                    "public_metrics": {
                        "followers_count": 100,
                    },
                }
            ]
        },
    }
    
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        mock_response_obj = Mock()
        mock_response_obj.raise_for_status.return_value = None
        mock_response_obj.json.return_value = mock_response
        mock_client.get.return_value = mock_response_obj
        
        posts, newest_id = await client.search_recent("test query")
    
    # Should track the highest ID (2000), not the most recent by timestamp
    assert newest_id == "2000"
    assert len(posts) == 3
