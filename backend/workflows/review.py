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
from temporalio.exceptions import ApplicationError

# Constants
SECONDS_PER_DAY = 86400
QUICK_ACTIVITY_TIMEOUT_SEC = 10

# --- Reply plan (issue #5) ---
# Patch id guarding the plan step. Review workflows started before the step
# existed replay without it: workflow.patched() is False for their history.
REPLY_PLAN_PATCH_ID = "reply-plan-step-v1"
# The first plan request plus one re-request; then the post is skipped.
REPLY_PLAN_MAX_ATTEMPTS = 2
# Patch id: a 404 on the founder's reply URL no longer deletes the target
# post's text (found in the issue #5 review).
KEEP_TARGET_ON_REPLY_404_PATCH_ID = "keep-target-on-reply-404-v1"
# --- end reply plan ---
# Patch id (issue #9): approve re-fetches the target post first (removal duty,
# ADR-0006/0086). Reviews approved before this replay without the re-fetch.
TARGET_REFETCH_PATCH_ID = "target-refetch-on-approve-v1"

with workflow.unsafe.imports_passed_through():
    from backend.models import (
        ReviewCard,
        Post,
        Evidence,
        ClaimGateResult,
        ReplyPlan,
        SentenceGateResult,
    )
    from backend.activities import (
        ResearchPostInput,
        DraftReplyInput,
        PlanReplyInput,
        PlanReplyResult,
        StoreCardInput,
        UpdateCardInput,
        LookupPostInput,
        CheckFollowupInput,
    )
    from backend.activities.claim_gate_activity import (
        ClaimGateInput,
        claim_gate,
        EditedTextGateInput,
        verify_edited_text,
    )
    from backend.activities.store_activities import (
        store_card,
        update_card,
        delete_post_text,
        DeletePostTextInput,
        POST_TEXT_REMOVED,
        POST_REMOVED_SKIP_REASON,
        redact_post_text_copies,
    )
    from backend.config.confidence import CONFIDENCE_THRESHOLD


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
    confidence_threshold: float = CONFIDENCE_THRESHOLD  # dated in backend/config/confidence.py
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
    1b. Plan the reply (issue #5): reply type, target point, product mention
    2. Draft a reply (steered by the plan)
    3. Run claim gate
    4. Wait for human (approve/skip/edit/nudge)
    5. If approved, wait for confirm_posted with reply URL
    6. Start follow-up timer (3 days)
    7. Check for replies and resurface
    """
    
    def __init__(self):
        self.evidence: Optional[Evidence] = None
        self.status = "pending"
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
        # Edit re-gate fields (ADR-0006 Amendment E)
        self.edited_text_gate_results: Optional[List[SentenceGateResult]] = None
        self.edited_text_gate_state: Optional[str] = None  # "passed", "failed", or "not_checked"
        # QA R12: edited_text_hash removed (dead code, see validate_approve)
        self.post_anyway: Optional[bool] = None
        self.post_anyway_failing_sentences: Optional[List[str]] = None
        # S-3: Concurrency guard for edit-in-flight (B3: counter instead of bool)
        self._edit_in_flight_count = 0
        # QA R13: Monotonic edit sequence to prevent stale card writes
        self._edit_seq = 0
        # issue #5: set once delete_post_text ran, so get_card stops returning
        # the post text and the reply plan's copied target_point
        self._post_text_removed = False
        # issue #5: nudges and edits change the draft across activity awaits;
        # validators use this to serialize them (and to block approve)
        self._nudge_in_flight_count = 0
        # issue #9: approve's target re-fetch (and any removal it triggers) is
        # in flight; validators block edits, nudges and other approves meanwhile
        self._approve_in_flight_count = 0

    def _build_card(self, workflow_id: str, post: Post, created_at: str) -> ReviewCard:
        """Helper to build a ReviewCard from current state (R4-B1: convert enum to str)."""
        return ReviewCard(
            workflow_id=workflow_id,
            post=post,
            evidence=self.evidence,
            status=self.status,  # R5-N1: Now using string literals, not enum
            created_at=created_at,
            updated_at=workflow.now().isoformat(),
            skip_reason=self.skip_reason if self.skipped else None,
            posted_url=self.posted_url,
            reply_post_id=self.reply_post_id,
            edited_text_gate_results=self.edited_text_gate_results,
            edited_text_gate_state=self.edited_text_gate_state,
            post_anyway=self.post_anyway,
            post_anyway_failing_sentences=self.post_anyway_failing_sentences,
        )

    async def _research_post(self, workflow_id: str, created_at: str) -> Optional[Evidence]:
        """Research post and return evidence, or None if skipped before drafting.

        Skips (issue #4) when a should-we-reply rule fired in research_post or
        the computed confidence is below the threshold.
        """
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
        
        # --- Issue #4: decide whether to reply before any draft is made ---
        # research_post applied the skip rules in code (backend/gate/reply_fit.py)
        # and set evidence.skip_reason; then the computed confidence must clear
        # the threshold. Either way no draft_reply call is made.
        skip_reason = evidence.skip_reason
        # A research result recorded before the rules existed has no reply_fit
        # and an old-rule score. New workflow tasks skip it; replaying a history
        # that already went on to draft keeps the old path (no patch marker).
        if evidence.reply_fit is None and workflow.patched("issue-4-reply-fit-gate"):
            skip_reason = skip_reason or "Research predates the should-we-reply rules; not drafting"
        if not skip_reason and evidence.confidence_score < input.confidence_threshold:
            skip_reason = f"Low confidence: {evidence.confidence_score:.2f} < {input.confidence_threshold}"
        if skip_reason:
            self.status = "skipped"
            self.skipped = True
            self.skip_reason = skip_reason
            evidence.skip_reason = skip_reason
            self.evidence = evidence
            workflow.logger.info(f"Skipped before drafting: {skip_reason}")
            # --- end issue #4 block ---
            
            card = self._build_card(workflow_id, input.post, created_at)
            
            await workflow.execute_activity(
                store_card,
                StoreCardInput(card=card, idempotency_key=f"{workflow_id}:store:skip"),
                start_to_close_timeout=timedelta(seconds=input.config.activity_timeout_short_sec),
            )
            
            return None
        
        return evidence

    async def _plan_reply(self, evidence: Evidence, workflow_id: str, created_at: str) -> Optional[Evidence]:
        """
        Plan the reply before drafting (issue #5). Returns None if the post is skipped.
        
        The plan_reply activity asks the reply agent for a plan and validates it
        in code (target_point is a substring of the post; answer_question needs a
        question). An invalid plan is re-requested once with the errors as
        feedback; a second invalid plan skips the post with the reasons recorded.
        """
        input = self.input
        result: Optional[PlanReplyResult] = None
        feedback: Optional[List[str]] = None
        attempts = 0
        
        while attempts < REPLY_PLAN_MAX_ATTEMPTS:
            attempts += 1
            result = await workflow.execute_activity(
                "plan_reply",
                PlanReplyInput(
                    post=input.post,
                    evidence=evidence,
                    idempotency_key=f"{input.post.id}:plan:attempt{attempts}",
                    feedback=feedback,
                ),
                start_to_close_timeout=timedelta(seconds=input.config.activity_timeout_long_sec),
                retry_policy=RetryPolicy(maximum_attempts=input.config.activity_max_attempts),
                result_type=PlanReplyResult,
            )
            if not result.errors:
                break
            workflow.logger.warning(f"Reply plan invalid on attempt {attempts}: {result.errors}")
            feedback = list(result.errors)
        
        evidence.reply_plan = result.plan
        evidence.reply_plan_errors = list(result.errors)
        evidence.reply_plan_attempts = attempts
        
        if not result.errors and not result.skip_reason:
            workflow.logger.info(
                f"Reply plan: {result.plan.reply_type}, mention_product={result.plan.mention_product}"
            )
            return evidence
        
        if result.errors:
            skip_reason = f"Reply plan invalid after {attempts} attempts: " + "; ".join(result.errors[:3])
            store_key = f"{workflow_id}:store:plan_invalid"
        else:
            # A valid plan that a plan-stage skip rule rejects (needs_written_drafts);
            # the verdict comes from the plan_reply activity result.
            evidence.reply_plan_skip_rule = result.skip_rule
            skip_reason = result.skip_reason
            store_key = f"{workflow_id}:store:plan_skip"
        
        self.status = "skipped"
        self.skipped = True
        self.skip_reason = skip_reason
        evidence.skip_reason = skip_reason
        self.evidence = evidence
        workflow.logger.info(f"Skipped: {self.skip_reason}")
        
        card = self._build_card(workflow_id, input.post, created_at)
        await workflow.execute_activity(
            store_card,
            StoreCardInput(card=card, idempotency_key=store_key),
            start_to_close_timeout=timedelta(seconds=input.config.activity_timeout_short_sec),
        )
        return None

    async def _draft_and_validate(self, evidence: Evidence, workflow_id: str, created_at: str) -> Optional[Evidence]:
        """
        Draft reply with claim gate validation retries. Returns None if validation fails.
        
        Per ADR-0006 Amendment 2026-11-21:
        - First draft with no steer
        - On failure: exactly one redraft with per-sentence reasons as steer
        - Second failure: end as skipped with reasons recorded
        """
        input = self.input
        claim_gate_attempts = 0
        max_claim_gate_attempts = input.config.claim_gate_max_attempts
        # (sentence, reason) pairs from the last failed attempt: claim-gate
        # failures, preceded by reply-plan failures (issue #5) when there is a plan
        last_failures: List[tuple] = []
        
        while claim_gate_attempts < max_claim_gate_attempts:
            claim_gate_attempts += 1
            
            # Build steer from the last attempt's per-sentence failures
            steer = None
            if claim_gate_attempts > 1 and last_failures:
                reasons = []
                for sentence, reason in last_failures[:3]:  # Limit to first 3 for brevity
                    reasons.append(f"'{sentence[:50]}...': {reason}")
                steer = f"Previous draft failed claim gate. Fix these issues: {'; '.join(reasons)}"
            
            # Draft reply
            evidence = await workflow.execute_activity(
                "draft_reply",
                DraftReplyInput(
                    evidence=evidence,
                    steer=steer,
                    idempotency_key=f"{input.post.id}:draft:attempt{claim_gate_attempts}",
                    plan=evidence.reply_plan,
                ),
                start_to_close_timeout=timedelta(seconds=input.config.activity_timeout_long_sec),
                retry_policy=RetryPolicy(maximum_attempts=input.config.activity_max_attempts),
                result_type=Evidence,
            )
            
            # issue #5: does the draft follow the reply plan? The verdict comes
            # from the draft_reply activity result (draft.plan_conformance_failures,
            # kept separate from the claim-gate verdict) and is only consulted
            # when there is a plan, so pre-plan workflows replay as before. A
            # non-conforming draft counts as a failed attempt: one redraft with
            # the reasons as the steer, then skip. Plan reasons come first so
            # the steer and skip reason (cut to 3 entries) always carry them.
            plan_failures: List[tuple] = []
            if evidence.reply_plan is not None:
                reasons = (
                    list(evidence.draft.plan_conformance_failures)
                    if evidence.draft
                    else ["the draft is missing"]
                )
                plan_failures = [("[reply plan]", r) for r in reasons]
            
            # Run deterministic claim gate with segments
            if evidence.draft and evidence.draft.segments:
                claim_gate_result = await workflow.execute_activity(
                    claim_gate,
                    ClaimGateInput(
                        segments=evidence.draft.segments,
                        founder_materials=evidence.founder_materials,
                        idempotency_key=f"{input.post.id}:gate:attempt{claim_gate_attempts}",
                    ),
                    start_to_close_timeout=timedelta(seconds=input.config.activity_timeout_short_sec),
                )
                
                # Update evidence with gate result
                evidence.draft.claim_gate = claim_gate_result
                # Update draft text with properly constructed version from claim_gate
                if claim_gate_result.draft_text:
                    evidence.draft.text = claim_gate_result.draft_text
                # Update segments with processed versions (text filled in)
                evidence.draft.segments = claim_gate_result.segments
                last_failures = plan_failures + list(claim_gate_result.failed_sentences)
                
                if claim_gate_result.passed and not plan_failures:
                    workflow.logger.info(f"Claim gate passed on attempt {claim_gate_attempts}")
                    return evidence
                else:
                    workflow.logger.warning(
                        f"Draft failed on attempt {claim_gate_attempts}: "
                        f"{len(claim_gate_result.failed_sentences)} failed sentences, "
                        f"{len(plan_failures)} reply-plan failures"
                    )
            elif plan_failures:
                # issue #5: an empty draft under a plan is redrafted, not ended.
                # The claim gate did not run; say so instead of keeping the
                # adapter's placeholder result.
                last_failures = plan_failures
                if evidence.draft:
                    evidence.draft.claim_gate = ClaimGateResult(
                        passed=False,
                        segments=[],
                        failed_sentences=[],
                        reasoning="Claim gate not run: the draft has no segments",
                    )
                workflow.logger.warning(
                    f"Draft does not follow the reply plan on attempt {claim_gate_attempts}: {plan_failures}"
                )
            else:
                workflow.logger.error("No draft or segments generated")
                break
        
        # Skip if the draft failed after retries
        # Record the per-sentence reasons from the last attempt
        skip_reason = "Not enough evidence - claim gate failed after regeneration"
        if last_failures:
            failure_summary = "; ".join(
                f"{sentence[:30]}... ({reason})"
                for sentence, reason in last_failures[:3]
            )
            skip_reason = f"Not enough evidence: {failure_summary}"
        
        self.status = "skipped"
        self.skipped = True
        self.skip_reason = skip_reason
        self.evidence = evidence
        workflow.logger.info(f"Skipped: {skip_reason}")
        
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
            self.status = "expired"
            workflow.logger.info(f"Review expired after {timeout_hours}h")
            
            await workflow.execute_activity(
                update_card,
                UpdateCardInput(
                    workflow_id=workflow_id,
                    idempotency_key=f"{workflow_id}:update:expire",
                    status="expired",
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
        
        # Step 1b: Plan the reply (issue #5). Guarded by a patch so Review
        # workflows started before this step replay unchanged.
        if workflow.patched(REPLY_PLAN_PATCH_ID):
            evidence = await self._plan_reply(evidence, workflow_id, created_at)
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
        
        # Update to approved status (including post_anyway if used)
        await workflow.execute_activity(
            update_card,
            UpdateCardInput(
                workflow_id=workflow_id,
                idempotency_key=f"{workflow_id}:update:approve",
                status="approved",
                posted_url=self.posted_url,
                post_anyway=self.post_anyway,
                post_anyway_failing_sentences=self.post_anyway_failing_sentences,
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
            self.status = "approved_not_confirmed"
            workflow.logger.info(f"Approved but not confirmed after {timeout_hours}h")
            
            await workflow.execute_activity(
                update_card,
                UpdateCardInput(
                    workflow_id=workflow_id,
                    idempotency_key=f"{workflow_id}:update:not_confirmed",
                    status="approved_not_confirmed",
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
                idempotency_key=f"{workflow_id}:update:followup_due",
                status="followup_due",
                followup_replies=followup_replies,
            ),
            start_to_close_timeout=timedelta(seconds=10),
        )
        
        return "followup_due"
    
    @workflow.update
    async def approve(self, posted_url: str, client_request_id: str, post_anyway: bool = False) -> str:
        """
        Approve update - founder will post this.
        
        Per ADR-0004: approve just yields the copy text.
        The founder posts manually and then calls confirm_posted.
        
        Per ADR-0006 Amendment E: If edited text failed re-gate, requires post_anyway=True.
        Validator rejects approve without post_anyway when edited text fails gate.
        """
        # Deduplicate (B9: per-handler set)
        if client_request_id in self._approve_requests:
            workflow.logger.info(f"Ignoring duplicate approve: {client_request_id}")
            return "Approved"
        
        workflow.logger.info(f"Received approve (post_anyway={post_anyway})")
        self._approve_requests.add(client_request_id)

        # issue #9: re-fetch the target post before the first approval (removal
        # duty, ADR-0006/0086). Approvals recorded before this step replay
        # without it (no patch marker in their history).
        if self.status == "pending" and workflow.patched(TARGET_REFETCH_PATCH_ID):
            try:
                await self._refetch_target_before_approve(client_request_id)
            except Exception:
                # Not approved: a retry with the same request id must run again
                # rather than report "Approved" through the dedupe path.
                self._approve_requests.discard(client_request_id)
                raise

        self.approved = True
        self.posted_url = posted_url
        self.status = "approved"
        self.post_anyway = post_anyway if post_anyway else None
        
        # If post_anyway was used, record the failing sentences
        if post_anyway and self.edited_text_gate_results:
            failing = [r.sentence for r in self.edited_text_gate_results if not r.passed]
            self.post_anyway_failing_sentences = failing
            workflow.logger.info(f"Approved with post_anyway: {len(failing)} failing sentences")
        
        return "Approved"

    async def _refetch_target_before_approve(self, client_request_id: str) -> None:
        """Re-fetch the target post for approve (issue #9); raise if not approvable.

        A 404 removes the post text and the model-written copies of it, ends the
        Review as skipped ("post removed") and fails the update with type
        PostRemoved. The in-flight count keeps edits, nudges and other approves
        out until the re-fetch and any removal finish.
        """
        self._approve_in_flight_count += 1
        try:
            found = await self._refetch_target(f"approve:{client_request_id}")
        finally:
            self._approve_in_flight_count -= 1
        if not found:
            raise ApplicationError(POST_REMOVED_SKIP_REASON, type="PostRemoved", non_retryable=True)
        if self.status != "pending":
            # The founder skipped, or the review expired, during the re-fetch
            raise ApplicationError(f"Cannot approve in status {self.status}", non_retryable=True)

    async def _refetch_target(self, key: str) -> bool:
        """Look up the target post again (issue #9). Returns False after a 404.

        Uses the lookup_post activity, which reserves one X read through
        backend.budget before the call; activity retries reuse that
        reservation, and over budget it raises (never reports not-found).
        On a 404 the removal duty runs before returning.
        """
        workflow_id = workflow.info().workflow_id
        post = await workflow.execute_activity(
            "lookup_post",
            LookupPostInput(
                post_id=self.input.post.id,
                idempotency_key=f"{workflow_id}:refetch_target:{key}",
            ),
            start_to_close_timeout=timedelta(seconds=self.input.config.activity_timeout_short_sec),
            retry_policy=RetryPolicy(maximum_attempts=self.input.config.activity_max_attempts),
            result_type=Optional[Post],
        )
        if post is not None:
            return True
        workflow.logger.warning(f"Target post {self.input.post.id} not found on re-fetch (404); removing its text")
        await self._remove_target_post_text()
        return False

    async def _remove_target_post_text(self) -> None:
        """Removal duty for a 404 on the target post (ADR-0006/0086, issue #9).

        delete_post_text replaces the stored post text and every evidence copy
        of it (redact_post_text_copies: why_it_fits, the problem span, the plan
        target point, ...) and marks the card; get_card applies the same
        redaction. A pending Review then ends as skipped "post removed". The
        card is written before the state changes, so the main loop cannot
        finish the workflow with the card still pending.
        """
        workflow_id = workflow.info().workflow_id
        await workflow.execute_activity(
            delete_post_text,
            DeletePostTextInput(
                workflow_id=workflow_id,
                idempotency_key=f"{workflow_id}:delete_text:target",
            ),
            start_to_close_timeout=timedelta(seconds=self.input.config.activity_timeout_short_sec),
        )
        self._post_text_removed = True
        if self.status != "pending":
            return
        await workflow.execute_activity(
            update_card,
            UpdateCardInput(
                workflow_id=workflow_id,
                idempotency_key=f"{workflow_id}:update:post_removed",
                status="skipped",
                skip_reason=POST_REMOVED_SKIP_REASON,
            ),
            start_to_close_timeout=timedelta(seconds=self.input.config.activity_timeout_short_sec),
        )
        self.status = "skipped"
        self.skipped = True
        self.skip_reason = POST_REMOVED_SKIP_REASON
        if self.evidence:
            self.evidence.skip_reason = POST_REMOVED_SKIP_REASON

    @approve.validator
    def validate_approve(self, posted_url: str, client_request_id: str, post_anyway: bool = False) -> None:
        """
        Validator for approve Update.
        
        Per ADR-0006 Amendment E + QA R11 B2/C fixes:
        - Rejects approve without post_anyway=True when edited text fails gate or is not_checked
        - B2: Validates gate results correspond to CURRENT text (via hash)
        - B3: Uses counter instead of bool for edit-in-flight check
        - S-3: Concurrency guard - rejects if edit is in flight
        """
        if self.status not in ["pending", "approved"]:
            raise ValueError(f"Cannot approve in status {self.status}")
        
        # S-3 + B3: Reject if any edit is currently in flight (count > 0)
        if self._edit_in_flight_count > 0:
            raise ValueError("Cannot approve while edit gate is in progress. Retry after edit completes.")
        
        # issue #5: likewise, do not approve a draft a nudge is about to replace
        if self._nudge_in_flight_count > 0:
            raise ValueError("Cannot approve while a nudge is in progress. Retry after it completes.")

        # issue #9: one approve at a time (its target re-fetch awaits an activity)
        if self._approve_in_flight_count > 0:
            raise ValueError("Cannot approve while another approval is in progress. Retry after it completes.")

        # QA R12: Hash check removed (dead code).
        # The edit Update atomically sets both draft.text and edited_text_hash from the same
        # new_text. There is no path to tamper with draft.text after gating without calling
        # edit again (which would recompute the hash). Temporal's deterministic replay protects
        # workflow state. No honest tamper path exists, so the check could never fail.
        
        # B2 + C: Check if edited text exists and require post_anyway for failed/not_checked states
        if self.edited_text_gate_state:
            # C: Require post_anyway=True for both failed and not_checked states
            if self.edited_text_gate_state in ["failed", "not_checked"] and not post_anyway:
                if self.edited_text_gate_state == "failed":
                    failing_count = sum(1 for r in self.edited_text_gate_results if not r.passed)
                    raise ValueError(
                        f"Edited text has {failing_count} failing sentence(s). "
                        f"Set post_anyway=true to approve anyway."
                    )
                else:  # not_checked
                    raise ValueError(
                        "Edited text gate check failed (retries exhausted). "
                        "Set post_anyway=true to approve anyway."
                    )
    
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
        self.status = "skipped"
    
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
        try:
            return await self._run_confirm_posted(reply_url, client_request_id)
        except Exception:
            # A failed confirmation is not "done": a retry with the same request
            # id must check again, not report "Already confirmed" (issue #5
            # review; these failures used to leave the update unresolved).
            self._confirm_requests.discard(client_request_id)
            raise
    
    async def _run_confirm_posted(self, reply_url: str, client_request_id: str) -> str:
        """Body of the confirm_posted update."""
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
            # A missing *reply* says nothing about the target post, so its text
            # stays (the removal duty, ADR-0006/0086, is about a 404 on the
            # target itself). Histories recorded before this fix scheduled
            # delete_post_text here; the patch keeps them replaying.
            if not workflow.patched(KEEP_TARGET_ON_REPLY_404_PATCH_ID):
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
                self._post_text_removed = True
            else:
                workflow.logger.warning(f"Reply post {reply_post_id} not found (404)")
            
            # A plain ValueError raised in an update handler body (not a
            # validator) fails the workflow task and retries it forever, so the
            # update never resolves. ApplicationError fails just the update.
            raise ApplicationError(
                f"Reply post {reply_post_id} not found (deleted or 404)", non_retryable=True
            )
        
        # Verify it's a reply to our target post
        # For real X API, check in_reply_to_user_id or conversation_id
        # For fake client, check conversation_id matches
        if reply_post.conversation_id != self.evidence.post_id:
            raise ApplicationError(
                f"Post {reply_post_id} (conversation_id={reply_post.conversation_id}) "
                f"is not a reply to target post {self.evidence.post_id}",
                non_retryable=True,
            )
        
        workflow.logger.info(f"Confirmed reply {reply_post_id} is valid")
        
        self.confirmed = True
        self.reply_post_id = reply_post_id
        
        # Update card with reply_post_id
        await workflow.execute_activity(
            update_card,
            UpdateCardInput(
                workflow_id=workflow.info().workflow_id,
                idempotency_key=f"{workflow.info().workflow_id}:update:reply_id:{client_request_id}",
                reply_post_id=reply_post_id,
            ),
            start_to_close_timeout=timedelta(seconds=10),
        )
        
        return f"Confirmed reply {reply_post_id}"
    
    @confirm_posted.validator
    def validate_confirm_posted(self, reply_url: str, client_request_id: str) -> None:
        """Validator for confirm_posted Update - checks state and URL format. S-4: Fix .value reference."""
        # Check state
        if not self.approved:
            raise ValueError("Cannot confirm: draft not approved yet")
        
        # S-4: status is already a string, not an enum
        if self.status != "approved":
            raise ValueError(f"Cannot confirm in status {self.status}")
        
        # Validate URL format
        match = re.search(r'/status/(\d+)', reply_url)
        if not match:
            raise ValueError(f"Invalid X post URL: {reply_url}")
    
    @workflow.update
    async def edit(self, new_text: str, client_request_id: str) -> str:
        """
        Edit update - founder edits the draft text.
        
        Re-gate the edited text per ADR-0006 Amendment E + QA R11 B2/B3/C fixes:
        - B2: Don't commit text until gate returns; record failures instead of raising
        - B3: Use counter for edit-in-flight (two concurrent edits)
        - C: Handle activity errors → record "not_checked" state
        Results stored for evidence trail; not a hard block (approve requires post_anyway if fails/not_checked).
        
        QA R12: Concurrent edits resolve as last-writer-wins by finish order. Each edit
        runs its gate independently; the last to complete sets the final draft.text and
        gate state. This is documented in ADR-0006.
        """
        # Deduplicate (B9: per-handler set)
        if client_request_id in self._edit_requests:
            workflow.logger.info(f"Ignoring duplicate edit: {client_request_id}")
            return self.evidence.draft.text if self.evidence and self.evidence.draft else ""
        
        workflow.logger.info(f"Received edit update: {len(new_text)} chars")
        self._edit_requests.add(client_request_id)
        
        workflow_id = workflow.info().workflow_id
        
        # S-3 + B3: Increment edit-in-flight counter (not bool) to handle concurrent edits
        # Concurrency scenario: Client A calls edit (awaits gate activity), Client B calls approve
        # while gate is running. Without this guard, B's validator sees old edited_text_gate_results
        # and incorrectly allows approval. The counter ensures A's validator rejects until all edits complete.
        self._edit_in_flight_count += 1
        
        try:
            # Re-gate edited text (ADR-0006 Amendment E)
            workflow.logger.info(f"Running re-gate on edited text")
            
            # B2 + C: Wrap activity call in try/except to handle errors
            gate_results = None
            gate_state = "not_checked"  # Default if activity fails
            
            try:
                gate_results = await workflow.execute_activity(
                    verify_edited_text,
                    EditedTextGateInput(
                        edited_text=new_text,
                        gated_segments=self.evidence.draft.segments,
                        founder_materials=self.evidence.founder_materials,
                        idempotency_key=f"{workflow_id}:edit_gate:{client_request_id}",
                    ),
                    start_to_close_timeout=timedelta(seconds=self.input.config.activity_timeout_medium_sec),
                    retry_policy=RetryPolicy(
                        maximum_attempts=self.input.config.activity_max_attempts,
                    ),
                    result_type=List[SentenceGateResult],
                )
                
                # Activity succeeded - determine state from results
                has_failures = any(not r.passed for r in gate_results)
                gate_state = "failed" if has_failures else "passed"
                
                passed_count = sum(1 for r in gate_results if r.passed)
                workflow.logger.info(
                    f"Edit gate: {passed_count}/{len(gate_results)} sentences passed (state: {gate_state})"
                )
                
            except Exception as e:
                # C: Activity failed (retries exhausted or other error)
                workflow.logger.error(f"Edit gate activity failed: {e}")
                gate_state = "not_checked"
                gate_results = []  # Empty results
            
            # B2: Only NOW commit the edited text and gate results (after activity returns)
            self.evidence.draft.text = new_text
            self.evidence.draft.steers_applied.append("manual_edit")
            
            # Store gate results and state
            self.edited_text_gate_results = gate_results if gate_results else []
            self.edited_text_gate_state = gate_state
            
            # QA R12: Hash removed (dead code, see validate_approve)
            
            # QA R12+R13: Increment edit_seq AFTER gate completes, so last-to-finish wins
            # This implements "last-writer-wins by finish order" (R12) while preventing
            # stale writes (R13): whichever edit finishes last gets highest edit_seq.
            self._edit_seq += 1
            current_edit_seq = self._edit_seq
            
            # QA R13: Update card with edited text, gate results, and edit_seq
            await workflow.execute_activity(
                update_card,
                UpdateCardInput(
                    workflow_id=workflow_id,
                    idempotency_key=f"{workflow_id}:update:edit:{client_request_id}",
                    evidence=self.evidence,
                    edited_text_gate_results=self.edited_text_gate_results,
                    edited_text_gate_state=gate_state,
                    edit_seq=current_edit_seq,
                ),
                start_to_close_timeout=timedelta(seconds=10),
            )
            
            return self.evidence.draft.text
            
        finally:
            # S-3 + B3: Decrement counter (was bool, now counter)
            self._edit_in_flight_count -= 1
    
    @edit.validator
    def validate_edit(self, new_text: str, client_request_id: str) -> None:
        """Validator for edit Update - checks state and input. S-1: Reject edits once approved. S-4: Fix AttributeError."""
        # S-4: Use string comparison, not .value (status is already a string)
        if self.status == "approved":
            # S-1: Reject edits once approved
            raise ValueError("Cannot edit after approval")
        
        if self.status not in ["pending"]:
            raise ValueError(f"Cannot edit in status {self.status}")
        
        if not self.evidence or not self.evidence.draft:
            raise ValueError("No draft to edit")
        
        # issue #5: a nudge in flight would overwrite this edit (or be
        # overwritten by it) across its activity awaits
        if self._nudge_in_flight_count > 0:
            raise ValueError("Cannot edit while a nudge is in progress. Retry after it completes.")

        # issue #9: an edit during approve's re-fetch would change the draft
        # after the validator checked its gate state
        if self._approve_in_flight_count > 0:
            raise ValueError("Cannot edit while an approval is in progress. Retry after it completes.")

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
        
        self._nudge_in_flight_count += 1
        try:
            return await self._run_nudge(instruction, client_request_id)
        finally:
            self._nudge_in_flight_count -= 1
    
    async def _run_nudge(self, instruction: str, client_request_id: str) -> str:
        """Body of the nudge update (the handler wraps it in the in-flight count)."""
        plan = self.evidence.reply_plan
        if plan is not None:
            # issue #5: under a plan, a nudge that fails for any reason (an
            # activity error, or a draft rejected by _check_nudged_draft) is not
            # "done": a retry with the same request id must run again, not
            # report success via the dedupe path. Pre-plan workflows keep the
            # old dedupe behaviour so their histories replay unchanged.
            try:
                return await self._nudge_with_plan(instruction, client_request_id, plan)
            except Exception:
                self._nudge_requests.discard(client_request_id)
                raise
        
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
                idempotency_key=f"{workflow_id}:update:nudge:{client_request_id}",
                evidence=self.evidence,
            ),
            start_to_close_timeout=timedelta(seconds=10),
        )
        
        return self.evidence.draft.text
    
    async def _nudge_with_plan(self, instruction: str, client_request_id: str, plan: ReplyPlan) -> str:
        """Nudge for a workflow that ran the plan step (issue #5).
        
        The redraft is steered by the plan, must follow it and pass the
        unchanged claim gate before it replaces the current draft.
        """
        redrafted = await workflow.execute_activity(
            "draft_reply",
            DraftReplyInput(
                evidence=self.evidence,
                steer=instruction,
                idempotency_key=f"{self.evidence.post_id}:nudge:{client_request_id}",
                plan=plan,
            ),
            start_to_close_timeout=timedelta(seconds=self.input.config.activity_timeout_long_sec),
            retry_policy=RetryPolicy(maximum_attempts=self.input.config.activity_max_attempts),
            result_type=Evidence,
        )
        await self._check_nudged_draft(redrafted, client_request_id)
        
        # Persist first, then commit to workflow state, so a failed card
        # update leaves both the query and the stored card on the old draft.
        # The new draft is model-assembled and gated, so any earlier edit's
        # gate results describe text that is gone: clear them in both places.
        workflow_id = workflow.info().workflow_id
        await workflow.execute_activity(
            update_card,
            UpdateCardInput(
                workflow_id=workflow_id,
                idempotency_key=f"{workflow_id}:update:nudge:{client_request_id}",
                evidence=redrafted,
                clear_edited_text_gate=True,
            ),
            start_to_close_timeout=timedelta(seconds=10),
        )
        self.evidence = redrafted
        self.edited_text_gate_results = None
        self.edited_text_gate_state = None
        return self.evidence.draft.text
    
    async def _check_nudged_draft(self, redrafted: Evidence, client_request_id: str) -> None:
        """Gate a nudged draft under a plan (issue #5); raise to reject the nudge.
        
        On success, the draft's text and segments are the gate's assembled
        versions, as on the initial draft path. On failure the update fails
        with the reasons and the current draft stays unchanged.
        """
        draft = redrafted.draft
        failures: List[str] = list(draft.plan_conformance_failures) if draft else ["the draft is missing"]
        if draft and draft.segments:
            gate_result = await workflow.execute_activity(
                claim_gate,
                ClaimGateInput(
                    segments=draft.segments,
                    founder_materials=redrafted.founder_materials,
                    idempotency_key=f"{redrafted.post_id}:nudge_gate:{client_request_id}",
                ),
                start_to_close_timeout=timedelta(seconds=self.input.config.activity_timeout_short_sec),
            )
            draft.claim_gate = gate_result
            if gate_result.draft_text:
                draft.text = gate_result.draft_text
            draft.segments = gate_result.segments
            if not gate_result.passed:
                failures += [f"{sentence[:50]}: {reason}" for sentence, reason in gate_result.failed_sentences]
                if not gate_result.failed_sentences:
                    failures.append(gate_result.reasoning)
        if failures:
            raise ApplicationError(
                "Nudged draft rejected; the current draft is unchanged: " + "; ".join(failures[:3]),
                non_retryable=True,
            )
    
    @nudge.validator
    def validate_nudge(self, instruction: str, client_request_id: str) -> None:
        """Validator for nudge Update - checks state and input. S-4: Fix .value reference."""
        # S-4: status is already a string, not an enum
        if self.status not in ["pending", "approved"]:
            raise ValueError(f"Cannot nudge in status {self.status}")
        
        if not self.evidence or not self.evidence.draft:
            raise ValueError("No draft to nudge")
        
        # issue #5: serialize draft changes; a nudge overlapping an edit or
        # another nudge could leave the card and the query on different drafts
        if self._edit_in_flight_count > 0 or self._nudge_in_flight_count > 0:
            raise ValueError("Cannot nudge while another draft change is in progress. Retry after it completes.")

        # issue #9: likewise during approve's target re-fetch
        if self._approve_in_flight_count > 0:
            raise ValueError("Cannot nudge while an approval is in progress. Retry after it completes.")
        
        if not instruction or not instruction.strip():
            raise ValueError("Instruction cannot be empty")
    
    @workflow.query
    def get_card(self) -> dict:
        """
        R4-NN5: Query to get the current card state using _build_card helper.
        
        Returns a dict matching ReviewCard schema (see models/review.py).
        S-4: Fix .value references - status is already a string.
        """
        # Early returns for workflows that haven't stored metadata yet
        # S-4: status is already a string, not an enum
        if not self.workflow_id or not self.created_at:
            return {
                "status": self.status,
                "evidence": None,
            }
        
        if not self.input:
            return {
                "status": self.status,
                "evidence": None,
            }
        
        # Use _build_card helper to ensure consistency with ReviewCard structure
        card = self._build_card(self.workflow_id, self.input.post, self.created_at)
        card_dict = card.to_dict()
        if self._post_text_removed:
            # issue #5: match the stored card after delete_post_text (X removal
            # duty): the post text and every evidence copy of it are gone
            card_dict["post"]["text"] = POST_TEXT_REMOVED
            card_dict["post"]["text_removed"] = True  # issue #9, as on the stored card
            card_dict["evidence"] = redact_post_text_copies(card_dict["evidence"])
        return card_dict
