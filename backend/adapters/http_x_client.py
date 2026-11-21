"""
HTTP-based X API v2 client.

Implements XClientPort using real X API v2 bearer token authentication.
"""

import httpx
from typing import List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from temporalio.exceptions import ApplicationError

from backend.ports import XClientPort
from backend.models import Post, Author


class HttpXClient(XClientPort):
    """Real X API v2 client using bearer token authentication."""
    
    def __init__(self, bearer_token: str):
        """
        Initialize HTTP X client.
        
        Args:
            bearer_token: X API bearer token
        
        Raises:
            ValueError: If bearer_token is empty
        """
        if not bearer_token or not bearer_token.strip():
            raise ValueError("X bearer token cannot be empty")
        
        self.bearer_token = bearer_token.strip()
        self.base_url = "https://api.twitter.com/2"
        
    def _get_headers(self) -> dict:
        """Get authorization headers for X API requests."""
        return {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
        }
    
    async def search_recent(
        self,
        query: str,
        since_id: Optional[str] = None,
        max_results: int = 10,
    ) -> Tuple[List[Post], Optional[str]]:
        """
        Search recent posts using X API v2 recent search endpoint.
        
        Args:
            query: Search query string
            since_id: Return posts newer than this ID
            max_results: Maximum number of results (10-100, clamped)
        
        Returns:
            Tuple of (list of posts, newest_id)
        
        Raises:
            httpx.HTTPError: On API errors
        """
        # X API requires 10-100 range (S-7)
        clamped_max = max(10, min(max_results, 100))
        
        params = {
            "query": query,
            "max_results": clamped_max,
            "tweet.fields": "created_at,conversation_id,in_reply_to_user_id,reply_settings,public_metrics",
            "expansions": "author_id",
            "user.fields": "name,username,verified,public_metrics",
        }
        
        if since_id:
            params["since_id"] = since_id
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/tweets/search/recent",
                    headers=self._get_headers(),
                    params=params,
                    timeout=30.0,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                # R4-SF4: Handle 429 rate limit with typed ApplicationError
                if e.response.status_code == 429:
                    retry_after_str = e.response.headers.get("Retry-After", "60")
                    try:
                        retry_after_seconds = int(retry_after_str)
                    except ValueError:
                        retry_after_seconds = 60
                    
                    raise ApplicationError(
                        f"X API rate limited. Retry after {retry_after_seconds} seconds",
                        type="RateLimitError",
                        non_retryable=False,
                        next_retry_delay=timedelta(seconds=retry_after_seconds),
                    )
                raise
            data = response.json()
        
        posts = []
        newest_id = None
        
        if "data" not in data or not data["data"]:
            return (posts, newest_id)
        
        # Build author lookup from includes
        authors_by_id = {}
        if "includes" in data and "users" in data["includes"]:
            for user_data in data["includes"]["users"]:
                authors_by_id[user_data["id"]] = Author(
                    id=user_data["id"],
                    username=user_data["username"],
                    name=user_data["name"],
                    followers_count=user_data.get("public_metrics", {}).get("followers_count", 0),
                    verified=user_data.get("verified", False),
                )
        
        # Parse tweets
        for tweet in data["data"]:
            author = authors_by_id.get(tweet["author_id"])
            if not author:
                # S-7: If author not in includes, skip this tweet rather than using placeholder
                # (Real X API should always include authors when expansions=author_id)
                continue
            
            metrics = tweet.get("public_metrics", {})
            
            post = Post(
                id=tweet["id"],
                text=tweet.get("text", ""),
                author=author,
                created_at=tweet.get("created_at", datetime.now(timezone.utc).isoformat()),
                like_count=metrics.get("like_count", 0),
                reply_count=metrics.get("reply_count", 0),
                repost_count=metrics.get("retweet_count", 0),
                quote_count=metrics.get("quote_count", 0),
                conversation_id=tweet.get("conversation_id", tweet["id"]),
                in_reply_to_user_id=tweet.get("in_reply_to_user_id"),
                reply_settings=tweet.get("reply_settings", "everyone"),
                url=f"https://x.com/{author.username}/status/{tweet['id']}",
            )
            posts.append(post)
            
            # Track newest ID
            if newest_id is None or int(tweet["id"]) > int(newest_id):
                newest_id = tweet["id"]
        
        return (posts, newest_id)
    
    async def lookup_post(self, post_id: str) -> Optional[Post]:
        """
        Lookup a single post by ID using X API v2.
        
        Args:
            post_id: The post ID to lookup
        
        Returns:
            Post object if found, None if 404
        
        Raises:
            httpx.HTTPError: On API errors other than 404
        """
        params = {
            "tweet.fields": "created_at,conversation_id,in_reply_to_user_id,reply_settings,public_metrics",
            "expansions": "author_id",
            "user.fields": "name,username,verified,public_metrics",
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/tweets/{post_id}",
                    headers=self._get_headers(),
                    params=params,
                    timeout=30.0,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                # R4-SF4: Handle 429 rate limit with typed ApplicationError
                if e.response.status_code == 429:
                    retry_after_str = e.response.headers.get("Retry-After", "60")
                    try:
                        retry_after_seconds = int(retry_after_str)
                    except ValueError:
                        retry_after_seconds = 60
                    
                    raise ApplicationError(
                        f"X API rate limited. Retry after {retry_after_seconds} seconds",
                        type="RateLimitError",
                        non_retryable=False,
                        next_retry_delay=timedelta(seconds=retry_after_seconds),
                    )
                raise
            
            data = response.json()
        
        if "data" not in data:
            return None
        
        tweet = data["data"]
        
        # Parse author (R4-SF3: no placeholder usernames)
        author = None
        if "includes" in data and "users" in data["includes"] and data["includes"]["users"]:
            user_data = data["includes"]["users"][0]
            author = Author(
                id=user_data["id"],
                username=user_data["username"],
                name=user_data["name"],
                followers_count=user_data.get("public_metrics", {}).get("followers_count", 0),
                verified=user_data.get("verified", False),
            )
        else:
            # R4-SF3: Real X API should always include author when expansions=author_id
            # If missing, return None rather than using placeholder data
            return None
        
        metrics = tweet.get("public_metrics", {})
        
        return Post(
            id=tweet["id"],
            text=tweet.get("text", ""),
            author=author,
            created_at=tweet.get("created_at", datetime.now(timezone.utc).isoformat()),
            like_count=metrics.get("like_count", 0),
            reply_count=metrics.get("reply_count", 0),
            repost_count=metrics.get("retweet_count", 0),
            quote_count=metrics.get("quote_count", 0),
            conversation_id=tweet.get("conversation_id", tweet["id"]),
            in_reply_to_user_id=tweet.get("in_reply_to_user_id"),
            reply_settings=tweet.get("reply_settings", "everyone"),
            url=f"https://x.com/{author.username}/status/{tweet['id']}",
        )
