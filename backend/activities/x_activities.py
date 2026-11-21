"""X API activities with injected client."""

from dataclasses import dataclass
from typing import Optional, List
from temporalio import activity

from backend.models import Post
from backend.ports import XClientPort
from backend.budget import reserve_x_budget, reconcile_x_reads, BudgetExceeded
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


@dataclass
class CheckFollowupInput:
    """Input for check_followup activity."""
    
    reply_post_id: str
    idempotency_key: str


@dataclass
class NotifyInput:
    """Input for notify activity (B8)."""
    
    message: str
    idempotency_key: str


class XActivities:
    """X API activities with injected client."""
    
    def __init__(self, x_client: XClientPort):
        self._x_client = x_client
    
    @activity.defn(name="search_recent")
    async def search_recent(self, input: SearchRecentInput) -> SearchRecentOutput:
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
        
        # Reserve budget atomically (NB3: reserve-then-commit)
        # This raises BudgetExceeded if cap would be exceeded
        try:
            cost_usd = reserve_x_budget(input.max_results, settings.x_post_read_cost)
        except BudgetExceeded as e:
            activity.logger.error(f"Budget exceeded: {e}")
            raise
        
        posts, newest_id = await self._x_client.search_recent(
            query=input.query,
            since_id=input.since_id,
            max_results=input.max_results,
        )
        
        # R4-B2: Reconcile budget to actual usage
        actual_reads = len(posts)
        actual_cost = reconcile_x_reads(input.max_results, actual_reads, settings.x_post_read_cost)
        
        if actual_reads < input.max_results:
            refund = (input.max_results - actual_reads) * settings.x_post_read_cost
            activity.logger.info(
                f"Reserved {input.max_results} reads, got {actual_reads} posts, "
                f"refunded ${refund:.4f}"
            )
        
        activity.logger.info(f"Found {len(posts)} posts, cost ${actual_cost:.4f}")
        
        return SearchRecentOutput(
            posts=posts,
            newest_id=newest_id,
            cost_usd=actual_cost,
        )
    
    @activity.defn(name="lookup_post")
    async def lookup_post(self, input: LookupPostInput) -> Optional[Post]:
        """
        Lookup a single post by ID.
        
        Idempotent: same post_id is cached.
        Returns None if post is deleted (404).
        Budget: Records 1 post read.
        """
        activity.logger.info(f"Looking up post {input.post_id} (key: {input.idempotency_key})")
        
        settings = get_settings()
        
        # Reserve budget atomically (NB3: raise if over budget, never return None)
        try:
            cost_usd = reserve_x_budget(1, settings.x_post_read_cost)
        except BudgetExceeded as e:
            activity.logger.error(f"Budget exceeded: {e}")
            raise
        
        post = await self._x_client.lookup_post(input.post_id)
        
        if post:
            activity.logger.info(f"Found post {input.post_id} by @{post.author.username}, cost ${cost_usd:.4f}")
        else:
            activity.logger.info(f"Post {input.post_id} not found (deleted or 404), cost ${cost_usd:.4f}")
        
        return post
    
    @activity.defn(name="check_followup")
    async def check_followup(self, input: CheckFollowupInput) -> List[Post]:
        """
        Check for replies to a posted reply.
        
        Per S-2: Reserves budget for X API reads.
        Searches conversation_id:<reply_post_id> to find replies.
        Returns list of reply posts.
        """
        activity.logger.info(
            f"Checking followup for reply {input.reply_post_id} (key: {input.idempotency_key})"
        )
        
        settings = get_settings()
        max_results = 10
        
        # Reserve budget atomically (S-2: check_followup must reserve)
        try:
            cost_usd = reserve_x_budget(max_results, settings.x_post_read_cost)
        except BudgetExceeded as e:
            activity.logger.error(f"Budget exceeded: {e}")
            raise
        
        # Search for posts in the conversation thread
        query = f"conversation_id:{input.reply_post_id}"
        posts, _ = await self._x_client.search_recent(
            query=query,
            max_results=max_results,
        )
        
        # R4-B2: Reconcile budget to actual usage
        actual_reads = len(posts)
        actual_cost = reconcile_x_reads(max_results, actual_reads, settings.x_post_read_cost)
        
        if actual_reads < max_results:
            refund = (max_results - actual_reads) * settings.x_post_read_cost
            activity.logger.info(f"check_followup: refunded ${refund:.4f} ({max_results - actual_reads} unused reads)")
        
        activity.logger.info(
            f"Found {actual_reads} replies in conversation (cost ${actual_cost:.4f})"
        )
        
        return posts
    
    @activity.defn(name="notify")
    async def notify(self, input: NotifyInput) -> None:
        """
        Notify activity for alerting about events (B8).
        
        Currently logs the message. In production this could send to:
        - Slack/email
        - Push notification service
        - Monitoring system
        """
        activity.logger.info(f"NOTIFY: {input.message} (key: {input.idempotency_key})")
        
        # Future: Add actual notification mechanism here
        # e.g., slack webhook, email, etc.
