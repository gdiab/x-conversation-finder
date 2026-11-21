"""
Plain LLM reply agent - baseline implementation.

Single-prompt calls, no Agent Kit, no tool loop.
This is the baseline for ADR-0006 evaluation.
"""

import json
from datetime import datetime, timezone
from typing import Optional

from openai import AsyncOpenAI

from backend.config import get_settings
from backend.models import (
    Evidence,
    Post,
    FounderMaterial,
    Claim,
    ClaimGateResult,
    Draft,
)
from backend.ports import ReplyAgentPort


class PlainReplyAgent(ReplyAgentPort):
    """Plain LLM adapter - baseline implementation."""
    
    def __init__(self, openai_client: Optional[AsyncOpenAI] = None):
        settings = get_settings()
        self.client = openai_client or AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        )
        self.model = settings.openai_model
    
    async def research(
        self,
        post: Post,
        topic: str,
        product_brief: str,
        idempotency_key: str,
    ) -> Evidence:
        """Research post and gather evidence."""
        
        prompt = f"""Analyze this X post for potential reply:

Post: {post.text}
Author: @{post.author.username} ({post.author.followers_count} followers)
Topic: {topic}

Product Brief:
{product_brief}

Return JSON with:
- matched_topic: which topic this relates to
- why_it_fits: short explanation quoting the post
- founder_materials: list of {{source, passage, relevance}} from the brief
- topic_match_strength: 0.0-1.0 score

Be honest about uncertainty. If the post doesn't fit well, say so."""

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a research assistant. Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        
        result = json.loads(response.choices[0].message.content)
        
        confidence_inputs = {
            "lookup_succeeded": 1.0,
            "replies_open": 1.0 if post.reply_settings == "everyone" else 0.0,
            "topic_match_strength": result.get("topic_match_strength", 0.5),
            "freshness": self._compute_freshness(post.created_at),
            "author_reach": self._compute_reach(post.author.followers_count),
            "has_founder_material": 1.0 if result.get("founder_materials") else 0.0,
        }
        
        founder_materials = []
        for i, m in enumerate(result.get("founder_materials", [])):
            material = FounderMaterial(
                source=m["source"],
                passage=m["passage"],
                relevance=m["relevance"],
                passage_id=f"passage_{i}",
            )
            founder_materials.append(material)
        
        evidence = Evidence(
            post_id=post.id,
            matched_topic=result["matched_topic"],
            why_it_fits=result["why_it_fits"],
            confidence_score=0.0,
            confidence_inputs=confidence_inputs,
            founder_materials=founder_materials,
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
        Draft a reply with structured segments.
        
        Per ADR-0006 Amendment 2026-11-21:
        The LLM outputs segments {text, kind, source_passage_id?, quote?}.
        Activity payloads use plain str Literals, not str-Enums.
        """
        materials_text = "\n\n".join(
            f"[{m.passage_id}] Source: {m.source}\n{m.passage}"
            for m in evidence.founder_materials
        )
        
        steer_text = f"\n\nAdditional instruction: {steer}" if steer else ""
        
        # QA R13: List available phrases for the model
        available_phrases = """OPENERS: opener.hi, opener.hello, opener.hey, opener.thanks, opener.great_question
THANKS: thanks.for_sharing, thanks.for_asking, thanks.for_question, thanks.interesting
OFFERS: offer.happy_to_help, offer.happy_to_share, offer.share_details, offer.glad_to_help, offer.love_to_hear, offer.feel_free
QUESTIONS: q.current_tool, q.stack, q.use_case, q.context, q.tried, q.looking_for, q.problem"""
        
        prompt = f"""Draft a reply to this post as structured segments.

CRITICAL RULES - QA R13:
- Claims are QUOTED, not paraphrased. You reference a passage sentence by its index (0-based).
- For claims: output ONLY {{"kind": "claim", "source_passage_id": "passage_X", "sentence_index": N}}
- For non-claims: output {{"kind": "non_claim", "phrase_id": "phrase.id"}} - select from the fixed list below
- The code will insert the exact passage sentence for claims and the phrase text for non-claims.
- You do NOT write ANY free text. Claims are sentence indices, non-claims are phrase IDs.

Available Phrase IDs (select one by exact ID):
{available_phrases}

Why this post fits: {evidence.why_it_fits}

Founder Materials (sentence indices shown):
{materials_text}
{steer_text}

Return JSON with:
- segments: list of objects (1-3 segments, at most 1 claim):
  - For claims: {{"kind": "claim", "source_passage_id": "passage_X", "sentence_index": N}}
  - For non-claims: {{"kind": "non_claim", "phrase_id": "exact.phrase.id"}}

Example:
[
  {{"kind": "non_claim", "phrase_id": "opener.thanks"}},
  {{"kind": "claim", "source_passage_id": "passage_0", "sentence_index": 0}},
  {{"kind": "non_claim", "phrase_id": "offer.share_details"}}
]

Be conversational and honest. Never invent features. Reference founder material by sentence index only.
Max 280 chars total after text insertion."""

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant. Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
        )
        
        result = json.loads(response.choices[0].message.content)
        
        from backend.models import Segment
        from backend.activities.claim_gate_activity import split_into_sentences
        from backend.gate.non_claim_set import get_phrase_text
        
        # QA R13: Claims have {kind, source_passage_id, sentence_index}, non-claims have {kind, phrase_id}
        segments = []
        for s in result.get("segments", []):
            if s["kind"] == "claim":
                segments.append(Segment(
                    kind="claim",
                    source_passage_id=s.get("source_passage_id"),
                    sentence_index=s.get("sentence_index")
                ))
            else:
                # QA R13: Non-claims use phrase_id (no text)
                segments.append(Segment(
                    kind="non_claim",
                    phrase_id=s.get("phrase_id")
                ))
        
        # QA R13: Construct draft text by inserting claim sentences and phrase texts
        # (claim_gate will validate, but we need text for the draft)
        text_parts = []
        for seg in segments:
            if seg.kind == "claim":
                # Insert the sentence from the passage
                if seg.source_passage_id and seg.sentence_index is not None:
                    # Find the passage
                    passage = None
                    for m in evidence.founder_materials:
                        if m.passage_id == seg.source_passage_id:
                            passage = m.passage
                            break
                    if passage:
                        sentences = split_into_sentences(passage)
                        if 0 <= seg.sentence_index < len(sentences):
                            text_parts.append(sentences[seg.sentence_index].rstrip('.!?'))
            else:
                # QA R13: Look up phrase text by phrase_id
                if seg.phrase_id:
                    phrase_text = get_phrase_text(seg.phrase_id)
                    if phrase_text:
                        text_parts.append(phrase_text.rstrip('.!?'))
        
        draft_text = ' '.join(text_parts)
        
        claim_gate = ClaimGateResult(
            passed=True,
            segments=segments,
            failed_sentences=[],
            reasoning="Gate not yet run (will be verified by claim_gate activity)",
        )
        
        steers = evidence.draft.steers_applied if evidence.draft else []
        if steer:
            steers = steers + [steer]
        
        # R4-B2: Extract real token usage from response
        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        
        evidence.draft = Draft(
            text=draft_text,
            segments=segments,
            claim_gate=claim_gate,
            steers_applied=steers,
            token_count=len(draft_text.split()),
            model_used=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
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
    
    def _compute_reach(self, followers: int) -> float:
        """Compute author reach score."""
        import math
        if followers <= 0:
            return 0.0
        return min(1.0, math.log10(followers) / 6.0)
