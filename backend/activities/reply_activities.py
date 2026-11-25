"""Reply agent activities with injected agent."""

from dataclasses import dataclass, field
from typing import List, Optional
from temporalio import activity
from temporalio.exceptions import ApplicationError

from backend.models import Evidence, Post, ReplyPlan
from backend.planning import (
    NEEDS_WRITTEN_DRAFTS_RULE,
    needs_written_drafts_skip_reason,
    plan_conformance_failures,
    validate_reply_plan,
)
from backend.ports import ReplyAgentPort
from backend.budget import meter_inference
from backend.activities.store_activities import card_post_text_removed
from backend.gate.reply_fit import assess_reply_fit


@dataclass
class ResearchPostInput:
    """Input for research_post activity."""
    
    post: Post
    topic: str
    product_brief: str
    idempotency_key: str


@dataclass
class DraftReplyInput:
    """Input for draft_reply activity."""
    
    evidence: Evidence
    steer: Optional[str] = None
    idempotency_key: str = ""
    plan: Optional[ReplyPlan] = None  # issue #5: validated reply plan, None before the plan step


@dataclass
class PlanReplyInput:
    """Input for plan_reply activity (issue #5)."""
    
    post: Post
    evidence: Evidence
    idempotency_key: str
    feedback: Optional[List[str]] = None  # previous attempt's validation errors


@dataclass
class PlanReplyResult:
    """Output of plan_reply: the proposed plan and code's validation errors (empty = valid).
    
    A valid plan can still skip the post: skip_rule and skip_reason are set
    when a plan-stage skip rule applies (today only needs_written_drafts).
    """
    
    plan: ReplyPlan
    errors: List[str] = field(default_factory=list)
    skip_rule: Optional[str] = None
    skip_reason: Optional[str] = None


class ReplyActivities:
    """Reply agent activities with injected agent."""
    
    def __init__(self, reply_agent: ReplyAgentPort):
        self._reply_agent = reply_agent
    
    @activity.defn(name="research_post")
    async def research_post(self, input: ResearchPostInput) -> Evidence:
        """
        Research a post and gather evidence.
        
        Calls the ReplyAgentPort to:
        - Analyze the post against the topic
        - Find relevant founder materials
        - Then, in code: check the proposed reply features, compute confidence,
          and apply the should-we-reply skip rules (backend/gate/reply_fit.py)
        
        Idempotent by post_id + topic hash.
        
        Budget (issue #8): the adapter reserves each paid call of this attempt
        through the meter, sized from its prompt, and reconciles it to the
        reported usage. Raises BudgetExceeded (non-retryable) at the cap.
        """
        activity.logger.info(
            f"Researching post {input.post.id} for topic '{input.topic}' "
            f"(key: {input.idempotency_key})"
        )
        
        with meter_inference() as meter:
            evidence = await self._reply_agent.research(
                post=input.post,
                topic=input.topic,
                product_brief=input.product_brief,
                idempotency_key=input.idempotency_key,
            )

        # Issue #4 / ADR-0006: code checks the proposed features, sets the
        # confidence and applies the skip rules, whatever the adapter returned.
        evidence = assess_reply_fit(evidence, input.post, input.product_brief)
        if evidence.skip_reason:
            activity.logger.info(
                f"Post {input.post.id} fails rule {evidence.reply_fit.skip_rule}: {evidence.skip_reason}"
            )
        
        activity.logger.info(
            f"Research complete: confidence={evidence.confidence_score:.2f}, "
            f"materials={len(evidence.founder_materials)}, {meter.summary()}"
        )
        
        return evidence
    
    @activity.defn(name="plan_reply")
    async def plan_reply(self, input: PlanReplyInput) -> PlanReplyResult:
        """
        Plan the reply before drafting (issue #5).
        
        Calls ReplyAgentPort.plan(), then validates the plan in code against the
        post text. Validation runs here, not in workflow code, so the verdict is
        recorded in history and replays identically even if the rules change.
        """
        activity.logger.info(
            f"Planning reply for post {input.post.id} "
            f"(retry={'yes' if input.feedback else 'no'}, key: {input.idempotency_key})"
        )
        
        with meter_inference() as meter:
            plan = await self._reply_agent.plan(
                post=input.post,
                evidence=input.evidence,
                idempotency_key=input.idempotency_key,
                feedback=input.feedback,
            )
        activity.logger.info(f"Plan budget: {meter.summary()}")
        
        errors = validate_reply_plan(plan, input.post.text)
        skip_rule = skip_reason = None
        if not errors:
            skip_reason = needs_written_drafts_skip_reason(plan)
            if skip_reason:
                skip_rule = NEEDS_WRITTEN_DRAFTS_RULE
        activity.logger.info(
            f"Plan: type={plan.reply_type!r} mention_product={plan.mention_product} "
            f"valid={not errors} skip_rule={skip_rule}"
        )
        return PlanReplyResult(plan=plan, errors=errors, skip_rule=skip_rule, skip_reason=skip_reason)
    
    @activity.defn(name="draft_reply")
    async def draft_reply(self, input: DraftReplyInput) -> Evidence:
        """
        Draft a reply with claim gate.
        
        Calls the ReplyAgentPort to:
        - Generate a draft reply
        - Check all product claims against founder materials
        - Apply any steering instructions
        
        Idempotent by evidence hash + steer.
        """
        activity.logger.info(
            f"Drafting reply for post {input.evidence.post_id} "
            f"steer={input.steer} (key: {input.idempotency_key})"
        )
        
        # Issue #9: once this Review's stored card records that the post was
        # removed (here or through another account's Review of it), its
        # evidence may quote a deleted post: do not send it to the model.
        if card_post_text_removed(activity.info().workflow_id):
            raise ApplicationError(
                "Post removed: the target post was deleted or is no longer visible on X; not redrafting",
                type="PostRemoved",
                non_retryable=True,
            )
        
        # Budget (issue #8): metered per paid call of this attempt; see research_post.
        with meter_inference() as meter:
            evidence = await self._reply_agent.draft(
                evidence=input.evidence,
                steer=input.steer,
                idempotency_key=input.idempotency_key,
                plan=input.plan,
            )
        
        if evidence.draft and input.plan is not None:
            # issue #5: record, in code, whether the draft follows the plan
            evidence.draft.plan_conformance_failures = plan_conformance_failures(
                evidence.draft.segments, input.plan
            )
        
        if evidence.draft:
            activity.logger.info(
                f"Draft complete: {len(evidence.draft.text)} chars, "
                f"claim_gate={'PASS' if evidence.draft.claim_gate.passed else 'FAIL'}, "
                f"{meter.summary()}"
            )
        
        return evidence
