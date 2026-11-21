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
from typing import Optional, Set
import asyncio

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from backend.activities import (
        search_recent,
        SearchRecentInput,
    )
    from backend.workflows.review import ReviewWorkflow, ReviewInput
    from temporalio.workflow import ParentClosePolicy


@dataclass
class TopicWatchInput:
    """Input for TopicWatch workflow."""
    
    account: str
    topic: str
    query: str
    product_brief: str
    poll_interval_hours: int = 3
    max_results: int = 10
    confidence_threshold: float = 0.6
    continue_as_new_after: int = 100
    since_id: Optional[str] = None
    iteration: int = 0
    seen_post_ids: Optional[List[str]] = None


@workflow.defn(name="TopicWatch")
class TopicWatchWorkflow:
    """
    TopicWatch workflow - polls X for a topic.
    
    Workflow ID: watch:{account}:{topic}
    
    Steps:
    1. Sleep for poll_interval_hours
    2. Search recent posts with since_id
    3. Filter already-seen post IDs
    4. Spawn Review child for each new post
    5. Continue-As-New after N iterations
    """
    
    def __init__(self):
        self.stop_requested = False
    
    @workflow.run
    async def run(self, input: TopicWatchInput) -> str:
        """Run the topic watch loop."""
        workflow.logger.info(
            f"TopicWatch starting: account={input.account}, topic={input.topic}, "
            f"iteration={input.iteration}"
        )
        
        seen_post_ids = input.seen_post_ids or []
        since_id = input.since_id
        
        for iteration in range(input.iteration, input.continue_as_new_after):
            if self.stop_requested:
                workflow.logger.info("Stop requested, ending watch")
                return "stopped"
            
            workflow.logger.info(f"Iteration {iteration}: sleeping {input.poll_interval_hours}h")
            
            try:
                await workflow.wait_condition(
                    lambda: self.stop_requested,
                    timeout=timedelta(hours=input.poll_interval_hours),
                )
            except asyncio.TimeoutError:
                pass
            
            if self.stop_requested:
                workflow.logger.info("Stop requested during sleep, ending watch")
                return "stopped"
            
            workflow.logger.info(f"Searching for '{input.query}' since_id={since_id}")
            
            try:
                search_result = await workflow.execute_activity(
                    search_recent,
                    SearchRecentInput(
                        query=input.query,
                        since_id=since_id,
                        max_results=input.max_results,
                        idempotency_key=f"{input.account}:{input.topic}:{iteration}",
                    ),
                    start_to_close_timeout=timedelta(seconds=settings.activity_timeout_medium_sec),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                
                workflow.logger.info(
                    f"Search returned {len(search_result.posts)} posts, "
                    f"cost=${search_result.cost_usd:.4f}"
                )
                
                new_posts = [
                    p for p in search_result.posts
                    if p.id not in seen_post_ids
                ]
                
                workflow.logger.info(f"Found {len(new_posts)} new posts")
                
                for post in new_posts:
                    review_id = f"review:{input.account}:{post.id}"
                    
                    workflow.logger.info(f"Starting Review for post {post.id}")
                    
                    try:
                        # Start child without awaiting (non-blocking)
                        # Use ABANDON so reviews continue if watch stops
                        await workflow.start_child_workflow(
                            ReviewWorkflow.run,
                            ReviewInput(
                                post=post,
                                topic=input.topic,
                                product_brief=input.product_brief,
                                account=input.account,
                                confidence_threshold=input.confidence_threshold,
                            ),
                            id=review_id,
                            parent_close_policy=ParentClosePolicy.ABANDON,
                        )
                        workflow.logger.info(f"Started Review {review_id}")
                    except workflow.exceptions.WorkflowAlreadyStartedError:
                        workflow.logger.info(f"Review {review_id} already exists, skipping")
                    except workflow.exceptions.ChildWorkflowError as e:
                        workflow.logger.error(f"Failed to start Review: {e}")
                    
                    seen_post_ids.add(post.id)
                
                if search_result.newest_id:
                    since_id = search_result.newest_id
                
                # Keep last 1000 IDs (deterministic truncation)
                if len(seen_post_ids) > 1000:
                    seen_post_ids = seen_post_ids[-1000:]
            
            except Exception as e:
                workflow.logger.error(f"Search failed: {e}")
                # Re-raise to fail the workflow - don't continue silently
                raise
        
        workflow.logger.info(
            f"Reached Continue-As-New threshold ({input.continue_as_new_after} iterations)"
        )
        
        workflow.continue_as_new(
            TopicWatchInput(
                account=input.account,
                topic=input.topic,
                query=input.query,
                product_brief=input.product_brief,
                poll_interval_hours=input.poll_interval_hours,
                max_results=input.max_results,
                confidence_threshold=input.confidence_threshold,
                continue_as_new_after=input.continue_as_new_after,
                since_id=since_id,
                iteration=0,
                seen_post_ids=seen_post_ids,
            )
        )
    
    @workflow.signal
    def stop(self):
        """Stop the watch loop."""
        workflow.logger.info("Received stop signal")
        self.stop_requested = True
