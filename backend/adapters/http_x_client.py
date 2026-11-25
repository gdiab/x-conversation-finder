"""
HTTP-based X API v2 client.

Implements XClientPort using real X API v2 bearer token authentication.
"""

import httpx
from typing import Optional
from datetime import datetime, timezone, timedelta
from temporalio.exceptions import ApplicationError

from backend.ports import SearchResult, XClientPort
from backend.models import Post, Author

# Issue #9: X API v2 problem types meaning the post is gone or no longer
# visible to us (deleted, or its author went protected); the removal duty
# applies to both. Shapes from the X API v2 docs, not yet seen live.
POST_GONE_PROBLEM_TYPES = (
    "https://api.twitter.com/2/problems/resource-not-found",
    "https://api.twitter.com/2/problems/not-authorized-for-resource",
)


def _post_is_gone(body: dict, post_id: str) -> bool:
    """True when a lookup body without data carries a post-gone error for this post."""
    errors = body.get("errors")
    if not isinstance(errors, list):
        return False
    for error in errors:
        if not isinstance(error, dict) or error.get("type") not in POST_GONE_PROBLEM_TYPES:
            continue
        resource_id = error.get("resource_id", error.get("value"))
        if resource_id is not None and str(resource_id) == str(post_id):
            return True
    return False


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
    
    def billable_max_results(self, max_results: int) -> int:
        """X's recent search takes 10-100 results; the request is clamped."""
        return max(10, min(max_results, 100))
    
    async def search_recent(
        self,
        query: str,
        since_id: Optional[str] = None,
        max_results: int = 10,
    ) -> SearchResult:
        """
        Search recent posts using X API v2 recent search endpoint.
        
        Args:
            query: Search query string
            since_id: Return posts newer than this ID
            max_results: Maximum number of results (10-100, clamped)
        
        Returns:
            SearchResult: the converted posts, the newest post ID in the
            response and its billable read count (every post object in
            `data`, issue #17)
        
        Raises:
            httpx.HTTPError: On API errors
        """
        # X API requires 10-100 range (S-7)
        clamped_max = self.billable_max_results(max_results)
        
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
            return SearchResult(posts=posts, newest_id=newest_id, billable_reads=0)
        
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
            # Issue #17: track the newest ID over every returned post, also
            # one dropped below: X billed it, and the next poll's since_id
            # must not fetch it again.
            if newest_id is None or int(tweet["id"]) > int(newest_id):
                newest_id = tweet["id"]
            
            author = authors_by_id.get(tweet.get("author_id"))
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
        
        # Issue #17: X bills every post object in `data`, including the ones
        # dropped above for a missing author.
        return SearchResult(posts=posts, newest_id=newest_id, billable_reads=len(data["data"]))
    
    async def lookup_post(self, post_id: str) -> Optional[Post]:
        """
        Lookup a single post by ID using X API v2.
        
        Args:
            post_id: The post ID to lookup
        
        Returns:
            Post object if found; None if X's error body (on a 200 without
            data, or on an HTTP 404) names this post as gone. None returned
            no post object and is not billed (issue #17).
        
        Raises:
            ApplicationError: MalformedLookupResponse for post data that
                cannot be converted (billed); LookupReturnedNoPost for a 200
                with neither the post nor a post-gone error (not billed)
            httpx.HTTPError: On other API errors, including a 404 that does
                not name this post
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
                    # Issue #9: None removes the post text, so a 404 counts
                    # only when its body names this post as gone (X answers a
                    # deleted post with a problem body). Any other 404 fails
                    # the lookup as the HTTP error it is (issue #8 releases the
                    # read for a refused request).
                    try:
                        body = e.response.json()
                    except Exception:
                        body = None
                    if isinstance(body, dict) and _post_is_gone(body, post_id):
                        return None
                    raise
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
            # Issue #9: None means "the post is gone" and makes the target
            # re-fetch remove its text, so only an error naming this post as
            # not found or not visible counts; any other body fails the lookup.
            if _post_is_gone(data, post_id):
                return None
            # Issue #17: no post object came back, so X billed nothing
            # (x_billed_nothing releases the read).
            raise ApplicationError(
                f"X API lookup of post {post_id} returned neither the post nor a not-found error",
                type="LookupReturnedNoPost",
            )
        
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
            # R4-SF3: Real X API should always include author when expansions=author_id.
            # Never use placeholder data. Issue #9: and never return None here,
            # since None means "not found" and makes the target re-fetch remove
            # the post text; the post exists, so fail the lookup (retryable).
            raise ApplicationError(
                f"X API returned post {post_id} without its author (expansions=author_id)",
                type="MalformedLookupResponse",
            )
        
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
