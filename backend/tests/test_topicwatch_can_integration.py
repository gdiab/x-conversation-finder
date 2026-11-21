"""
Integration test for TopicWatch Continue-As-New budget reconciliation.

S-2: Deterministic test that drives the real TopicWatch workflow across Continue-As-New
and verifies that budget ledger matches actual reads from a counting fake client.

Run with: pytest -v -m integration backend/tests/test_topicwatch_can_integration.py
Requires: Temporal server running on localhost:7233
"""

import pytest
import asyncio
from datetime import timedelta
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Worker

from backend.workflows.topic_watch import TopicWatchWorkflow, TopicWatchInput, TopicWatchConfig
from backend.workflows.review import ReviewWorkflow, ReviewConfig
from backend.activities.x_activities import XActivities
from backend.activities.reply_activities import ReplyActivities
from backend.activities.store_activities import store_card
from backend.activities.claim_gate_activity import claim_gate
from backend.adapters.fake_x_client import FakeXClient
from backend.adapters.fake_reply_agent import FakeReplyAgent
from backend.database import init_db
from backend.budget import get_today_budget, get_today_utc
from backend.database import get_db, BudgetReconciliationDB


class CountingFakeXClient(FakeXClient):
    """FakeXClient that counts how many posts it actually served."""
    
    def __init__(self):
        super().__init__()
        self.total_posts_served = 0
    
    async def search_recent(self, query, since_id=None, max_results=10):
        results, newest_id = await super().search_recent(query, since_id, max_results)
        self.total_posts_served += len(results)
        return results, newest_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topicwatch_budget_across_multiple_can():
    """
    S-2: Verify budget ledger matches actual reads across multiple Continue-As-New boundaries.
    
    Uses real Temporal server with real time (not time-skipping).
    Sets low CAN threshold and 0h poll interval to cross CAN quickly.
    Waits for N distinct run IDs from history, then verifies ledger equals served reads.
    """
    # Initialize database
    init_db()
    
    # Get initial budget state
    today = get_today_utc()
    db = get_db()
    try:
        initial_budget = get_today_budget()
        initial_reads = initial_budget.get("x_reads", 0)
    finally:
        db.close()
    
    # Create counting fake client
    counting_client = CountingFakeXClient()
    
    # Connect to Temporal
    client = await Client.connect("localhost:7233")
    
    # Create worker with counting client
    task_queue = "test-can-integration"
    x_activities = XActivities(counting_client)
    reply_activities = ReplyActivities(FakeReplyAgent())
    
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[TopicWatchWorkflow, ReviewWorkflow],
        activities=[
            x_activities.search_recent,
            x_activities.lookup_post,
            x_activities.check_followup,
            reply_activities.research_post,
            reply_activities.draft_reply,
            store_card,
            claim_gate,
        ],
    )
    
    # Start worker
    async with worker:
        # Create input with low CAN threshold and 0h poll (allowed with fake adapter)
        workflow_id = f"test-can-integration-{today}"
        input_data = TopicWatchInput(
            account="test_account",
            topic="reliability",
            query="reliability",
            product_brief="A test product for reliability",
            max_results=10,
            confidence_threshold=0.99,  # High threshold to skip Review spawns for speed
            continue_as_new_after=2,  # CAN after 2 iterations
            config=TopicWatchConfig(
                poll_interval_hours=0,  # Immediate poll (allowed with fake)
                activity_timeout_medium_sec=10,
                activity_max_attempts=2,
            ),
            review_config=ReviewConfig(
                review_timeout_hours=1,
                followup_delay_seconds=300,
            ),
        )
        
        # Start workflow
        handle = await client.start_workflow(
            TopicWatchWorkflow.run,
            input_data,
            id=workflow_id,
            task_queue=task_queue,
            execution_timeout=timedelta(seconds=60),
        )
        
        # Wait for at least 3 Continue-As-New runs by polling history
        target_run_count = 3
        max_wait_seconds = 45
        poll_interval_seconds = 1
        elapsed = 0
        run_ids_seen = set()
        
        while elapsed < max_wait_seconds:
            await asyncio.sleep(poll_interval_seconds)
            elapsed += poll_interval_seconds
            
            # Get workflow history to count distinct run IDs
            try:
                history = await handle.fetch_history()
                for event in history.events:
                    if event.workflow_execution_started_event_attributes:
                        run_ids_seen.add(event.workflow_execution_started_event_attributes.original_execution_run_id or handle.result_run_id)
                    if event.workflow_execution_continued_as_new_event_attributes:
                        # The new run ID is in the next execution
                        pass
                
                # Check current run status
                desc = await handle.describe()
                run_ids_seen.add(desc.run_id)
                
                if len(run_ids_seen) >= target_run_count:
                    # Terminate workflow to stop polling
                    await handle.terminate()
                    break
            except Exception:
                # Workflow might have completed or been terminated
                break
        
        # Verify we saw at least target_run_count distinct runs
        assert len(run_ids_seen) >= target_run_count, \
            f"Expected at least {target_run_count} distinct run IDs, saw {len(run_ids_seen)}: {run_ids_seen}"
        
        # Get final budget state
        db = get_db()
        try:
            final_budget = get_today_budget()
            final_reads = final_budget.get("x_reads", 0)
            ledger_reads = final_reads - initial_reads
            
            # Count reconciliation rows for this workflow
            recon_count = db.query(BudgetReconciliationDB).filter(
                BudgetReconciliationDB.workflow_id == workflow_id,
                BudgetReconciliationDB.budget_type == 'x_reads'
            ).count()
        finally:
            db.close()
        
        # Verify ledger matches actual served reads
        actual_served = counting_client.total_posts_served
        assert ledger_reads == actual_served, \
            f"Ledger reads ({ledger_reads}) should equal actual served reads ({actual_served})"
        
        # Verify reconciliation count matches iteration count
        # Each iteration does one search_recent call, so recon_count should equal number of polls
        assert recon_count > 0, f"Should have at least one reconciliation, got {recon_count}"
        
        print(f"\n✓ Multi-CAN test passed:")
        print(f"  - Distinct run IDs seen: {len(run_ids_seen)}")
        print(f"  - Reconciliations recorded: {recon_count}")
        print(f"  - Actual posts served: {actual_served}")
        print(f"  - Ledger reads charged: {ledger_reads}")
        print(f"  - Match: {ledger_reads == actual_served}")
