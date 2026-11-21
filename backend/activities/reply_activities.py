"""Reply agent activities with injected agent."""

from dataclasses import dataclass
from typing import Optional
from temporalio import activity

from backend.models import Evidence, Post
from backend.ports import ReplyAgentPort
from backend.budget import check_inference_budget, record_inference


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
        self.reply_agent = reply_agent
    
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
        
        # Check inference budget
        if not check_inference_budget():
            activity.logger.warning("Daily inference budget exceeded")
            raise RuntimeError("Daily inference budget exceeded")
        
        evidence = await self.reply_agent.research(
            post=input.post,
            topic=input.topic,
            product_brief=input.product_brief,
            idempotency_key=input.idempotency_key,
        )
        
        # Record inference (estimate)
        record_inference(cost_usd=0.001)
        
        activity.logger.info(
            f"Research complete: confidence={evidence.confidence_score:.2f}, "
            f"materials={len(evidence.founder_materials)}"
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
        
        # Check inference budget
        if not check_inference_budget():
            activity.logger.warning("Daily inference budget exceeded")
            raise RuntimeError("Daily inference budget exceeded")
        
        evidence = await self.reply_agent.draft(
            evidence=input.evidence,
            steer=input.steer,
            idempotency_key=input.idempotency_key,
        )
        
        # Record inference (estimate)
        record_inference(cost_usd=0.002)
        
        if evidence.draft:
            activity.logger.info(
                f"Draft complete: {len(evidence.draft.text)} chars, "
                f"claim_gate={'PASS' if evidence.draft.claim_gate.passed else 'FAIL'}"
            )
        
        return evidence
