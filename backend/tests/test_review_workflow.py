"""
Replay tests for Review workflow.

Per ADR-0003: Temporal replay tests catch non-determinism.
Uses fake adapters so no API keys needed.
"""

import pytest
import uuid
from datetime import datetime, timedelta

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.workflows.review import ReviewWorkflow, ReviewInput
from backend.activities import (
    research_post,
    draft_reply,
    store_card,
    update_card,
)


@pytest.mark.asyncio
async def test_review_workflow_approve(sample_post, product_brief):
    """Test Review workflow with approval."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[research_post, draft_reply, store_card, update_card],
        ):
            workflow_id = f"review:test:{sample_post.id}"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                ReviewInput(
                    post=sample_post,
                    topic="reliability",
                    product_brief=product_brief,
                    account="test",
                    confidence_threshold=0.5,
                ),
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            card = await handle.query(ReviewWorkflow.get_card)
            assert card["status"] == "pending"
            assert card["evidence"]["post_id"] == sample_post.id
            
            client_request_id = str(uuid.uuid4())
            await handle.signal(
                "approve",
                "https://x.com/test/status/999",
                client_request_id,
            )
            
            result = await handle.result()
            assert result == "approved"


@pytest.mark.asyncio
async def test_review_workflow_skip(sample_post, product_brief):
    """Test Review workflow with skip."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[research_post, draft_reply, store_card, update_card],
        ):
            workflow_id = f"review:test:{sample_post.id}_skip"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                ReviewInput(
                    post=sample_post,
                    topic="reliability",
                    product_brief=product_brief,
                    account="test",
                    confidence_threshold=0.5,
                ),
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            client_request_id = str(uuid.uuid4())
            await handle.signal(
                "skip",
                "Not relevant",
                client_request_id,
            )
            
            result = await handle.result()
            assert result == "skipped"


@pytest.mark.asyncio
async def test_review_workflow_nudge(sample_post, product_brief):
    """Test Review workflow with nudge (redraft)."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[research_post, draft_reply, store_card, update_card],
        ):
            workflow_id = f"review:test:{sample_post.id}_nudge"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                ReviewInput(
                    post=sample_post,
                    topic="reliability",
                    product_brief=product_brief,
                    account="test",
                    confidence_threshold=0.5,
                ),
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            client_request_id = str(uuid.uuid4())
            new_draft = await handle.execute_update(
                "nudge",
                "Make it shorter",
                client_request_id,
            )
            
            assert len(new_draft) > 0
            
            client_request_id = str(uuid.uuid4())
            await handle.signal(
                "approve",
                "https://x.com/test/status/999",
                client_request_id,
            )
            
            result = await handle.result()
            assert result == "approved"


@pytest.mark.asyncio
async def test_review_workflow_timeout(sample_post, product_brief):
    """Test Review workflow expires after 48h."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[research_post, draft_reply, store_card, update_card],
        ):
            workflow_id = f"review:test:{sample_post.id}_timeout"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                ReviewInput(
                    post=sample_post,
                    topic="reliability",
                    product_brief=product_brief,
                    account="test",
                    confidence_threshold=0.5,
                ),
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(timedelta(hours=49))
            
            result = await handle.result()
            assert result == "expired"


@pytest.mark.asyncio
async def test_review_workflow_low_confidence(sample_post, product_brief):
    """Test Review workflow skips on low confidence."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[research_post, draft_reply, store_card, update_card],
        ):
            workflow_id = f"review:test:{sample_post.id}_low_conf"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                ReviewInput(
                    post=sample_post,
                    topic="reliability",
                    product_brief=product_brief,
                    account="test",
                    confidence_threshold=0.95,
                ),
                id=workflow_id,
                task_queue="test",
            )
            
            result = await handle.result()
            assert result == "skipped"
