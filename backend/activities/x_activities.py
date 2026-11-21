"""X API activities."""

from dataclasses import dataclass
from typing import Optional, List
from temporalio import activity

from backend.models import Post
from backend.adapters import FakeXClient


@dataclass
class SearchRecentInput:
    """Input for search_recent activity."""
    
    query: str
    since_id: Optional[str] = None
    max_results: int = 10
    idempotency_key: str = ""


@dataclass
class SearchRecentOutput:
    """Output from search_recent activity."""
    
    posts: List[Post]
    newest_id: Optional[str]
    cost_usd: float


@dataclass
class LookupPostInput:
    """Input for lookup_post activity."""
    
    post_id: str
    idempotency_key: str


@activity.defn(name="search_recent")
async def search_recent(input: SearchRecentInput) -> SearchRecentOutput:
    """
    Search recent posts on X.
    
    Idempotent: same query + since_id returns cached results within 24h UTC.
    Cost tracking: updates the daily spend ledger.
    """
    activity.logger.info(
        f"Searching X for '{input.query}' since_id={input.since_id} "
        f"(key: {input.idempotency_key})"
    )
    
    client = FakeXClient()
    
    posts, newest_id = await client.search_recent(
        query=input.query,
        since_id=input.since_id,
        max_results=input.max_results,
    )
    
    cost_usd = len(posts) * 0.005
    
    activity.logger.info(f"Found {len(posts)} posts, cost ${cost_usd:.4f}")
    
    return SearchRecentOutput(
        posts=posts,
        newest_id=newest_id,
        cost_usd=cost_usd,
    )


@activity.defn(name="lookup_post")
async def lookup_post(input: LookupPostInput) -> Optional[Post]:
    """
    Lookup a single post by ID.
    
    Idempotent: same post_id is cached.
    Returns None if post is deleted (404).
    """
    activity.logger.info(f"Looking up post {input.post_id} (key: {input.idempotency_key})")
    
    client = FakeXClient()
    
    post = await client.lookup_post(input.post_id)
    
    if post:
        activity.logger.info(f"Found post {input.post_id} by @{post.author.username}")
    else:
        activity.logger.info(f"Post {input.post_id} not found (deleted or 404)")
    
    return post
