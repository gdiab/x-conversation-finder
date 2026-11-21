"""Review workflow models."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .evidence import Evidence
from .post import Post


class ReviewStatus(str, Enum):
    """Review workflow status."""
    
    PENDING = "pending"
    APPROVED = "approved"
    APPROVED_NOT_CONFIRMED = "approved_not_confirmed"
    SKIPPED = "skipped"
    EXPIRED = "expired"
    FOLLOWUP_DUE = "followup_due"


@dataclass
class ReviewCard:
    """Complete review card shown in the UI."""
    
    workflow_id: str
    post: Post
    evidence: Evidence
    status: ReviewStatus
    created_at: str
    updated_at: str
    approved_by: Optional[str] = None
    posted_url: Optional[str] = None
    reply_post_id: Optional[str] = None
    skip_reason: Optional[str] = None
    client_request_id: Optional[str] = None
    followup_replies: Optional[list] = None
    
    def to_dict(self):
        """Convert to dict for serialization."""
        return {
            "workflow_id": self.workflow_id,
            "post": {
                "id": self.post.id,
                "text": self.post.text,
                "url": self.post.url,
                "author": {
                    "username": self.post.author.username,
                    "name": self.post.author.name,
                    "followers_count": self.post.author.followers_count,
                },
                "created_at": self.post.created_at,
                "like_count": self.post.like_count,
                "reply_count": self.post.reply_count,
            },
            "evidence": {
                "matched_topic": self.evidence.matched_topic,
                "why_it_fits": self.evidence.why_it_fits,
                "confidence_score": self.evidence.confidence_score,
                "confidence_inputs": self.evidence.confidence_inputs,
                "founder_materials": [
                    {
                        "source": m.source,
                        "passage": m.passage,
                        "relevance": m.relevance,
                    }
                    for m in self.evidence.founder_materials
                ],
                "draft": {
                    "text": self.evidence.draft.text,
                    "claim_gate": {
                        "passed": self.evidence.draft.claim_gate.passed,
                        "claims_checked": self.evidence.draft.claim_gate.claims_checked,
                        "unsupported_claims": self.evidence.draft.claim_gate.unsupported_claims,
                        "supported_claims": self.evidence.draft.claim_gate.supported_claims,
                        "reasoning": self.evidence.draft.claim_gate.reasoning,
                    },
                    "steers_applied": self.evidence.draft.steers_applied,
                } if self.evidence.draft else None,
                "skip_reason": self.evidence.skip_reason,
            },
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "approved_by": self.approved_by,
            "posted_url": self.posted_url,
            "reply_post_id": self.reply_post_id,
            "skip_reason": self.skip_reason,
            "followup_replies": self.followup_replies,
        }
