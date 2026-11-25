"""Reply agent activities with injected agent."""

from dataclasses import dataclass, field
from typing import List, Optional
from temporalio import activity
from temporalio.exceptions import ApplicationError

from backend.config import get_settings
from backend.gate.brief import brief_feature_terms, split_brief
from backend.gate.written_check import clean_names
from backend.models import Evidence, Post, ReplyPlan
from backend.models.written import DRAFT_MODE_WRITTEN
from backend.planning import (
    NEEDS_WRITTEN_DRAFTS_RULE,
    needs_written_drafts_skip_reason,
    plan_conformance_failures,
    resolve_draft_mode,
    validate_reply_plan,
)
from backend.ports import ReplyAgentPort
from backend.ports.reply_agent import WrittenDraftRequest
from backend.budget import meter_inference
from backend.activities.store_activities import card_post_text_removed, post_removal_recorded
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
    # --- Written drafts (issue #12). The defaults are what Reviews recorded
    # before the fields existed decode to: an assembled draft. ---
    draft_mode: Optional[str] = None  # the Review's mode, fixed by plan_reply
    post: Optional[Post] = None  # the written drafter responds to the post itself
    product_brief: str = ""
    product_name: str = ""
    product_aliases: Optional[List[str]] = None


@dataclass
class PlanReplyInput:
    """Input for plan_reply activity (issue #5)."""
    
    post: Post
    evidence: Evidence
    idempotency_key: str
    feedback: Optional[List[str]] = None  # previous attempt's validation errors
    # issue #12: the watch's product name; written mode needs one
    product_name: str = ""


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
    # issue #12: the draft mode this Review uses from here on ("assembled" or
    # "written"), and why written mode was asked for but not used. None in
    # results recorded before the setting existed: those Reviews stay assembled.
    draft_mode: Optional[str] = None
    draft_mode_note: Optional[str] = None


class ReplyActivities:
    """Reply agent activities with injected agent."""
    
    def __init__(self, reply_agent: ReplyAgentPort, draft_mode: Optional[str] = None):
        """draft_mode: "assembled" or "written" (issue #12). None reads the
        DRAFT_MODE setting when a Review plans its reply."""
        self._reply_agent = reply_agent
        self._draft_mode = draft_mode
    
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
        
        # issue #12: fix this Review's draft mode here, so it is in history.
        # A written draft can answer a question or share experience without a
        # product claim, so needs_written_drafts applies to assembled only.
        draft_mode, draft_mode_note = resolve_draft_mode(
            self._draft_mode or get_settings().draft_mode, input.product_name
        )
        
        errors = validate_reply_plan(plan, input.post.text)
        skip_rule = skip_reason = None
        if not errors:
            skip_reason = needs_written_drafts_skip_reason(
                plan, enabled=False if draft_mode == DRAFT_MODE_WRITTEN else None
            )
            if skip_reason:
                skip_rule = NEEDS_WRITTEN_DRAFTS_RULE
        activity.logger.info(
            f"Plan: type={plan.reply_type!r} mention_product={plan.mention_product} "
            f"valid={not errors} skip_rule={skip_rule} draft_mode={draft_mode}"
        )
        return PlanReplyResult(
            plan=plan,
            errors=errors,
            skip_rule=skip_rule,
            skip_reason=skip_reason,
            draft_mode=draft_mode,
            draft_mode_note=draft_mode_note,
        )
    
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
        
        written = input.draft_mode == DRAFT_MODE_WRITTEN
        if written and (input.plan is None or input.post is None):
            raise ApplicationError(
                "A written draft needs the reply plan and the post", non_retryable=True
            )
        # Issue #12: the written drafter is sent the post itself, so it also
        # stops when another Review of this post (another account's) has
        # recorded its removal, before this Review has a card of its own.
        # The Review then ends as skipped "post removed".
        if written and post_removal_recorded(input.evidence.post_id):
            raise ApplicationError(
                "Post removed: the target post was deleted or is no longer visible on X; not drafting",
                type="PostRemoved",
                non_retryable=True,
            )
        
        # Budget (issue #8): metered per paid call of this attempt; see research_post.
        with meter_inference() as meter:
            if written:
                # issue #12: the model writes the reply. The brief is split
                # here, inside the activity; check_written_draft splits it
                # again and decides what is a product claim.
                name, aliases = clean_names(input.product_name, input.product_aliases)
                evidence = await self._reply_agent.draft_written(
                    WrittenDraftRequest(
                        post=input.post,
                        evidence=input.evidence,
                        plan=input.plan,
                        brief_sentences=split_brief(input.product_brief),
                        product_name=name,
                        product_aliases=aliases,
                        feature_terms=brief_feature_terms(input.product_brief),
                        steer=input.steer,
                    ),
                    idempotency_key=input.idempotency_key,
                )
            else:
                evidence = await self._reply_agent.draft(
                    evidence=input.evidence,
                    steer=input.steer,
                    idempotency_key=input.idempotency_key,
                    plan=input.plan,
                )
        
        if evidence.draft and input.plan is not None and not written:
            # issue #5: record, in code, whether the draft follows the plan.
            # For a written draft the check_written_draft activity does this:
            # only the check knows which sentences are product claims.
            evidence.draft.plan_conformance_failures = plan_conformance_failures(
                evidence.draft.segments, input.plan
            )
        
        if evidence.draft:
            activity.logger.info(
                f"Draft complete ({evidence.draft.mode}): {len(evidence.draft.text)} chars, "
                f"{meter.summary()}"
            )
        
        return evidence
