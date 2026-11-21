"""
Review workflow - one per post.

Per ADR-0002:
- Signals: approve, skip
- Updates: edit, nudge
- Query: get_card
- 48h expiry timeout
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
import asyncio

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
        research_post,
        draft_reply,
        store_card,
        update_card,
        ResearchPostInput,
        DraftReplyInput,
        StoreCardInput,
        UpdateCardInput,
    )


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
    5. Expire after 48h
    """
    
    def __init__(self):
        self.evidence: Optional[Evidence] = None
        self.status = ReviewStatus.PENDING
        self.approved = False
        self.skipped = False
        self.posted_url: Optional[str] = None
        self.skip_reason: Optional[str] = None
        self.client_request_id: Optional[str] = None
    
    @workflow.run
    async def run(self, input: ReviewInput) -> str:
        """Run the review workflow."""
        workflow.logger.info(f"Starting review for post {input.post.id}")
        
        workflow_id = workflow.info().workflow_id
        created_at = workflow.now().isoformat()
        
        try:
            evidence = await workflow.execute_activity(
                research_post,
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
                    store_card,
                    StoreCardInput(card=card, idempotency_key=f"{workflow_id}:store:skip"),
                    start_to_close_timeout=timedelta(seconds=10),
                )
                
                return "skipped"
            
            evidence = await workflow.execute_activity(
                draft_reply,
                DraftReplyInput(
                    evidence=evidence,
                    idempotency_key=f"{input.post.id}:draft:initial",
                ),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            
            if not evidence.draft or not evidence.draft.claim_gate.passed:
                self.status = ReviewStatus.SKIPPED
                self.skip_reason = "Claim gate failed"
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
                    store_card,
                    StoreCardInput(card=card, idempotency_key=f"{workflow_id}:store:gate_fail"),
                    start_to_close_timeout=timedelta(seconds=10),
                )
                
                return "skipped"
            
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
                store_card,
                StoreCardInput(card=card, idempotency_key=f"{workflow_id}:store:pending"),
                start_to_close_timeout=timedelta(seconds=10),
            )
            
            workflow.logger.info("Waiting for human decision (48h timeout)")
            
            try:
                await workflow.wait_condition(
                    lambda: self.approved or self.skipped,
                    timeout=timedelta(hours=48),
                )
            except asyncio.TimeoutError:
                self.status = ReviewStatus.EXPIRED
                workflow.logger.info("Review expired after 48h")
                
                await workflow.execute_activity(
                    update_card,
                    UpdateCardInput(
                        workflow_id=workflow_id,
                        updates={"status": ReviewStatus.EXPIRED.value},
                        idempotency_key=f"{workflow_id}:update:expire",
                    ),
                    start_to_close_timeout=timedelta(seconds=10),
                )
                
                return "expired"
            
            if self.approved:
                self.status = ReviewStatus.APPROVED
                workflow.logger.info(f"Approved by founder, posted_url={self.posted_url}")
                
                await workflow.execute_activity(
                    update_card,
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
                
                return "approved"
            else:
                workflow.logger.info(f"Skipped by founder: {self.skip_reason}")
                
                await workflow.execute_activity(
                    update_card,
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
        
        except Exception as e:
            workflow.logger.error(f"Review failed: {e}")
            raise
    
    @workflow.signal
    def approve(self, posted_url: str, client_request_id: str):
        """Approve signal - founder will post this."""
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
    async def edit(self, new_text: str, client_request_id: str) -> str:
        """Edit update - founder edits the draft."""
        workflow.logger.info(f"Received edit update: {len(new_text)} chars")
        
        if not self.evidence or not self.evidence.draft:
            raise ValueError("No draft to edit")
        
        self.evidence.draft.text = new_text
        self.evidence.draft.steers_applied.append("manual_edit")
        
        workflow_id = workflow.info().workflow_id
        
        await workflow.execute_activity(
            update_card,
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
            draft_reply,
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
            update_card,
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
