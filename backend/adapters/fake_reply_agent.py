"""
Fake reply agent for testing.

No API calls, deterministic outputs based on post content.
"""

from datetime import datetime, timezone
from typing import Optional
import hashlib

from backend.models import (
    Evidence,
    Post,
    FounderMaterial,
    Claim,
    ClaimGateResult,
    Draft,
)
from backend.ports import ReplyAgentPort


class FakeReplyAgent(ReplyAgentPort):
    """Fake reply agent for testing and CI."""
    
    async def research(
        self,
        post: Post,
        topic: str,
        product_brief: str,
        idempotency_key: str,
    ) -> Evidence:
        """Generate fake evidence deterministically."""
        
        post_hash = int(hashlib.md5(post.id.encode()).hexdigest()[:8], 16)
        confidence_base = (post_hash % 100) / 100.0
        
        has_topic_keyword = topic.lower() in post.text.lower()
        
        confidence_inputs = {
            "lookup_succeeded": 1.0,
            "replies_open": 1.0 if post.reply_settings == "everyone" else 0.0,
            "topic_match_strength": 0.8 if has_topic_keyword else 0.3,
            "freshness": self._compute_freshness(post.created_at),
            "author_reach": min(1.0, (post.author.followers_count / 100000) * 0.5),
            "has_founder_material": 1.0,
        }
        
        evidence = Evidence(
            post_id=post.id,
            matched_topic=topic,
            why_it_fits=f"Post mentions '{topic}' and discusses relevant themes.",
            confidence_score=0.0,
            confidence_inputs=confidence_inputs,
            founder_materials=[
                FounderMaterial(
                    source="product_brief.md",
                    passage="Our product helps developers build reliable systems.",
                    relevance="Directly relevant to the post's discussion of reliability",
                    passage_id="passage_0",
                )
            ],
        )
        
        evidence.confidence_score = evidence.compute_confidence()
        
        return evidence
    
    async def draft(
        self,
        evidence: Evidence,
        steer: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Evidence:
        """Generate fake draft deterministically."""
        
        steers = evidence.draft.steers_applied if evidence.draft else []
        if steer:
            steers = steers + [steer]
        
        # R3-B1: Claim text must appear in draft
        claim_text = "Our product helps developers build reliable systems"
        
        base_text = (
            f"Thanks for raising this point about {evidence.matched_topic}. "
            f"{claim_text}. "
            "Would be happy to share more details if useful."
        )
        
        if "shorter" in (steer or "").lower():
            base_text = f"Great point about {evidence.matched_topic}. {claim_text}. Happy to share more."
        elif "technical" in (steer or "").lower():
            base_text = (
                f"Re: {evidence.matched_topic} - our system uses distributed "
                "consensus and eventual consistency to handle this. Details in our docs."
            )
            claim_text = "system uses distributed consensus"
        
        claims = [
            Claim(
                text=claim_text,
                cited_passage_ids=["passage_0"],
                quoted_spans=["helps developers build reliable systems"],
            )
        ]
        
        claim_gate = ClaimGateResult(
            passed=True,
            claims=claims,
            failed_claims=[],
            reasoning="Gate not yet run (will be verified by claim_gate activity)",
        )
        
        evidence.draft = Draft(
            text=base_text,
            claim_gate=claim_gate,
            steers_applied=steers,
            token_count=len(base_text.split()),
            model_used="fake-model",
        )
        
        return evidence
    
    def _compute_freshness(self, created_at: str) -> float:
        """Compute freshness score."""
        created_dt = datetime.fromisoformat(created_at)
        age_hours = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600
        
        if age_hours < 24:
            return 1.0
        elif age_hours < 48:
            return 0.5
        elif age_hours < 168:
            return 0.25
        else:
            return 0.1
