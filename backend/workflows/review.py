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
from datetime import timedelta
from typing import Optional, List
import asyncio
import re

from temporalio import workflow
from temporalio.common import RetryPolicy

# Constants
SECONDS_PER_DAY = 86400
QUICK_ACTIVITY_TIMEOUT_SEC = 10

with workflow.unsafe.imports_passed_through():
    from backend.models import (
        ReviewCard,
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
    from backend.activities.claim_gate_activity import ClaimGateInput, claim_gate
    from backend.activities.store_activities import store_card, update_card


@dataclass(frozen=True)
class ReviewConfig:
    """Workflow config passed through input for determinism."""
    activity_timeout_short_sec: int = 10
    activity_timeout_medium_sec: int = 30
    activity_timeout_long_sec: int = 60
    claim_gate_max_attempts: int = 2
    followup_delay_days: int = 3
    followup_delay_seconds: Optional[int] = None  # Override for E2E testing
    activity_max_attempts: int = 3
    review_timeout_hours: int = 48
    
    @staticmethod
    def from_settings(settings):
        """Create ReviewConfig from Settings."""
        return ReviewConfig(
            activity_timeout_short_sec=settings.activity_timeout_short_sec,
            activity_timeout_medium_sec=settings.activity_timeout_medium_sec,
            activity_timeout_long_sec=settings.activity_timeout_long_sec,
            claim_gate_max_attempts=settings.claim_gate_max_attempts,
            followup_delay_days=settings.followup_delay_days,
            followup_delay_seconds=settings.followup_delay_seconds,
            activity_max_attempts=settings.activity_max_attempts,
            review_timeout_hours=settings.review_timeout_hours,
        )


@dataclass
class ReviewInput:
    """Input for Review workflow."""
    
    post: Post
    topic: str
    product_brief: str
    account: str
    confidence_threshold: float = 0.6
    config: ReviewConfig = None
    
    def __post_init__(self):
        """Default config if not provided."""
        if self.config is None:
            object.__setattr__(self, 'config', ReviewConfig())


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
        # R4-NN5: Store workflow metadata for get_card query
        self.workflow_id: Optional[str] = None
        self.created_at: Optional[str] = None
        # B9: Separate dedupe sets per handler
        self._approve_requests: set[str] = set()
        self._skip_requests: set[str] = set()
        self._edit_requests: set[str] = set()
        self._confirm_requests: set[str] = set()
        self._nudge_requests: set[str] = set()

    def _build_card(self, workflow_id: str, post: Post, created_at: str) -> ReviewCard:
        """Helper to build a ReviewCard from current state (R4-B1: convert enum to str)."""
        return ReviewCard(
            workflow_id=workflow_id,
            post=post,
            evidence=self.evidence,
            status=self.status.value,  # R4-B1: Convert enum to plain str at boundary
            created_at=created_at,
            updated_at=workflow.now().isoformat(),
            skip_reason=self.skip_reason if self.skipped else None,
            posted_url=self.posted_url,
            reply_post_id=self.reply_post_id,
        )

    async def _research_post(self, workflow_id: str, created_at: str) -> Optional[Evidence]:
        """Research post and return evidence, or None if skipped for low confidence."""
        input = self.input
        
        evidence = await workflow.execute_activity(
            "research_post",
            ResearchPostInput(
                post=input.post,
                topic=input.topic,
                product_brief=input.product_brief,
                idempotency_key=f"{input.post.id}:research",
            ),
            start_to_close_timeout=timedelta(seconds=input.config.activity_timeout_medium_sec),
            retry_policy=RetryPolicy(maximum_attempts=input.config.activity_max_attempts),
            result_type=Evidence,
        )
        
        workflow.logger.info(f"Research complete: confidence={evidence.confidence_score:.2f}")
        
        # Skip if low confidence
        if evidence.confidence_score < input.confidence_threshold:
            self.status = ReviewStatus.SKIPPED
            self.skip_reason = f"Low confidence: {evidence.confidence_score:.2f} < {input.confidence_threshold}"
            self.evidence = evidence
            workflow.logger.info(f"Skipped: low confidence {evidence.confidence_score:.2f}")
            
            card = self._build_card(workflow_id, input.post, created_at)
            
            await workflow.execute_activity(
                store_card,
                StoreCardInput(card=card, idempotency_key=f"{workflow_id}:store:skip"),
                start_to_close_timeout=timedelta(seconds=input.config.activity_timeout_short_sec),
            )
            
            return None
        
        return evidence

    async def _draft_and_validate(self, evidence: Evidence, workflow_id: str, created_at: str) -> Optional[Evidence]:
        """Draft reply with claim gate validation retries. Returns None if validation fails."""
        input = self.input
        claim_gate_attempts = 0
        max_claim_gate_attempts = input.config.claim_gate_max_attempts
        
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
                start_to_close_timeout=timedelta(seconds=input.config.activity_timeout_long_sec),
                retry_policy=RetryPolicy(maximum_attempts=input.config.activity_max_attempts),
                result_type=Evidence,
            )
            
            # Run deterministic claim gate (R3-B1: scans draft.text itself)
            if evidence.draft:
                claim_gate_result = await workflow.execute_activity(
                    claim_gate,
                    ClaimGateInput(
                        draft_text=evidence.draft.text,
                        claims=evidence.draft.claim_gate.claims,
                        founder_materials=evidence.founder_materials,
                        idempotency_key=f"{input.post.id}:gate:attempt{claim_gate_attempts}",
                    ),
                    start_to_close_timeout=timedelta(seconds=input.config.activity_timeout_short_sec),
                )
                
                # Update evidence with gate result
                evidence.draft.claim_gate = claim_gate_result
                
                if claim_gate_result.passed:
                    workflow.logger.info(f"Claim gate passed on attempt {claim_gate_attempts}")
                    return evidence
                else:
                    workflow.logger.warning(
                        f"Claim gate failed on attempt {claim_gate_attempts}: "
                        f"{len(claim_gate_result.failed_claims)} failed claims"
                    )
            else:
                workflow.logger.error("No draft generated")
                break
        
        # Skip if claim gate failed after retries (R4-NN5: set skipped flag)
        self.status = ReviewStatus.SKIPPED
        self.skipped = True
        self.skip_reason = "Not enough evidence - claim gate failed after regeneration"
        self.evidence = evidence
        workflow.logger.info(f"Skipped: claim gate failed after {claim_gate_attempts} attempts")
        
        card = self._build_card(workflow_id, input.post, created_at)
        
        await workflow.execute_activity(
            store_card,
            StoreCardInput(card=card, idempotency_key=f"{workflow_id}:store:gate_fail"),
            start_to_close_timeout=timedelta(seconds=input.config.activity_timeout_short_sec),
        )
        
        return None

    async def _wait_for_decision(self, workflow_id: str) -> Optional[str]:
        """Wait for human decision (approve/skip). Returns None if timed out."""
        input = self.input
        timeout_hours = input.config.review_timeout_hours
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
                update_card,
                UpdateCardInput(
                    workflow_id=workflow_id,
                    updates={"status": ReviewStatus.EXPIRED.value},
                    idempotency_key=f"{workflow_id}:update:expire",
                ),
                start_to_close_timeout=timedelta(seconds=QUICK_ACTIVITY_TIMEOUT_SEC),
            )
            
            return "expired"
        
        if self.skipped:
            workflow.logger.info("Skipped by founder")
            return "skipped"
        
        return None

    @workflow.run
    async def run(self, input: ReviewInput) -> str:
        """Run the review workflow."""
        workflow.logger.info(f"Starting review for post {input.post.id}")
        
        # Determinism: Store input for access in handlers
        self.input = input
        
        # R4-NN5: Store for get_card query
        self.workflow_id = workflow.info().workflow_id
        self.created_at = workflow.now().isoformat()
        
        workflow_id = self.workflow_id
        created_at = self.created_at
        
        # Step 1: Research
        evidence = await self._research_post(workflow_id, created_at)
        if evidence is None:
            return "skipped"
        
        # Step 2: Draft (with claim gate retry logic)
        evidence = await self._draft_and_validate(evidence, workflow_id, created_at)
        if evidence is None:
            return "skipped"
        
        self.evidence = evidence
        
        # Store pending card
        card = self._build_card(workflow_id, input.post, created_at)
        
        await workflow.execute_activity(
            store_card,
            StoreCardInput(card=card, idempotency_key=f"{workflow_id}:store:pending"),
            start_to_close_timeout=timedelta(seconds=input.config.activity_timeout_short_sec),
        )
        
        # Step 3: Wait for human decision
        result = await self._wait_for_decision(workflow_id)
        if result:
            return result
        
        # Handle approval (approved is True at this point)
        workflow.logger.info(f"Approved by founder")
        
        # Update to approved status
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
        
        # Step 4: Wait for confirm_posted and update completion
        timeout_hours = input.config.review_timeout_hours
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
                update_card,
                UpdateCardInput(
                    workflow_id=workflow_id,
                    updates={"status": ReviewStatus.APPROVED_NOT_CONFIRMED.value},
                    idempotency_key=f"{workflow_id}:update:not_confirmed",
                ),
                start_to_close_timeout=timedelta(seconds=QUICK_ACTIVITY_TIMEOUT_SEC),
            )
            
            return "approved_not_confirmed"
        
        # Step 5: Start follow-up timer (from config) - cannot be cancelled (R4-SF2)
        if input.config.followup_delay_seconds is not None:
            followup_seconds = input.config.followup_delay_seconds
            workflow.logger.info(f"Confirmed reply {self.reply_post_id}, starting {followup_seconds}s follow-up timer")
        else:
            followup_delay = input.config.followup_delay_days
            followup_seconds = followup_delay * SECONDS_PER_DAY
            workflow.logger.info(f"Confirmed reply {self.reply_post_id}, starting {followup_delay}d follow-up timer")
        
        # Wait for follow-up delay
        await asyncio.sleep(followup_seconds)
        workflow.logger.info("Follow-up timer completed")
        
        # Step 6: Check for follow-up replies (capped)
        workflow.logger.info("Follow-up timer fired, checking for replies")
        
        followup_posts = await workflow.execute_activity(
            "check_followup",
            CheckFollowupInput(
                reply_post_id=self.reply_post_id,
                idempotency_key=f"{workflow_id}:followup",
            ),
            start_to_close_timeout=timedelta(seconds=input.config.activity_timeout_medium_sec),
            retry_policy=RetryPolicy(maximum_attempts=input.config.activity_max_attempts),
            result_type=List[Post],
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
            update_card,
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
    
    @workflow.signal
    async def approve(self, posted_url: str, client_request_id: str):
        """
        Approve signal - founder will post this.
        
        Per ADR-0004: approve just yields the copy text.
        The founder posts manually and then calls confirm_posted.
        """
        # Deduplicate (B9: per-handler set)
        if client_request_id in self._approve_requests:
            workflow.logger.info(f"Ignoring duplicate approve signal: {client_request_id}")
            return
        
        workflow.logger.info(f"Received approve signal")
        self._approve_requests.add(client_request_id)
        
        self.approved = True
        self.posted_url = posted_url
        self.status = ReviewStatus.APPROVED
    
    @workflow.signal
    async def skip(self, reason: str, client_request_id: str):
        """Skip signal - founder declines to post."""
        # Deduplicate (B9: per-handler set)
        if client_request_id in self._skip_requests:
            workflow.logger.info(f"Ignoring duplicate skip signal: {client_request_id}")
            return
        
        workflow.logger.info(f"Received skip signal")
        self._skip_requests.add(client_request_id)
        
        self.skipped = True
        self.skip_reason = reason
        self.status = ReviewStatus.SKIPPED
    
    @workflow.update
    async def confirm_posted(self, reply_url: str, client_request_id: str) -> str:
        """
        Confirm posted Update - verify the reply was actually posted.
        
        Parses reply post ID from URL, verifies it's a reply to our target post.
        Returns success message or raises error if invalid.
        """
        # Deduplicate (B9: per-handler set)
        if client_request_id in self._confirm_requests:
            workflow.logger.info(f"Ignoring duplicate confirm_posted: {client_request_id}")
            return f"Already confirmed reply {self.reply_post_id}"
        
        workflow.logger.info(f"Received confirm_posted: {reply_url}")
        
        self._confirm_requests.add(client_request_id)
        
        # Parse reply post ID from URL (already validated by validator)
        match = re.search(r'/status/(\d+)', reply_url)
        reply_post_id = match.group(1)
        
        # Lookup the reply post to verify it exists
        reply_post = await workflow.execute_activity(
            "lookup_post",
            LookupPostInput(
                post_id=reply_post_id,
                idempotency_key=f"{workflow.info().workflow_id}:confirm:{reply_post_id}",
            ),
            start_to_close_timeout=timedelta(seconds=self.input.config.activity_timeout_short_sec),
            retry_policy=RetryPolicy(maximum_attempts=self.input.config.activity_max_attempts),
            result_type=Optional[Post],
        )
        
        if not reply_post:
            # B8: 404 handling - delete post text per X API compliance
            workflow.logger.warning(f"Reply post {reply_post_id} not found (404), deleting post text")
            
            from backend.activities import DeletePostTextInput, delete_post_text
            
            await workflow.execute_activity(
                delete_post_text,
                DeletePostTextInput(
                    workflow_id=workflow.info().workflow_id,
                    idempotency_key=f"{workflow.info().workflow_id}:delete_text:{reply_post_id}",
                ),
                start_to_close_timeout=timedelta(seconds=self.input.config.activity_timeout_short_sec),
            )
            
            raise ValueError(f"Reply post {reply_post_id} not found (deleted or 404)")
        
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
            update_card,
            UpdateCardInput(
                workflow_id=workflow.info().workflow_id,
                updates={"reply_post_id": reply_post_id},
                idempotency_key=f"{workflow.info().workflow_id}:update:reply_id:{client_request_id}",
            ),
            start_to_close_timeout=timedelta(seconds=10),
        )
        
        return f"Confirmed reply {reply_post_id}"
    
    @confirm_posted.validator
    def validate_confirm_posted(self, reply_url: str, client_request_id: str) -> None:
        """Validator for confirm_posted Update - checks state and URL format."""
        # Check state
        if not self.approved:
            raise ValueError("Cannot confirm: draft not approved yet")
        
        if self.status != ReviewStatus.APPROVED:
            raise ValueError(f"Cannot confirm in status {self.status.value}")
        
        # Validate URL format
        match = re.search(r'/status/(\d+)', reply_url)
        if not match:
            raise ValueError(f"Invalid X post URL: {reply_url}")
    
    @workflow.update
    async def edit(self, new_text: str, client_request_id: str) -> str:
        """Edit update - founder edits the draft."""
        # Deduplicate (B9: per-handler set)
        if client_request_id in self._edit_requests:
            workflow.logger.info(f"Ignoring duplicate edit: {client_request_id}")
            return self.evidence.draft.text if self.evidence and self.evidence.draft else ""
        
        workflow.logger.info(f"Received edit update: {len(new_text)} chars")
        self._edit_requests.add(client_request_id)
        
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
    
    @edit.validator
    def validate_edit(self, new_text: str, client_request_id: str) -> None:
        """Validator for edit Update - checks state and input."""
        if self.status not in [ReviewStatus.PENDING, ReviewStatus.APPROVED]:
            raise ValueError(f"Cannot edit in status {self.status.value}")
        
        if not self.evidence or not self.evidence.draft:
            raise ValueError("No draft to edit")
        
        if not new_text or not new_text.strip():
            raise ValueError("New text cannot be empty")
    
    @workflow.update
    async def nudge(self, instruction: str, client_request_id: str) -> str:
        """Nudge update - steer a redraft."""
        # Deduplicate (B9: per-handler set)
        if client_request_id in self._nudge_requests:
            workflow.logger.info(f"Ignoring duplicate nudge: {client_request_id}")
            return self.evidence.draft.text if self.evidence and self.evidence.draft else ""
        
        workflow.logger.info(f"Received nudge update: {len(instruction)} chars")
        self._nudge_requests.add(client_request_id)
        
        self.evidence = await workflow.execute_activity(
            "draft_reply",
            DraftReplyInput(
                evidence=self.evidence,
                steer=instruction,
                idempotency_key=f"{self.evidence.post_id}:nudge:{client_request_id}",
            ),
            start_to_close_timeout=timedelta(seconds=self.input.config.activity_timeout_long_sec),
            retry_policy=RetryPolicy(maximum_attempts=self.input.config.activity_max_attempts),
            result_type=Evidence,
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
        
        return self.evidence.draft.text
    
    @nudge.validator
    def validate_nudge(self, instruction: str, client_request_id: str) -> None:
        """Validator for nudge Update - checks state and input (B9)."""
        if self.status not in [ReviewStatus.PENDING, ReviewStatus.APPROVED]:
            raise ValueError(f"Cannot nudge in status {self.status.value}")
        
        if not self.evidence or not self.evidence.draft:
            raise ValueError("No draft to nudge")
        
        if not instruction or not instruction.strip():
            raise ValueError("Instruction cannot be empty")
    
    @workflow.query
    def get_card(self) -> dict:
        """
        R4-NN5: Query to get the current card state using _build_card helper.
        
        Returns a dict matching ReviewCard schema (see models/review.py).
        """
        # Early returns for workflows that haven't stored metadata yet
        if not self.workflow_id or not self.created_at:
            return {
                "status": self.status.value,
                "evidence": None,
            }
        
        if not self.input:
            return {
                "status": self.status.value,
                "evidence": None,
            }
        
        # Use _build_card helper to ensure consistency with ReviewCard structure
        card = self._build_card(self.workflow_id, self.input.post, self.created_at)
        return card.to_dict()
