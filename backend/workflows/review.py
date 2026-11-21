"""
Review workflow - one per post.

Per ADR-0002:
- Signals: approve, skip
- Updates: edit, nudge, confirm_posted
- Query: get_card
- 48h expiry timeout
- Follow-up timer after confirmation
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, List
import asyncio
import re

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from backend.models import (
        ReviewCard,
        ReviewStatus,
        Post,
        Evidence,
    )
    from backend.activities import (
        ResearchPostInput,
        DraftReplyInput,
        StoreCardInput,
        UpdateCardInput,
        LookupPostInput,
        CheckFollowupInput,
    )
    from backend.activities.claim_gate_activity import ClaimGateInput
    from backend.config import get_settings


@dataclass
class ReviewInput:
    """Input for Review workflow."""
    
    post: Post
    topic: str
    product_brief: str
    account: str
    confidence_threshold: float = 0.6


@workflow.defn(name="Review")
class ReviewWorkflow:
    """
    Review workflow for a single post.
    
    Workflow ID: review:{account}:{post_id}
    
    Steps:
    1. Research the post
    2. Draft a reply
    3. Run claim gate
    4. Wait for human (approve/skip/edit/nudge)
    5. If approved, wait for confirm_posted with reply URL
    6. Start follow-up timer (3 days)
    7. Check for replies and resurface
    """
    
    def __init__(self):
        self.evidence: Optional[Evidence] = None
        self.status = ReviewStatus.PENDING
        self.approved = False
        self.confirmed = False
        self.skipped = False
        self.posted_url: Optional[str] = None
        self.reply_post_id: Optional[str] = None
        self.skip_reason: Optional[str] = None
        self.client_request_id: Optional[str] = None
    
    @workflow.run
    async def run(self, input: ReviewInput) -> str:
        """Run the review workflow."""
        workflow.logger.info(f"Starting review for post {input.post.id}")
        
        settings = get_settings()
        workflow_id = workflow.info().workflow_id
        created_at = workflow.now().isoformat()
        
        try:
            # Step 1: Research
            evidence = await workflow.execute_activity(
                "research_post",
                ResearchPostInput(
                    post=input.post,
                    topic=input.topic,
                    product_brief=input.product_brief,
                    idempotency_key=f"{input.post.id}:research",
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            
            workflow.logger.info(f"Research complete: confidence={evidence.confidence_score:.2f}")
            
            # Skip if low confidence
            if evidence.confidence_score < input.confidence_threshold:
                self.status = ReviewStatus.SKIPPED
                self.skip_reason = f"Low confidence: {evidence.confidence_score:.2f} < {input.confidence_threshold}"
                workflow.logger.info(f"Skipped: {self.skip_reason}")
                
                card = ReviewCard(
                    workflow_id=workflow_id,
                    post=input.post,
                    evidence=evidence,
                    status=self.status,
                    created_at=created_at,
                    updated_at=workflow.now().isoformat(),
                    skip_reason=self.skip_reason,
                )
                
                await workflow.execute_activity(
                    "store_card",
                    StoreCardInput(card=card, idempotency_key=f"{workflow_id}:store:skip"),
                    start_to_close_timeout=timedelta(seconds=10),
                )
                
                return "skipped"
            
            # Step 2: Draft (with claim gate retry logic)
            claim_gate_attempts = 0
            max_claim_gate_attempts = 2
            
            while claim_gate_attempts < max_claim_gate_attempts:
                claim_gate_attempts += 1
                
                # Draft reply
                steer = "Try again with only supported claims" if claim_gate_attempts > 1 else None
                evidence = await workflow.execute_activity(
                    "draft_reply",
                    DraftReplyInput(
                        evidence=evidence,
                        steer=steer,
                        idempotency_key=f"{input.post.id}:draft:attempt{claim_gate_attempts}",
                    ),
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                
                # Run deterministic claim gate
                if evidence.draft:
                    claim_gate_result = await workflow.execute_activity(
                        "claim_gate",
                        ClaimGateInput(
                            draft_text=evidence.draft.text,
                            founder_materials=evidence.founder_materials,
                            idempotency_key=f"{input.post.id}:gate:attempt{claim_gate_attempts}",
                        ),
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                    
                    # Update evidence with gate result
                    evidence.draft.claim_gate = claim_gate_result
                    
                    if claim_gate_result.passed:
                        workflow.logger.info(f"Claim gate passed on attempt {claim_gate_attempts}")
                        break
                    else:
                        workflow.logger.warning(
                            f"Claim gate failed on attempt {claim_gate_attempts}: "
                            f"{len(claim_gate_result.unsupported_claims)} unsupported claims"
                        )
                else:
                    workflow.logger.error("No draft generated")
                    break
            
            # Skip if claim gate failed after retries
            if not evidence.draft or not evidence.draft.claim_gate.passed:
                self.status = ReviewStatus.SKIPPED
                self.skip_reason = "Not enough evidence - claim gate failed after regeneration"
                workflow.logger.info(f"Skipped: {self.skip_reason}")
                
                card = ReviewCard(
                    workflow_id=workflow_id,
                    post=input.post,
                    evidence=evidence,
                    status=self.status,
                    created_at=created_at,
                    updated_at=workflow.now().isoformat(),
                    skip_reason=self.skip_reason,
                )
                
                await workflow.execute_activity(
                    "store_card",
                    StoreCardInput(card=card, idempotency_key=f"{workflow_id}:store:gate_fail"),
                    start_to_close_timeout=timedelta(seconds=10),
                )
                
                return "skipped"
            
            self.evidence = evidence
            
            # Store pending card
            self.evidence = evidence
            card = ReviewCard(
                workflow_id=workflow_id,
                post=input.post,
                evidence=evidence,
                status=self.status,
                created_at=created_at,
                updated_at=workflow.now().isoformat(),
            )
            
            await workflow.execute_activity(
                "store_card",
                StoreCardInput(card=card, idempotency_key=f"{workflow_id}:store:pending"),
                start_to_close_timeout=timedelta(seconds=10),
            )
            
            # Step 3: Wait for human decision
            timeout_hours = settings.review_timeout_hours
            workflow.logger.info(f"Waiting for human decision ({timeout_hours}h timeout)")
            
            try:
                await workflow.wait_condition(
                    lambda: self.approved or self.skipped,
                    timeout=timedelta(hours=timeout_hours),
                )
            except asyncio.TimeoutError:
                self.status = ReviewStatus.EXPIRED
                workflow.logger.info(f"Review expired after {timeout_hours}h")
                
                await workflow.execute_activity(
                    "update_card",
                    UpdateCardInput(
                        workflow_id=workflow_id,
                        updates={"status": ReviewStatus.EXPIRED.value},
                        idempotency_key=f"{workflow_id}:update:expire",
                    ),
                    start_to_close_timeout=timedelta(seconds=10),
                )
                
                return "expired"
            
            # Handle skip
            if self.skipped:
                workflow.logger.info(f"Skipped by founder")
                
                await workflow.execute_activity(
                    "update_card",
                    UpdateCardInput(
                        workflow_id=workflow_id,
                        updates={
                            "status": ReviewStatus.SKIPPED.value,
                            "skip_reason": self.skip_reason,
                        },
                        idempotency_key=f"{workflow_id}:update:skip",
                    ),
                    start_to_close_timeout=timedelta(seconds=10),
                )
                
                return "skipped"
            
            # Handle approve - wait for confirmation
            workflow.logger.info(f"Approved by founder")
            
            # Update to approved status
            await workflow.execute_activity(
                "update_card",
                UpdateCardInput(
                    workflow_id=workflow_id,
                    updates={
                        "status": ReviewStatus.APPROVED.value,
                        "posted_url": self.posted_url,
                    },
                    idempotency_key=f"{workflow_id}:update:approve",
                ),
                start_to_close_timeout=timedelta(seconds=10),
            )
            
            # Step 4: Wait for confirm_posted
            workflow.logger.info(f"Waiting for reply URL confirmation ({timeout_hours}h timeout)")
            
            try:
                await workflow.wait_condition(
                    lambda: self.confirmed,
                    timeout=timedelta(hours=timeout_hours),
                )
            except asyncio.TimeoutError:
                self.status = ReviewStatus.APPROVED_NOT_CONFIRMED
                workflow.logger.info(f"Approved but not confirmed after {timeout_hours}h")
                
                await workflow.execute_activity(
                    "update_card",
                    UpdateCardInput(
                        workflow_id=workflow_id,
                        updates={"status": ReviewStatus.APPROVED_NOT_CONFIRMED.value},
                        idempotency_key=f"{workflow_id}:update:not_confirmed",
                    ),
                    start_to_close_timeout=timedelta(seconds=10),
                )
                
                return "approved_not_confirmed"
            
            # Step 5: Start follow-up timer (from config)
            followup_delay = settings.followup_delay_days
            workflow.logger.info(f"Confirmed reply {self.reply_post_id}, starting {followup_delay}d follow-up timer")
            
            await workflow.sleep(timedelta(days=followup_delay))
            
            # Step 6: Check for follow-up replies (capped)
            workflow.logger.info("Follow-up timer fired, checking for replies")
            
            followup_posts = await workflow.execute_activity(
                "check_followup",
                CheckFollowupInput(
                    reply_post_id=self.reply_post_id,
                    idempotency_key=f"{workflow_id}:followup",
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            
            workflow.logger.info(f"Found {len(followup_posts)} follow-up replies")
            
            # Update card with followup_due status and replies (no auto-draft)
            followup_replies = [
                {
                    "id": p.id,
                    "text": p.text,
                    "author": p.author.username,
                    "url": p.url,
                }
                for p in followup_posts
            ]
            
            await workflow.execute_activity(
                "update_card",
                UpdateCardInput(
                    workflow_id=workflow_id,
                    updates={
                        "status": ReviewStatus.FOLLOWUP_DUE.value,
                        "followup_replies": followup_replies,
                    },
                    idempotency_key=f"{workflow_id}:update:followup_due",
                ),
                start_to_close_timeout=timedelta(seconds=10),
            )
            
            return "followup_due"
        
        except Exception as e:
            workflow.logger.error(f"Review failed: {e}")
            raise
    
    @workflow.signal
    def approve(self, posted_url: str, client_request_id: str):
        """
        Approve signal - founder will post this.
        
        Per ADR-0004: approve just yields the copy text.
        The founder posts manually and then calls confirm_posted.
        """
        if self.client_request_id == client_request_id:
            workflow.logger.info(f"Duplicate approve signal: {client_request_id}")
            return
        
        workflow.logger.info(f"Received approve signal: {posted_url}")
        self.approved = True
        self.posted_url = posted_url
        self.client_request_id = client_request_id
    
    @workflow.signal
    def skip(self, reason: str, client_request_id: str):
        """Skip signal - founder declines to post."""
        if self.client_request_id == client_request_id:
            workflow.logger.info(f"Duplicate skip signal: {client_request_id}")
            return
        
        workflow.logger.info(f"Received skip signal: {reason}")
        self.skipped = True
        self.skip_reason = reason
        self.client_request_id = client_request_id
    
    @workflow.update
    async def confirm_posted(self, reply_url: str, client_request_id: str) -> str:
        """
        Confirm posted Update - verify the reply was actually posted.
        
        Parses reply post ID from URL, verifies it's a reply to our target post.
        Returns success message or raises error if invalid.
        """
        workflow.logger.info(f"Received confirm_posted: {reply_url}")
        
        # Parse reply post ID from URL
        # Expected format: https://x.com/username/status/1234567890
        match = re.search(r'/status/(\d+)', reply_url)
        if not match:
            raise ValueError(f"Invalid X post URL: {reply_url}")
        
        reply_post_id = match.group(1)
        
        # Lookup the reply post to verify it exists
        reply_post = await workflow.execute_activity(
            "lookup_post",
            LookupPostInput(
                post_id=reply_post_id,
                idempotency_key=f"{workflow.info().workflow_id}:confirm:{reply_post_id}",
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        
        if not reply_post:
            raise ValueError(f"Reply post {reply_post_id} not found")
        
        # Verify it's a reply to our target post
        # For real X API, check in_reply_to_user_id or conversation_id
        # For fake client, check conversation_id matches
        if reply_post.conversation_id != self.evidence.post_id:
            raise ValueError(
                f"Post {reply_post_id} (conversation_id={reply_post.conversation_id}) "
                f"is not a reply to target post {self.evidence.post_id}"
            )
        
        workflow.logger.info(f"Confirmed reply {reply_post_id} is valid")
        
        self.confirmed = True
        self.reply_post_id = reply_post_id
        
        # Update card with reply_post_id
        await workflow.execute_activity(
            "update_card",
            UpdateCardInput(
                workflow_id=workflow.info().workflow_id,
                updates={"reply_post_id": reply_post_id},
                idempotency_key=f"{workflow.info().workflow_id}:update:reply_id:{client_request_id}",
            ),
            start_to_close_timeout=timedelta(seconds=10),
        )
        
        return f"Confirmed reply {reply_post_id}"
    
    @workflow.update
    async def edit(self, new_text: str, client_request_id: str) -> str:
        """Edit update - founder edits the draft."""
        workflow.logger.info(f"Received edit update: {len(new_text)} chars")
        
        if not self.evidence or not self.evidence.draft:
            raise ValueError("No draft to edit")
        
        self.evidence.draft.text = new_text
        self.evidence.draft.steers_applied.append("manual_edit")
        
        workflow_id = workflow.info().workflow_id
        
        await workflow.execute_activity(
            "update_card",
            UpdateCardInput(
                workflow_id=workflow_id,
                updates={"evidence": self.evidence},
                idempotency_key=f"{workflow_id}:update:edit:{client_request_id}",
            ),
            start_to_close_timeout=timedelta(seconds=10),
        )
        
        return self.evidence.draft.text
    
    @workflow.update
    async def nudge(self, instruction: str, client_request_id: str) -> str:
        """Nudge update - steer a redraft."""
        workflow.logger.info(f"Received nudge update: {instruction}")
        
        if not self.evidence:
            raise ValueError("No evidence to nudge")
        
        self.evidence = await workflow.execute_activity(
            "draft_reply",
            DraftReplyInput(
                evidence=self.evidence,
                steer=instruction,
                idempotency_key=f"{workflow.info().workflow_id}:nudge:{client_request_id}",
            ),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        
        workflow_id = workflow.info().workflow_id
        
        await workflow.execute_activity(
            "update_card",
            UpdateCardInput(
                workflow_id=workflow_id,
                updates={"evidence": self.evidence},
                idempotency_key=f"{workflow_id}:update:nudge:{client_request_id}",
            ),
            start_to_close_timeout=timedelta(seconds=10),
        )
        
        return self.evidence.draft.text if self.evidence.draft else ""
    
    @workflow.query
    def get_card(self) -> dict:
        """Query to get the current card state."""
        if not self.evidence:
            return {"status": self.status.value, "evidence": None}
        
        return {
            "status": self.status.value,
            "evidence": {
                "post_id": self.evidence.post_id,
                "matched_topic": self.evidence.matched_topic,
                "confidence_score": self.evidence.confidence_score,
                "draft": {
                    "text": self.evidence.draft.text,
                    "claim_gate_passed": self.evidence.draft.claim_gate.passed,
                } if self.evidence.draft else None,
            },
        }
