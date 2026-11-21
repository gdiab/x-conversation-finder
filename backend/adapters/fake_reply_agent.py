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
                    # Per Architect: short, standalone, claim-ready sentences
                    passage="Our product helps developers build reliable systems. Our product provides automatic failover. Our product guarantees strong consistency.",
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
        """
        Generate fake draft deterministically with segments.
        
        Per ADR-0006 Amendment 2026-11-21: same segment contract as plain adapter.
        """
        from backend.models import Segment
        
        steers = evidence.draft.steers_applied if evidence.draft else []
        if steer:
            steers = steers + [steer]
        
        # Per Architect: Claims have {kind:'claim', source_passage_id, sentence_index} with NO text
        # Non-claims have {kind:'non_claim', text}
        # Build segments based on steer
        if "shorter" in (steer or "").lower():
            segments = [
                Segment(
                    kind="claim",
                    source_passage_id="passage_0",
                    sentence_index=0  # "Our product helps developers build reliable systems."
                ),
                Segment(
                    kind="non_claim",
                    text="Happy to share more."
                ),
            ]
        elif "technical" in (steer or "").lower():
            segments = [
                Segment(
                    kind="claim",
                    source_passage_id="passage_0",
                    sentence_index=2  # "Our product guarantees strong consistency."
                ),
                Segment(
                    kind="non_claim",
                    text="Happy to share more details."
                ),
            ]
        else:
            # Default segments
            segments = [
                Segment(
                    kind="non_claim",
                    text="Thanks!"
                ),
                Segment(
                    kind="claim",
                    source_passage_id="passage_0",
                    sentence_index=0  # "Our product helps developers build reliable systems."
                ),
                Segment(
                    kind="non_claim",
                    text="Happy to share more details."
                ),
            ]
        
        # Construct draft text from segments by inserting claim sentences
        # (claim_gate will do this validation, but we need text for the draft)
        from backend.activities.claim_gate_activity import split_into_sentences
        
        text_parts = []
        for seg in segments:
            if seg.kind == "claim":
                # Insert the sentence from the passage
                passage = evidence.founder_materials[0].passage  # Simple: use first material
                sentences = split_into_sentences(passage)
                if seg.sentence_index is not None and 0 <= seg.sentence_index < len(sentences):
                    text_parts.append(sentences[seg.sentence_index].rstrip('.!?'))
            else:
                text_parts.append(seg.text)
        
        draft_text = ' '.join(text_parts)
        
        claim_gate = ClaimGateResult(
            passed=True,
            segments=segments,
            failed_sentences=[],
            reasoning="Gate not yet run (will be verified by claim_gate activity)",
        )
        
        # R4-B2: Compute token usage from text length (~1 token per 4 chars)
        # Input: evidence materials + steer (if any) + draft context
        input_text_len = 0
        for m in evidence.founder_materials:
            input_text_len += len(m.passage)
        if steer:
            input_text_len += len(steer)
        if evidence.draft and evidence.draft.text:
            input_text_len += len(evidence.draft.text)
        # Rough estimate: prompt overhead + context
        input_tokens = (input_text_len + 500) // 4
        
        # Output: new draft text
        output_tokens = len(draft_text) // 4
        
        evidence.draft = Draft(
            text=draft_text,
            segments=segments,
            claim_gate=claim_gate,
            steers_applied=steers,
            token_count=len(draft_text.split()),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_used="fake-model",
        )
        
        return evidence
    
    def _compute_freshness(self, created_at: str) -> float:
        """Compute freshness score."""
        created_dt = datetime.fromisoformat(created_at)
        # Ensure timezone-aware for comparison
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600
        
        if age_hours < 24:
            return 1.0
        elif age_hours < 48:
            return 0.5
        elif age_hours < 168:
            return 0.25
        else:
            return 0.1
