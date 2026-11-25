"""
Fake reply agent for testing.

No API calls, deterministic outputs based on post content.
"""

from typing import List, Optional
import re

from backend.models import (
    Evidence,
    Post,
    FounderMaterial,
    Claim,
    ClaimGateResult,
    Draft,
    ReplyFit,
    ReplyPlan,
)
from backend.ports import ReplyAgentPort
from backend.planning import question_spans
from backend.gate.sentence_boundary import SPLITTER_VERSION

# issue #5: the closer the fake draft uses for each planned reply type.
_FAKE_CLOSER_BY_REPLY_TYPE = {
    "answer_question": "offer.share_details",
    "add_observation": "q.current_tool",
    "share_experience": "offer.love_to_hear",
}



def _brief_material(product_brief: str) -> list[FounderMaterial]:
    """The brief's first prose paragraph (no heading or list), as passage_0.

    The default fake draft claims sentence 0 of passage_0 (sentence 2 when
    steered "technical"), so a brief whose first paragraph has short,
    standalone sentences drafts cleanly; the test brief (conftest.py) does.
    """
    for block in re.split(r"\n\s*\n", product_brief):
        lines = [line.strip() for line in block.strip().splitlines()]
        if not lines or any(line.startswith(("#", "-", "*")) for line in lines):
            continue
        return [FounderMaterial(
            source="product_brief.md",
            passage=" ".join(lines),
            relevance="First paragraph of the product brief",
            passage_id="passage_0",
        )]
    return []

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
        Its founder passage is the brief's first paragraph, so it passes the
        check that passages appear in the brief, as the real model's must.
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
            founder_materials=_brief_material(product_brief),
            reply_fit=ReplyFit(
                post_kind=post_kind,
                brief_problem_span=span,
                in_others_thread=bool(post.in_reply_to_user_id),
                is_vendor_post=False,
            ),
        )
        
        return evidence
    
    async def plan(
        self,
        post: Post,
        evidence: Evidence,
        idempotency_key: str,
        feedback: Optional[List[str]] = None,
    ) -> ReplyPlan:
        """
        Plan deterministically (issue #5).
        
        A post with a question gets answer_question, targeting the first
        question sentence; otherwise add_observation, targeting the first
        sentence. The fake always mentions the product, so existing fake
        drafts keep their claim; tests subclass this to plan otherwise.
        """
        spans = question_spans(post.text)
        if spans:
            start, end = spans[0]
            reply_type = "answer_question"
            target_point = post.text[start:end].strip()
        else:
            match = re.match(r"\s*([^.!?\n]+[.!?]?)", post.text)
            reply_type = "add_observation"
            target_point = match.group(1).strip() if match else post.text.strip()
        
        return ReplyPlan(
            reply_type=reply_type,
            target_point=target_point,
            mention_product=True,
            mention_product_reason="Fake planner: the founder material covers this topic.",
            model_used="fake-model",
            input_tokens=(len(post.text) + 400) // 4,
            output_tokens=40,
        )
    
    async def draft(
        self,
        evidence: Evidence,
        steer: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        plan: Optional[ReplyPlan] = None,
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
                    splitter_version=SPLITTER_VERSION,
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
                    splitter_version=SPLITTER_VERSION,
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
                    splitter_version=SPLITTER_VERSION,
                    sentence_index=0  # "Our product helps developers build reliable systems."
                ),
                Segment(
                    kind="non_claim",
                    # issue #5: the closer follows the planned reply type
                    phrase_id=_FAKE_CLOSER_BY_REPLY_TYPE.get(
                        plan.reply_type if plan else "", "offer.share_details"
                    ),
                ),
            ]
        
        # issue #5: the fake follows a plan that says not to mention the product
        if plan is not None and plan.mention_product is False:
            segments = [s for s in segments if s.kind != "claim"]
        
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
