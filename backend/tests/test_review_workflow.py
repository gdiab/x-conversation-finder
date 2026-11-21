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

from backend.workflows.review import ReviewWorkflow, ReviewInput, ReviewConfig
from backend.activities.x_activities import XActivities
from backend.activities.reply_activities import ReplyActivities
from backend.activities.store_activities import store_card, update_card
from backend.activities.claim_gate_activity import claim_gate
from backend.adapters import FakeXClient, FakeReplyAgent


@pytest.mark.asyncio
async def test_review_workflow_approve(make_review_input, x_activities, reply_activities):
    """Test Review workflow with approval."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            card = await handle.query("get_card")
            assert card["status"] == "pending"
            assert card["evidence"]["post_id"] == review_input.post.id
            
            # Approve
            client_request_id = str(uuid.uuid4())
            await handle.signal(
                "approve",
                args=["https://x.com/test/status/999", client_request_id]
            )
            
            await env.sleep(1)
            
            # Confirm posted with reply URL (different post ID, same conversation)
            client_request_id = str(uuid.uuid4())
            reply_msg = await handle.execute_update(
                "confirm_posted",
                args=["https://x.com/founder/status/9999", client_request_id]
            )
            assert "Confirmed" in reply_msg
            
            # Skip ahead 3 days for follow-up
            await env.sleep(timedelta(days=3, hours=1))
            
            result = await handle.result()
            assert result == "followup_due"


@pytest.mark.asyncio
async def test_review_workflow_skip(make_review_input, x_activities, reply_activities):
    """Test Review workflow with skip."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_skip"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            client_request_id = str(uuid.uuid4())
            await handle.signal(
                "skip",
                args=["Not relevant", client_request_id]
            )
            
            result = await handle.result()
            assert result == "skipped"


@pytest.mark.asyncio
async def test_review_workflow_nudge(make_review_input, x_activities, reply_activities):
    """Test Review workflow with nudge (redraft)."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_nudge"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            client_request_id = str(uuid.uuid4())
            new_draft = await handle.execute_update(
                "nudge",
                args=["Make it shorter", client_request_id]
            )
            
            assert len(new_draft) > 0
            
            # Approve
            client_request_id = str(uuid.uuid4())
            await handle.signal(
                "approve",
                args=["https://x.com/test/status/999", client_request_id]
            )
            
            await env.sleep(1)
            
            # Confirm posted
            client_request_id = str(uuid.uuid4())
            await handle.execute_update(
                "confirm_posted",
                args=["https://x.com/test/status/1001", client_request_id]
            )
            
            # Skip ahead for follow-up
            await env.sleep(timedelta(days=3, hours=1))
            
            result = await handle.result()
            assert result == "followup_due"


@pytest.mark.asyncio
async def test_review_workflow_timeout(make_review_input, x_activities, reply_activities):
    """Test Review workflow expires after 48h."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_timeout"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(timedelta(hours=49))
            
            result = await handle.result()
            assert result == "expired"


@pytest.mark.asyncio
async def test_review_workflow_low_confidence(make_review_input, x_activities, reply_activities):
    """Test Review workflow skips on low confidence."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input(confidence_threshold=0.95)
            workflow_id = f"review:test:{review_input.post.id}_low_conf"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            result = await handle.result()
            assert result == "skipped"


@pytest.mark.asyncio
async def test_review_workflow_approved_not_confirmed(make_review_input, x_activities, reply_activities):
    """Test Review workflow expires to approved_not_confirmed if no confirmation."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_not_confirmed"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            # Approve but don't confirm
            client_request_id = str(uuid.uuid4())
            await handle.signal(
                "approve",
                args=["https://x.com/test/status/999", client_request_id]
            )
            
            # Skip ahead 48h without confirming
            await env.sleep(timedelta(hours=49))
            
            result = await handle.result()
            assert result == "approved_not_confirmed"


@pytest.mark.asyncio
async def test_review_workflow_confirm_posted_invalid_url(make_review_input, x_activities, reply_activities):
    """Test confirm_posted rejects invalid URL."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_invalid_url"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            # Approve
            client_request_id = str(uuid.uuid4())
            await handle.signal(
                "approve",
                args=["https://x.com/test/status/999", client_request_id]
            )
            
            await env.sleep(1)
            
            # Try to confirm with invalid URL
            client_request_id = str(uuid.uuid4())
            from temporalio.client import WorkflowUpdateFailedError
            try:
                await handle.execute_update(
                    "confirm_posted",
                    args=["https://invalid-url.com/foo", client_request_id]
                )
                assert False, "Should have raised WorkflowUpdateFailedError"
            except WorkflowUpdateFailedError as e:
                # Validator should reject bad URL
                error_msg = str(e)
                if e.__cause__:
                    error_msg += " " + str(e.__cause__)
                assert "Invalid X post URL" in error_msg


@pytest.mark.asyncio
async def test_review_workflow_duplicate_approve_ignored(make_review_input, x_activities, reply_activities):
    """Test that duplicate approve requests with same request_id are ignored."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_dup_approve"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            # Approve with first request ID
            request_id = str(uuid.uuid4())
            await handle.signal(
                "approve",
                args=["https://x.com/test/status/999", request_id]
            )
            
            await env.sleep(1)
            
            # Try to approve again with same request ID (should be idempotent)
            await handle.signal(
                "approve",
                args=["https://x.com/test/status/999", request_id]
            )
            
            await env.sleep(1)
            
            # Query the card to verify it's approved
            card = await handle.query("get_card")
            assert card["status"] == "approved"


@pytest.mark.asyncio
async def test_review_workflow_claim_gate_regenerate_then_skip(make_review_input, x_activities):
    """
    Test claim gate failure triggers regenerate, then skip after max attempts.
    
    Per R3-B2: This test uses the REAL gate activity with a fake reply agent 
    that generates unsupported claims (not a stub gate).
    """
    from backend.activities.claim_gate_activity import claim_gate
    from backend.ports import ReplyAgentPort
    from backend.activities.reply_activities import ReplyActivities
    from backend.models import Evidence, FounderMaterial, Claim, ClaimGateResult, Draft
    from typing import Optional
    
    class BadClaimReplyAgent(ReplyAgentPort):
        """Fake agent that generates unsupported claims for testing."""
        
        async def research(self, post, topic, product_brief, idempotency_key) -> Evidence:
            """Generate fake evidence."""
            evidence = Evidence(
                post_id=post.id,
                matched_topic=topic,
                why_it_fits="Post discusses relevant topics.",
                confidence_score=0.85,
                confidence_inputs={"topic_match_strength": 0.9},
                founder_materials=[
                    FounderMaterial(
                        source="product_brief",
                        passage="Our platform is designed for reliability.",
                        relevance="high",
                        passage_id="passage_0",
                    )
                ],
            )
            return evidence
        
        async def draft(self, evidence, steer=None, idempotency_key=None) -> Evidence:
            """Generate draft with unsupported claim (99.99% uptime guaranteed)."""
            from backend.models import Segment
            
            # Draft contains risky words not backed by founder materials
            segments = [
                Segment(
                    text="We guarantee 99.99% uptime with zero downtime for your critical applications.",
                    kind="claim",
                    source_passage_id="passage_0",
                    quote="reliability"  # Quote exists but doesn't support the numbers
                )
            ]
            draft_text = ' '.join(s.text for s in segments)
            
            unsupported_claim = Claim(
                text="We guarantee 99.99% uptime with zero downtime",
                cited_passage_ids=["passage_0"],
                quoted_spans=["reliability"],  # Spans exist but don't support the numbers
            )
            
            evidence.draft = Draft(
                text=draft_text,
                segments=segments,
                claim_gate=ClaimGateResult(
                    passed=True,
                    segments=segments,
                    failed_sentences=[],
                    reasoning="Gate not yet run",
                    claims=[unsupported_claim],
                    failed_claims=[],
                ),
                steers_applied=[steer] if steer else [],
                token_count=len(draft_text.split()),
                model_used="bad-claim-agent",
            )
            return evidence
    
    bad_agent = BadClaimReplyAgent()
    bad_reply_activities = ReplyActivities(bad_agent)
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                bad_reply_activities.research_post,
                bad_reply_activities.draft_reply,
                claim_gate,  # Use REAL claim gate (R3-B2)
                store_card,
                update_card,
                x_activities.lookup_post,
                x_activities.check_followup,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_claim_fail"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            # Wait for workflow to complete (should skip after 2 failed attempts)
            result = await handle.result()
            assert result == "skipped"
            
            # Query the card to verify final status
            card = await handle.query("get_card")
            assert card["status"] == "skipped"
            # New skip reason format per ADR-0006 Amendment: includes per-sentence failures
            assert "not enough evidence" in card["skip_reason"].lower()
            assert "insufficient support" in card["skip_reason"].lower()
            
            # Gap 2: Verify ALL failed sentences are recorded in evidence, not just summary
            assert card["evidence"]["draft"] is not None
            failed_sentences = card["evidence"]["draft"]["claim_gate"]["failed_sentences"]
            assert len(failed_sentences) == 2  # Both sentences failed (split by sentence splitter)
            # Verify all failures are recorded with reasons (not truncated to display summary)
            for failure in failed_sentences:
                assert "sentence" in failure
                assert "reason" in failure
                assert len(failure["sentence"]) > 0
                assert "insufficient support" in failure["reason"].lower()
            # Verify this is more than what's in skip_reason (which is capped at 3 with 30 char limit)
            assert card["skip_reason"] is not None


@pytest.mark.asyncio
async def test_review_workflow_confirm_posted_with_post_1005(x_activities, reply_activities, product_brief):
    """Test confirm_posted works with post 1005 (not just 1001)."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.search_recent,
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            # Use post 1005 specifically
            from backend.models import Post, Author
            post = Post(
                id="1005",
                text="Enterprise reliability challenges and solutions.",
                author=Author(
                    id="author_1005",
                    username="enterprise_dev",
                    name="Enterprise Dev",
                    followers_count=30000,
                    verified=True,
                ),
                created_at="2026-11-21T00:00:00Z",
                like_count=100,
                reply_count=20,
                repost_count=50,
                quote_count=10,
                conversation_id="1005",
                in_reply_to_user_id=None,
                reply_settings="everyone",
                url="https://x.com/enterprise_dev/status/1005",
            )
            
            review_input = ReviewInput(
                post=post,
                account="test",
                topic="reliability",
                product_brief=product_brief,
                config=ReviewConfig(
                    review_timeout_hours=1,
                    followup_delay_days=3,
                    followup_delay_seconds=8,
                    activity_timeout_medium_sec=30,
                    activity_timeout_long_sec=60,
                    activity_max_attempts=2,
                    claim_gate_max_attempts=2,
                ),
            )
            
            workflow_id = "review:test:1005_confirm"
            
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            
            await env.sleep(1)
            
            # Approve
            client_request_id = str(uuid.uuid4())
            await handle.signal(
                "approve",
                args=["https://x.com/test/status/999", client_request_id]
            )
            
            await env.sleep(1)
            
            # Confirm posted with a reply ID > 1001 (fake client maps to conversation 1005)
            client_request_id = str(uuid.uuid4())
            await handle.execute_update(
                "confirm_posted",
                args=["https://x.com/founder/status/10005", client_request_id]
            )
            
            # Should work without error - fake client maps post 10005 to conversation 1005
