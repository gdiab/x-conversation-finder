"""
ReplyAgentPort - abstraction for reply generation.

Per ADR-0003: the Agent Kit sits behind this port.
The plain activity adapter is the baseline and default.
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from backend.models import Evidence, Post, ReplyPlan


class ReplyAgentPort(ABC):
    """Port for reply scoring and drafting."""
    
    @abstractmethod
    async def research(
        self,
        post: Post,
        topic: str,
        product_brief: str,
        idempotency_key: str,
    ) -> Evidence:
        """
        Research a post and gather evidence.
        
        Returns Evidence with:
        - matched_topic
        - why_it_fits
        - confidence_inputs (raw features)
        - founder_materials
        - computed confidence_score
        """
        pass
    
    @abstractmethod
    async def plan(
        self,
        post: Post,
        evidence: Evidence,
        idempotency_key: str,
        feedback: Optional[List[str]] = None,
    ) -> ReplyPlan:
        """
        Plan the reply before drafting (issue #5).
        
        Returns a ReplyPlan: reply_type, target_point (an exact span of
        post.text), mention_product and a one-line reason. The plan is the
        model's proposal only; the plan_reply activity validates it in code.
        `feedback` carries the previous attempt's validation errors when the
        plan is re-requested. Adapters must not raise on malformed model output:
        return what was parsed (unknown or missing values as ""/None) so that
        validation records the reasons.
        """
        pass
    
    @abstractmethod
    async def draft(
        self,
        evidence: Evidence,
        steer: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        plan: Optional[ReplyPlan] = None,
    ) -> Evidence:
        """
        Draft a reply based on evidence.
        
        Updates evidence.draft with:
        - text
        - claim_gate result
        - steers_applied
        
        `plan` is the validated reply plan (None for Review workflows started
        before the plan step existed). Under assembled drafts it steers the
        closer choice and whether a claim is included. The draft_reply activity
        checks the result against the plan in code
        (backend.planning.plan_conformance_failures); a non-conforming draft is
        redrafted once, then skipped.
        
        Returns updated evidence.
        """
        pass
