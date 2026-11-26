"""
TopicWatch workflow - one per account+topic.

Per ADR-0002:
- Long-lived loop with durable timer
- Polls X search every few hours
- Tracks since_id
- Uses Continue-As-New after N iterations
- Spawns Review child workflows per new post
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional
import asyncio

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from backend.activities import SearchRecentInput, SearchRecentOutput
    from backend.workflows.review import ReviewWorkflow, ReviewInput, ReviewConfig
    from temporalio.workflow import ParentClosePolicy
    from backend.config.confidence import CONFIDENCE_THRESHOLD


@dataclass(frozen=True)
class TopicWatchConfig:
    """Workflow config passed through input for determinism."""
    poll_interval_hours: int = 3
    activity_timeout_medium_sec: int = 30
    activity_max_attempts: int = 3
    allow_sub_floor_poll: bool = False  # S-3: Allow poll_interval_hours < 1 (test-only)


@dataclass
class TopicWatchInput:
    """Input for TopicWatch workflow."""
    
    account: str
    topic: str
    query: str
    product_brief: str
    max_results: int = 10
    confidence_threshold: float = CONFIDENCE_THRESHOLD  # dated in backend/config/confidence.py
    continue_as_new_after: int = 100
    since_id: Optional[str] = None
    iteration: int = 0
    seen_post_ids: Optional[list[str]] = None
    config: TopicWatchConfig = None
    review_config: ReviewConfig = None
    # issue #12: the Start Watch form's "Product name" and "Also called"
    # fields, passed to each Review for the written-draft name trigger. The
    # defaults are what a watch started before the fields existed decodes to;
    # its Reviews stay in assembled mode until it is restarted with a name.
    product_name: str = ""
    product_aliases: Optional[list[str]] = None
    # issue #12 (George's decision, 2026-11-25): the form's optional "Key
    # features" list, passed to each Review like the aliases. It is the only
    # source of feature terms. None for a watch started without the field.
    key_features: Optional[list[str]] = None
    # issue #41: the form's "Product URL", passed to each Review; code appends
    # it to drafts that name the product. None for a watch started before
    # the field existed: its Reviews' drafts get no link.
    product_url: Optional[str] = None
    
    def __post_init__(self):
        """Default configs if not provided."""
        if self.config is None:
            object.__setattr__(self, 'config', TopicWatchConfig())
        if self.review_config is None:
            object.__setattr__(self, 'review_config', ReviewConfig())


@workflow.defn(name="TopicWatch")
class TopicWatchWorkflow:
    """
    TopicWatch workflow - polls X for a topic.
    
    Workflow ID: watch:{account}:{topic}
    
    Steps (each iteration):
    1. Search recent posts with since_id
    2. Filter already-seen post IDs
    3. Spawn Review child for each new post
    4. Sleep for poll_interval_hours
    Continue-As-New after N iterations. The search runs first, so a new watch
    returns results right away; because every iteration ends with its sleep,
    the run started by Continue-As-New can also search immediately without
    shortening the poll interval.
    """
    
    def __init__(self):
        self.stop_requested = False
    
    @workflow.run
    async def run(self, input: TopicWatchInput) -> str:
        """Run the topic watch loop.
        
        S-3: Enforces minimum 1-hour poll interval unless allow_sub_floor_poll=True.
        Direct workflow starts with sub-floor polls will be clamped with a warning.
        """
        workflow.logger.info(
            f"TopicWatch starting: account={input.account}, topic={input.topic}, "
            f"iteration={input.iteration}"
        )
        
        # S-3: Validate poll floor at workflow start
        poll_interval_hours = input.config.poll_interval_hours
        if poll_interval_hours < 1 and not input.config.allow_sub_floor_poll:
            workflow.logger.warning(
                f"Poll interval {poll_interval_hours}h is below 1h minimum "
                f"and allow_sub_floor_poll=False, clamping to 1h"
            )
            poll_interval_hours = 1
        
        seen_post_ids = input.seen_post_ids or []
        since_id = input.since_id
        
        for iteration in range(input.iteration, input.continue_as_new_after):
            if self.stop_requested:
                workflow.logger.info("Stop requested, ending watch")
                return "stopped"

            workflow.logger.info(
                f"Iteration {iteration}: searching for '{input.query}' since_id={since_id}"
            )
            
            try:
                search_result = await workflow.execute_activity(
                    "search_recent",
                    SearchRecentInput(
                        query=input.query,
                        since_id=since_id,
                        max_results=input.max_results,
                        idempotency_key=f"{input.account}:{input.topic}:{iteration}",
                        poll_interval_hours=poll_interval_hours,  # Item 3: pass for adapter validation
                        # issue #23: the activity stores the posts and returns their ids
                        store_posts=True,
                    ),
                    start_to_close_timeout=timedelta(seconds=input.config.activity_timeout_medium_sec),
                    retry_policy=RetryPolicy(maximum_attempts=input.config.activity_max_attempts),
                    result_type=SearchRecentOutput,
                )
                
                # issue #23: a result recorded before the change carries the
                # posts themselves (and they were never stored): those
                # Reviews get the post in their input, as before. A new
                # result carries only ids. Chosen by the recorded result, so
                # replay starts the same children.
                found = [(p.id, p) for p in search_result.posts] or [
                    (post_id, None) for post_id in search_result.post_ids
                ]
                workflow.logger.info(
                    f"Search returned {len(found)} posts, "
                    f"cost=${search_result.cost_usd:.4f}"
                )
                
                new_posts = [
                    (post_id, post) for post_id, post in found
                    if post_id not in seen_post_ids
                ]
                
                workflow.logger.info(f"Found {len(new_posts)} new posts")
                
                for post_id, post in new_posts:
                    review_id = f"review:{input.account}:{post_id}"
                    
                    workflow.logger.info(f"Starting Review for post {post_id}")
                    
                    try:
                        # Start child without awaiting (non-blocking)
                        # Use ABANDON so reviews continue if watch stops
                        await workflow.start_child_workflow(
                            ReviewWorkflow.run,
                            ReviewInput(
                                post=post,
                                post_id=post_id,
                                topic=input.topic,
                                product_brief=input.product_brief,
                                account=input.account,
                                confidence_threshold=input.confidence_threshold,
                                config=input.review_config,
                                product_name=input.product_name,
                                product_aliases=input.product_aliases,
                                key_features=input.key_features,
                                product_url=input.product_url,
                            ),
                            id=review_id,
                            parent_close_policy=ParentClosePolicy.ABANDON,
                        )
                        workflow.logger.info(f"Started Review {review_id}")
                    except workflow.exceptions.WorkflowAlreadyStartedError:
                        workflow.logger.info(f"Review {review_id} already exists, skipping")
                    except workflow.exceptions.ChildWorkflowError as e:
                        workflow.logger.error(f"Failed to start Review: {e}")
                    
                    if post_id not in seen_post_ids:
                        seen_post_ids.append(post_id)
                
                if search_result.newest_id:
                    since_id = search_result.newest_id
                
                # Keep last 1000 IDs (deterministic truncation)
                if len(seen_post_ids) > 1000:
                    seen_post_ids = seen_post_ids[-1000:]
            
            except Exception as e:
                workflow.logger.error(f"Search failed: {e}")
                # Re-raise to fail the workflow - don't continue silently
                raise

            workflow.logger.info(f"Iteration {iteration}: sleeping {poll_interval_hours}h")

            try:
                await workflow.wait_condition(
                    lambda: self.stop_requested,
                    timeout=timedelta(hours=poll_interval_hours),
                )
            except asyncio.TimeoutError:
                pass

            if self.stop_requested:
                workflow.logger.info("Stop requested during sleep, ending watch")
                return "stopped"

        workflow.logger.info(
            f"Reached Continue-As-New threshold ({input.continue_as_new_after} iterations)"
        )
        
        workflow.continue_as_new(
            TopicWatchInput(
                account=input.account,
                topic=input.topic,
                query=input.query,
                product_brief=input.product_brief,
                max_results=input.max_results,
                confidence_threshold=input.confidence_threshold,
                continue_as_new_after=input.continue_as_new_after,
                since_id=since_id,
                iteration=0,
                seen_post_ids=seen_post_ids,
                config=input.config,
                review_config=input.review_config,
                product_name=input.product_name,
                product_aliases=input.product_aliases,
                key_features=input.key_features,
                product_url=input.product_url,
            )
        )
    
    @workflow.signal
    def stop(self):
        """Stop the watch loop."""
        workflow.logger.info("Received stop signal")
        self.stop_requested = True
