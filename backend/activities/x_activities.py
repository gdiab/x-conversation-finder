"""X API activities with injected client."""

from dataclasses import dataclass, field
from typing import Optional, List, Union

import httpx
from temporalio import activity

from backend.models import Post
from backend.models.summaries import PostLookup
from backend.ports import XClientPort
from backend.budget import (
    reserve_x_budget,
    reconcile_x_reads,
    release_x_reservation,
    request_never_sent,
    BudgetExceeded,
)
from backend.config import get_settings
from backend.activities.store_activities import post_removal_recorded
from temporalio.exceptions import ApplicationError


# ApplicationError types for X responses that carried no post object
# (issues #8, #17): X bills per post returned, so these attempts are released.
X_UNBILLED_ERROR_TYPES = (
    "RateLimitError",  # 429 from search or lookup
    "LookupReturnedNoPost",  # a lookup 200 with neither the post nor a post-gone error
)


def x_billed_nothing(exc: BaseException) -> bool:
    """True when X returned nothing billable for the request (issue #8): X
    refused it with a 4xx status, answered without any post object (issue
    #17), or the connection was never established.

    X bills per post returned, and a refused request returns none, so the
    attempt's reservation is released. The HTTP adapter's base URL is fixed to
    the X API, so a 4xx comes from X itself, not from a gateway. The HTTP adapter raises
    httpx.HTTPStatusError, or an ApplicationError: RateLimitError for 429,
    LookupReturnedNoPost for a lookup 200 without data, MalformedLookupResponse
    for post data it cannot convert (X returned and billed the post, so it
    stays counted).
    408, 5xx, read timeouts and other errors return False: the request may
    have been served, so the reservation stays counted.
    """
    if request_never_sent(exc):
        return True
    if isinstance(exc, ApplicationError):
        return exc.type in X_UNBILLED_ERROR_TYPES
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return 400 <= status < 500 and status != 408
    return False


@dataclass
class SearchRecentInput:
    """Input for search_recent activity."""
    
    query: str
    since_id: Optional[str] = None
    max_results: int = 10
    idempotency_key: str = ""
    poll_interval_hours: Optional[int] = None  # For adapter validation (item 3)
    # Issue #23: store the posts found (backend/review_docs.py) and return
    # only their ids, so no post content enters the TopicWatch history.
    # False in inputs recorded before the field existed.
    store_posts: bool = False


@dataclass
class SearchRecentOutput:
    """Output from search_recent activity."""
    
    posts: List[Post]
    newest_id: Optional[str]
    cost_usd: float
    # Issue #23: with store_posts, the ids of the posts found (stored, in
    # order) and `posts` is empty. Empty in results recorded before.
    post_ids: List[str] = field(default_factory=list)


@dataclass
class LookupPostInput:
    """Input for lookup_post activity."""
    
    post_id: str
    idempotency_key: str
    # Issue #23: return a PostLookup (id and conversation only), not the post
    summary_only: bool = False


@dataclass
class CheckFollowupInput:
    """Input for check_followup activity."""
    
    reply_post_id: str
    idempotency_key: str
    # Issue #23: write the replies to this workflow's card and return their
    # count, so their text and authors never enter the Review's history
    store_on_card: bool = False


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
        Cost tracking: updates the daily spend ledger. Each attempt reserves and
        reconciles on its own (issue #8); an attempt X answered with an error
        status is released. Reads are reconciled to the client's billable
        count, which includes posts the adapter dropped (issue #17).
        Budget enforcement: checks daily cap before calling.
        Poll interval validation: HTTP adapter rejects sub-1h and negative intervals (item 3).
        """
        activity.logger.info(
            f"Searching X for '{input.query}' since_id={input.since_id} "
            f"(key: {input.idempotency_key})"
        )
        
        # Item 3: Validate poll interval with HTTP adapter
        # Negative intervals are rejected regardless of adapter
        if input.poll_interval_hours is not None:
            if input.poll_interval_hours < 0:
                raise ApplicationError(
                    f"Poll interval cannot be negative: {input.poll_interval_hours}h",
                    non_retryable=True,
                    type="InvalidPollInterval"
                )
            
            # HTTP adapter rejects sub-1h (only fake adapter allows it for testing)
            from backend.adapters.http_x_client import HttpXClient
            if isinstance(self._x_client, HttpXClient) and input.poll_interval_hours < 1:
                raise ApplicationError(
                    f"HTTP X adapter requires poll interval >= 1h, got {input.poll_interval_hours}h",
                    non_retryable=True,
                    type="InvalidPollInterval"
                )
        
        settings = get_settings()
        
        # Reserve budget atomically (NB3: reserve-then-commit)
        # This raises BudgetExceeded if cap would be exceeded
        # Issue #8: reserve what the request can be billed for, which the HTTP
        # adapter clamps to X's 10-100 range, not the raw max_results.
        reserved_reads = self._x_client.billable_max_results(input.max_results)
        try:
            reserved_cost, reservation_date = reserve_x_budget(
                reads_needed=reserved_reads,
                cost_per_read=settings.x_post_read_cost,
                workflow_id=activity.info().workflow_id,
                workflow_run_id=activity.info().workflow_run_id,
                activity_id=activity.info().activity_id,
            )
        except BudgetExceeded as e:
            activity.logger.error(f"Budget exceeded: {e}")
            raise
        
        try:
            result = await self._x_client.search_recent(
                query=input.query,
                since_id=input.since_id,
                max_results=input.max_results,
            )
        except Exception as e:
            if x_billed_nothing(e):
                release_x_reservation(
                    reserved_reads=reserved_reads,
                    reservation_date=reservation_date,
                    workflow_id=activity.info().workflow_id,
                    workflow_run_id=activity.info().workflow_run_id,
                    activity_id=activity.info().activity_id,
                    cost_per_read=settings.x_post_read_cost,
                )
            raise
        
        posts = result.posts
        # R5-B1: Reconcile budget to actual usage (two-way, idempotent).
        # Issue #17: X billed every post it returned, also the ones the
        # adapter dropped, so reconcile to the billable count, not len(posts).
        actual_reads = result.billable_reads
        actual_cost = reconcile_x_reads(
            reserved_reads=reserved_reads,
            actual_reads=actual_reads,
            reservation_date=reservation_date,
            workflow_id=activity.info().workflow_id,
            workflow_run_id=activity.info().workflow_run_id,
            activity_id=activity.info().activity_id,
            cost_per_read=settings.x_post_read_cost,
        )
        
        if actual_reads != reserved_reads:
            activity.logger.info(
                f"Reserved {reserved_reads} reads, X returned {actual_reads} posts "
                f"({len(posts)} usable), reconciled to ${actual_cost:.4f}"
            )
        
        activity.logger.info(f"Found {len(posts)} posts, cost ${actual_cost:.4f}")
        
        if input.store_posts:
            from backend.review_docs import store_posts

            return SearchRecentOutput(
                posts=[],
                newest_id=result.newest_id,
                cost_usd=actual_cost,
                post_ids=store_posts(posts),
            )
        return SearchRecentOutput(
            posts=posts,
            newest_id=result.newest_id,
            cost_usd=actual_cost,
        )
    
    @activity.defn(name="lookup_post")
    async def lookup_post(self, input: LookupPostInput) -> Optional[Union[Post, PostLookup]]:
        """
        Lookup a single post by ID.
        
        Idempotent: same post_id is cached.
        Returns None if the post is gone (X's error names it as not found or
        not visible; issue #9).
        Budget: 1 post read per attempt that returns the post or post data the
        adapter cannot convert (it raises; X billed it). None is reconciled to
        0 reads: X returned no post object (issue #17). An attempt X refused
        with a 4xx status, or answered without any post, is released (issue #8).
        A post whose removal is already recorded (by another account's Review
        of it, or the eval re-fetch) is gone: None, without an X read or a
        budget reservation (issue #22).
        """
        activity.logger.info(f"Looking up post {input.post_id} (key: {input.idempotency_key})")
        
        if post_removal_recorded(input.post_id):
            activity.logger.info(f"Post {input.post_id}: removal already recorded; not asking X")
            return None
        
        settings = get_settings()
        
        # Reserve budget atomically (NB3: raise if over budget, never return None)
        try:
            cost_usd, reservation_date = reserve_x_budget(
                reads_needed=1,
                cost_per_read=settings.x_post_read_cost,
                workflow_id=activity.info().workflow_id,
                workflow_run_id=activity.info().workflow_run_id,
                activity_id=activity.info().activity_id,
            )
        except BudgetExceeded as e:
            activity.logger.error(f"Budget exceeded: {e}")
            raise
        
        try:
            post = await self._x_client.lookup_post(input.post_id)
        except Exception as e:
            if x_billed_nothing(e):
                release_x_reservation(
                    reserved_reads=1,
                    reservation_date=reservation_date,
                    workflow_id=activity.info().workflow_id,
                    workflow_run_id=activity.info().workflow_run_id,
                    activity_id=activity.info().activity_id,
                    cost_per_read=settings.x_post_read_cost,
                )
            raise
        
        # Issue #8: close the reservation. Issue #17: the port returns None only
        # when X answered that the post is gone, without a post object, which
        # X does not bill; post data the adapter could not convert raises
        # instead (above) and stays counted.
        actual_reads = 1 if post is not None else 0
        cost_usd = reconcile_x_reads(
            reserved_reads=1,
            actual_reads=actual_reads,
            reservation_date=reservation_date,
            workflow_id=activity.info().workflow_id,
            workflow_run_id=activity.info().workflow_run_id,
            activity_id=activity.info().activity_id,
            cost_per_read=settings.x_post_read_cost,
        )
        if post:
            activity.logger.info(f"Found post {input.post_id}, cost ${cost_usd:.4f}")
        else:
            activity.logger.info(f"Post {input.post_id} not found (deleted or 404), cost ${cost_usd:.4f}")
        
        if input.summary_only:
            return PostLookup(post_id=post.id, conversation_id=post.conversation_id) if post else None
        return post
    
    @activity.defn(name="check_followup")
    async def check_followup(self, input: CheckFollowupInput) -> Union[List[Post], int]:
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
        max_results = self._x_client.billable_max_results(10)
        
        # Reserve budget atomically (S-2: check_followup must reserve)
        try:
            reserved_cost, reservation_date = reserve_x_budget(
                reads_needed=max_results,
                cost_per_read=settings.x_post_read_cost,
                workflow_id=activity.info().workflow_id,
                workflow_run_id=activity.info().workflow_run_id,
                activity_id=activity.info().activity_id,
            )
        except BudgetExceeded as e:
            activity.logger.error(f"Budget exceeded: {e}")
            raise
        
        # Search for posts in the conversation thread
        query = f"conversation_id:{input.reply_post_id}"
        try:
            result = await self._x_client.search_recent(
                query=query,
                max_results=max_results,
            )
        except Exception as e:
            if x_billed_nothing(e):
                release_x_reservation(
                    reserved_reads=max_results,
                    reservation_date=reservation_date,
                    workflow_id=activity.info().workflow_id,
                    workflow_run_id=activity.info().workflow_run_id,
                    activity_id=activity.info().activity_id,
                    cost_per_read=settings.x_post_read_cost,
                )
            raise
        
        posts = result.posts
        # R5-B1: Reconcile budget to actual usage (two-way, idempotent);
        # issue #17: to every post X returned, dropped ones included
        actual_reads = result.billable_reads
        actual_cost = reconcile_x_reads(
            reserved_reads=max_results,
            actual_reads=actual_reads,
            reservation_date=reservation_date,
            workflow_id=activity.info().workflow_id,
            workflow_run_id=activity.info().workflow_run_id,
            activity_id=activity.info().activity_id,
            cost_per_read=settings.x_post_read_cost,
        )
        
        if actual_reads != max_results:
            activity.logger.info(f"check_followup: reconciled to ${actual_cost:.4f} ({actual_reads} reads)")
        
        activity.logger.info(
            f"Found {len(posts)} replies in conversation (cost ${actual_cost:.4f})"
        )
        
        if input.store_on_card:
            import asyncio
            from backend.activities.store_activities import (
                UpdateCardInput,
                _update_card_sync,
                followup_reply_fields,
            )

            await asyncio.to_thread(
                _update_card_sync,
                UpdateCardInput(
                    workflow_id=activity.info().workflow_id,
                    idempotency_key=input.idempotency_key,
                    followup_replies=[followup_reply_fields(p) for p in posts],
                    # a late attempt cannot replace its retry's replies
                    writer_slot="followup",
                ),
            )
            return len(posts)
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
