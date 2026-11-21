"""X API activities."""

from dataclasses import dataclass
from typing import Optional, List
from temporalio import activity

from backend.models import Post
from backend.adapters import FakeXClient
from backend.budget import check_x_budget, record_x_reads
from backend.config import get_settings


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
    Budget enforcement: checks daily cap before calling.
    """
    activity.logger.info(
        f"Searching X for '{input.query}' since_id={input.since_id} "
        f"(key: {input.idempotency_key})"
    )
    
    settings = get_settings()
    
    # Check budget before making the call
    if not check_x_budget(input.max_results):
        activity.logger.warning(f"Daily X budget cap reached, returning empty results")
        return SearchRecentOutput(
            posts=[],
            newest_id=input.since_id,
            cost_usd=0.0,
        )
    
    client = FakeXClient()
    
    posts, newest_id = await client.search_recent(
        query=input.query,
        since_id=input.since_id,
        max_results=input.max_results,
    )
    
    # Calculate cost from config
    cost_usd = len(posts) * settings.x_post_read_cost
    
    # Record in ledger
    record_x_reads(len(posts), cost_usd)
    
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
    Budget: Records 1 post read.
    """
    activity.logger.info(f"Looking up post {input.post_id} (key: {input.idempotency_key})")
    
    settings = get_settings()
    
    # Check budget
    if not check_x_budget(1):
        activity.logger.warning(f"Daily X budget cap reached, skipping lookup")
        return None
    
    client = FakeXClient()
    
    post = await client.lookup_post(input.post_id)
    
    if post:
        activity.logger.info(f"Found post {input.post_id} by @{post.author.username}")
        # Record cost
        record_x_reads(1, settings.x_post_read_cost)
    else:
        activity.logger.info(f"Post {input.post_id} not found (deleted or 404)")
    
    return post


@dataclass
class CheckFollowupInput:
    """Input for check_followup activity."""
    
    reply_post_id: str
    idempotency_key: str


@activity.defn(name="check_followup")
async def check_followup(input: CheckFollowupInput) -> List[Post]:
    """
    Check for replies to a posted reply.
    
    Searches conversation_id:<reply_post_id> to find replies.
    Returns list of reply posts.
    """
    activity.logger.info(
        f"Checking followup for reply {input.reply_post_id} (key: {input.idempotency_key})"
    )
    
    client = FakeXClient()
    
    # Search for posts in the conversation thread
    query = f"conversation_id:{input.reply_post_id}"
    posts, _ = await client.search_recent(
        query=query,
        max_results=10,
    )
    
    activity.logger.info(f"Found {len(posts)} replies in conversation")
    
    return posts
