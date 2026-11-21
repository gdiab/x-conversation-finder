"""Reply agent activities with injected agent."""

from dataclasses import dataclass
from typing import Optional
from temporalio import activity

from backend.models import Evidence, Post
from backend.ports import ReplyAgentPort
from backend.budget import reserve_inference_budget, reconcile_inference_tokens, BudgetExceeded


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
        - Compute confidence features
        
        Idempotent by post_id + topic hash.
        """
        activity.logger.info(
            f"Researching post {input.post.id} for topic '{input.topic}' "
            f"(key: {input.idempotency_key})"
        )
        
        # Reserve inference budget with token estimates (NB3: per-token pricing)
        # Estimate: ~500 input tokens, ~200 output tokens for research
        try:
            reserved_cost, reservation_date = reserve_inference_budget(input_tokens=500, output_tokens=200)
        except BudgetExceeded as e:
            activity.logger.error(f"Budget exceeded: {e}")
            raise
        
        evidence = await self._reply_agent.research(
            post=input.post,
            topic=input.topic,
            product_brief=input.product_brief,
            idempotency_key=input.idempotency_key,
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
                activity_id=activity.info().activity_id,
                attempt=activity.info().attempt,
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
            reserved_cost, reservation_date = reserve_inference_budget(input_tokens=800, output_tokens=100)
        except BudgetExceeded as e:
            activity.logger.error(f"Budget exceeded: {e}")
            raise
        
        evidence = await self._reply_agent.draft(
            evidence=input.evidence,
            steer=input.steer,
            idempotency_key=input.idempotency_key,
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
                    activity_id=activity.info().activity_id,
                    attempt=activity.info().attempt,
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
