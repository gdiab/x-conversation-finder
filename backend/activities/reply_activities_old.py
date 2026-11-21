"""Reply agent activities."""

from dataclasses import dataclass
from typing import Optional
from temporalio import activity

from backend.models import Evidence, Post
from backend.adapters import FakeReplyAgent


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


@activity.defn(name="research_post")
async def research_post(input: ResearchPostInput) -> Evidence:
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
    
    agent = FakeReplyAgent()
    
    evidence = await agent.research(
        post=input.post,
        topic=input.topic,
        product_brief=input.product_brief,
        idempotency_key=input.idempotency_key,
    )
    
    activity.logger.info(
        f"Research complete: confidence={evidence.confidence_score:.2f}, "
        f"materials={len(evidence.founder_materials)}"
    )
    
    return evidence


@activity.defn(name="draft_reply")
async def draft_reply(input: DraftReplyInput) -> Evidence:
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
    
    agent = FakeReplyAgent()
    
    evidence = await agent.draft(
        evidence=input.evidence,
        steer=input.steer,
        idempotency_key=input.idempotency_key,
    )
    
    if evidence.draft:
        activity.logger.info(
            f"Draft complete: {len(evidence.draft.text)} chars, "
            f"claim_gate={'PASS' if evidence.draft.claim_gate.passed else 'FAIL'}"
        )
    
    return evidence
