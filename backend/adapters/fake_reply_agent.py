"""
Fake reply agent for testing.

No API calls, deterministic outputs based on post content.
"""

from typing import Optional
import re

from backend.models import (
    Evidence,
    Post,
    FounderMaterial,
    Claim,
    ClaimGateResult,
    Draft,
    ReplyFit,
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
        """Propose reply features deterministically from the post text.

        Stands in for the research model: the span is the first sentence with a
        question mark, else the first sentence naming the topic, else None.
        The research_post activity checks the features and sets confidence.
        """
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", post.text.strip()) if s]
        span = next((s for s in sentences if "?" in s), None)
        if span is None:
            span = next((s for s in sentences if topic.lower() in s.lower()), None)

        if "?" in post.text:
            post_kind = "question"
        elif span is not None:
            post_kind = "problem"
        else:
            post_kind = "opinion"
        
        evidence = Evidence(
            post_id=post.id,
            matched_topic=topic,
            why_it_fits=f"Post mentions '{topic}' and discusses relevant themes.",
            confidence_score=0.0,
            confidence_inputs={},
            founder_materials=[
                FounderMaterial(
                    source="product_brief.md",
                    # Per Architect: short, standalone, claim-ready sentences
                    passage="Our product helps developers build reliable distributed systems. Our product provides automatic failover. Our product guarantees strong consistency.",
                    relevance="Directly relevant to the post's discussion of reliability",
                    passage_id="passage_0",
                )
            ],
            reply_fit=ReplyFit(
                post_kind=post_kind,
                brief_problem_span=span,
                in_others_thread=bool(post.in_reply_to_user_id),
                is_vendor_post=False,
            ),
        )
        
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
        
        # QA R13: Claims have {kind:'claim', source_passage_id, sentence_index} with NO text
        # Non-claims have {kind:'non_claim', phrase_id} with NO text
        # QA R14: Use v2.1.0 phrase_ids only (no offer.happy_to_share, etc.)
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
                    phrase_id="offer.happy_to_help"  # v2.1.0 phrase
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
                    phrase_id="offer.share_details"  # v2.1.0 phrase
                ),
            ]
        else:
            # Default segments
            segments = [
                Segment(
                    kind="non_claim",
                    phrase_id="opener.thanks"  # v2.1.0 phrase
                ),
                Segment(
                    kind="claim",
                    source_passage_id="passage_0",
                    sentence_index=0  # "Our product helps developers build reliable systems."
                ),
                Segment(
                    kind="non_claim",
                    phrase_id="offer.share_details"  # v2.1.0 phrase
                ),
            ]
        
        # QA R13: Construct draft text from segments (claim_gate will validate and insert text)
        # For FakeReplyAgent, we need to construct the text ourselves for the draft
        from backend.activities.claim_gate_activity import split_into_sentences
        from backend.gate.non_claim_set import get_phrase_text
        
        text_parts = []
        for seg in segments:
            if seg.kind == "claim":
                # Insert the sentence from the passage WITH trailing period
                passage = evidence.founder_materials[0].passage
                sentences = split_into_sentences(passage)
                if seg.sentence_index is not None and 0 <= seg.sentence_index < len(sentences):
                    text_parts.append(sentences[seg.sentence_index])
            else:
                # QA R13: Look up phrase by phrase_id
                if seg.phrase_id:
                    phrase_text = get_phrase_text(seg.phrase_id)
                    if phrase_text:
                        # Ensure phrase text ends with punctuation
                        if not phrase_text.endswith(('.', '!', '?')):
                            phrase_text = phrase_text + '.'
                        text_parts.append(phrase_text)
        
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
