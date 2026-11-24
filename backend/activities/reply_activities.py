"""Reply agent activities with injected agent."""

from dataclasses import dataclass, field
from typing import List, Optional
from temporalio import activity

from backend.models import Evidence, Post, ReplyPlan
from backend.planning import plan_conformance_failures, validate_reply_plan
from backend.ports import ReplyAgentPort
from backend.budget import reserve_inference_budget, reconcile_inference_tokens, BudgetExceeded
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
    """Output of plan_reply: the proposed plan and code's validation errors (empty = valid)."""
    
    plan: ReplyPlan
    errors: List[str] = field(default_factory=list)


# Budget estimate for one planning call (tokens).
PLAN_RESERVED_INPUT_TOKENS = 700
PLAN_RESERVED_OUTPUT_TOKENS = 120


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
        """
        activity.logger.info(
            f"Researching post {input.post.id} for topic '{input.topic}' "
            f"(key: {input.idempotency_key})"
        )
        
        # Reserve inference budget with token estimates (NB3: per-token pricing)
        # Estimate: ~500 input tokens, ~200 output tokens for research
        try:
            reserved_cost, reservation_date = reserve_inference_budget(
                input_tokens=500,
                output_tokens=200,
                workflow_id=activity.info().workflow_id,
                workflow_run_id=activity.info().workflow_run_id,
                activity_id=activity.info().activity_id,
            )
        except BudgetExceeded as e:
            activity.logger.error(f"Budget exceeded: {e}")
            raise
        
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
        
        # R5-B1: Reconcile inference budget (research_post must reconcile)
        # If usage is missing from LLM response, keep the full reservation
        if hasattr(evidence, 'input_tokens') and hasattr(evidence, 'output_tokens'):
            actual_cost = reconcile_inference_tokens(
                reserved_input=500,
                reserved_output=200,
                actual_input=evidence.input_tokens,
                actual_output=evidence.output_tokens,
                reservation_date=reservation_date,
                workflow_id=activity.info().workflow_id,
                workflow_run_id=activity.info().workflow_run_id,
                activity_id=activity.info().activity_id,
            )
            activity.logger.info(
                f"Research complete: confidence={evidence.confidence_score:.2f}, "
                f"materials={len(evidence.founder_materials)}, "
                f"tokens={evidence.input_tokens}/{evidence.output_tokens}, cost=${actual_cost:.4f}"
            )
        else:
            # Missing usage: keep full reservation as spent (R5-B1)
            activity.logger.info(
                f"Research complete: confidence={evidence.confidence_score:.2f}, "
                f"materials={len(evidence.founder_materials)}, "
                f"usage unavailable, kept reservation ${reserved_cost:.4f}"
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
        
        try:
            reserved_cost, reservation_date = reserve_inference_budget(
                input_tokens=PLAN_RESERVED_INPUT_TOKENS,
                output_tokens=PLAN_RESERVED_OUTPUT_TOKENS,
                workflow_id=activity.info().workflow_id,
                workflow_run_id=activity.info().workflow_run_id,
                activity_id=activity.info().activity_id,
            )
        except BudgetExceeded as e:
            activity.logger.error(f"Budget exceeded: {e}")
            raise
        
        plan = await self._reply_agent.plan(
            post=input.post,
            evidence=input.evidence,
            idempotency_key=input.idempotency_key,
            feedback=input.feedback,
        )
        
        if plan.input_tokens > 0 or plan.output_tokens > 0:
            reconcile_inference_tokens(
                reserved_input=PLAN_RESERVED_INPUT_TOKENS,
                reserved_output=PLAN_RESERVED_OUTPUT_TOKENS,
                actual_input=plan.input_tokens,
                actual_output=plan.output_tokens,
                reservation_date=reservation_date,
                workflow_id=activity.info().workflow_id,
                workflow_run_id=activity.info().workflow_run_id,
                activity_id=activity.info().activity_id,
            )
        else:
            activity.logger.info(f"Plan usage unavailable, kept reservation ${reserved_cost:.4f}")
        
        errors = validate_reply_plan(plan, input.post.text)
        activity.logger.info(
            f"Plan: type={plan.reply_type!r} mention_product={plan.mention_product} "
            f"valid={not errors}"
        )
        return PlanReplyResult(plan=plan, errors=errors)
    
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
        
        # Reserve inference budget with token estimates (NB3: per-token pricing)
        # Estimate: ~800 input tokens, ~100 output tokens for draft
        try:
            reserved_cost, reservation_date = reserve_inference_budget(
                input_tokens=800,
                output_tokens=100,
                workflow_id=activity.info().workflow_id,
                workflow_run_id=activity.info().workflow_run_id,
                activity_id=activity.info().activity_id,
            )
        except BudgetExceeded as e:
            activity.logger.error(f"Budget exceeded: {e}")
            raise
        
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
            # R5-B1: Reconcile inference budget (two-way, idempotent)
            # If usage is missing (0/0), keep the full reservation
            if evidence.draft.input_tokens > 0 or evidence.draft.output_tokens > 0:
                actual_cost = reconcile_inference_tokens(
                    reserved_input=800,
                    reserved_output=100,
                    actual_input=evidence.draft.input_tokens,
                    actual_output=evidence.draft.output_tokens,
                    reservation_date=reservation_date,
                    workflow_id=activity.info().workflow_id,
                    workflow_run_id=activity.info().workflow_run_id,
                    activity_id=activity.info().activity_id,
                )
                activity.logger.info(
                    f"Draft complete: {len(evidence.draft.text)} chars, "
                    f"claim_gate={'PASS' if evidence.draft.claim_gate.passed else 'FAIL'}, "
                    f"tokens={evidence.draft.input_tokens}/{evidence.draft.output_tokens}, "
                    f"cost=${actual_cost:.6f}"
                )
            else:
                # Missing usage (0/0): keep full reservation as spent (R5-B1)
                activity.logger.info(
                    f"Draft complete: {len(evidence.draft.text)} chars, "
                    f"claim_gate={'PASS' if evidence.draft.claim_gate.passed else 'FAIL'}, "
                    f"usage unavailable, kept reservation ${reserved_cost:.4f}"
                )
        
        return evidence
