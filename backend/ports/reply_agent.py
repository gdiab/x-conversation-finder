"""
ReplyAgentPort - abstraction for reply generation.

Per ADR-0003: the Agent Kit sits behind this port.
The plain activity adapter is the baseline and default.

Budget (issue #8): an adapter that makes paid inference calls reserves each one
through backend.budget.current_inference_meter() right before the request,
sized from an upper bound on the prompt's tokens and the max output tokens it
sends, and settles it after (see PlainReplyAgent._complete). A failed request
stays counted. The activities open the meter.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from backend.models import BriefSentence, Evidence, Post, ReplyPlan


@dataclass
class WrittenDraftRequest:
    """What the written drafter works from (issue #12).

    Unlike the claim checker (ClaimCheckerPort), the drafter sees the post and
    the reply plan: its job is to respond to the post's specific point.
    """

    post: Post
    evidence: Evidence
    plan: ReplyPlan
    brief_sentences: List[BriefSentence]
    product_name: str
    product_aliases: List[str] = field(default_factory=list)
    feature_terms: List[str] = field(default_factory=list)
    steer: Optional[str] = None
    # issue #41: the reply's length limit. Lower than X's 280 when code will
    # append the watch's product URL (the plan says to mention the product):
    # X counts the link as 23 characters, plus the space before it.
    max_chars: int = 280


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
        validation records the reasons. Output cut off at the max output tokens
        is a configuration error and may raise (issue #8).
        """
        pass
    
    @abstractmethod
    async def draft(
        self,
        evidence: Evidence,
        steer: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        plan: Optional[ReplyPlan] = None,
        max_chars: int = 280,
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

        `max_chars` is the reply's length limit after text insertion (issue
        #41: lower when code will append the watch's product URL).
        
        Returns updated evidence.
        """
        pass
    
    @abstractmethod
    async def draft_written(
        self,
        request: WrittenDraftRequest,
        idempotency_key: Optional[str] = None,
    ) -> Evidence:
        """
        Write a reply as free text (issue #12; ADR-0006 amendment 2026-11-24).
        
        Sets request.evidence.draft to a Draft with mode "written": `text` is
        the model's reply (at most 3 sentences and 280 characters, at most one
        product claim), `segments` is empty, and `written.drafter_hints` lists,
        for each sentence the model says is about the product, the brief
        sentence ids it says support it. The hints are never a verdict, and
        they are not passed to the claim checker either (it gets no drafting
        context, see ClaimCheckerPort): they stay on the draft as provenance.
        Nothing the drafter returns decides what is checked: the
        check_written_draft activity splits the text, runs the triggers and
        asks the independent checker.
        
        Adapters must not raise on malformed model output: return what was
        parsed (an empty text fails the check and is redrafted).
        
        Returns the updated evidence.
        """
        pass
