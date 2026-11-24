"""
Plain LLM reply agent - baseline implementation.

Single-prompt calls, no Agent Kit, no tool loop.
This is the baseline for ADR-0006 evaluation.
"""

import json
from typing import Any, Optional

from openai import AsyncOpenAI

from backend.config import get_settings
from backend.models import (
    Evidence,
    Post,
    FounderMaterial,
    Claim,
    ClaimGateResult,
    Draft,
    ReplyFit,
)
from backend.config.confidence import POST_KINDS
from backend.ports import ReplyAgentPort
from backend.activities.claim_gate_activity import (
    split_into_sentences,
    has_unresolved_referent,
    ends_with_abbreviation,
)
from backend.gate.non_claim_set import NON_CLAIM_PHRASES


def format_materials_for_prompt(materials: list[FounderMaterial]) -> str:
    """List each passage's selectable sentences under the numbers the claim gate uses.

    Numbers are indices into split_into_sentences(passage), the same split the
    claim gate checks against. Sentences the gate never accepts as claims (an
    unresolved referent at the start, or an abbreviation at the end) are left
    out, but the remaining sentences keep their original numbers.
    """
    blocks = []
    for m in materials:
        lines = [f"[{m.passage_id}] Source: {m.source}"]
        for i, sentence in enumerate(split_into_sentences(m.passage)):
            if has_unresolved_referent(sentence) or ends_with_abbreviation(sentence):
                continue
            lines.append(f"  {i}: {sentence}")
        if len(lines) == 1:
            lines.append("  (no selectable sentences)")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_phrases_for_prompt() -> str:
    """List the current fixed phrase set, grouped by category, from the code's own list."""
    labels = {
        "opener": "OPENERS",
        "thanks": "THANKS",
        "offer": "OFFERS (closer)",
        "question_to_poster": "QUESTIONS (closer)",
    }
    groups: dict[str, list[str]] = {}
    for p in NON_CLAIM_PHRASES:
        groups.setdefault(p.category, []).append(f"  {p.id}: {p.text}")
    return "\n".join(
        f"{labels.get(cat, cat.upper())}:\n" + "\n".join(lines)
        for cat, lines in groups.items()
    )


def _strict_bool(value: Any) -> Optional[bool]:
    """A JSON boolean, or the strings "true"/"false"; anything else is unknown."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def parse_reply_fit(result: dict) -> ReplyFit:
    """Read the model's proposed reply features, keeping only well-typed values.

    An unknown post_kind or a malformed flag becomes None, which the skip
    rules treat as "research incomplete" rather than guessing.
    """
    kind = result.get("post_kind")
    kind = kind.strip().lower() if isinstance(kind, str) else None
    span = result.get("brief_problem_span")
    return ReplyFit(
        post_kind=kind if kind in POST_KINDS else None,
        brief_problem_span=span if isinstance(span, str) and span.strip() else None,
        in_others_thread=_strict_bool(result.get("in_others_thread")),
        is_vendor_post=_strict_bool(result.get("is_vendor_post")),
    )


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
        """Research a post: propose reply features and pick founder material.

        Returns features only. The research_post activity checks them and sets
        confidence and any skip reason in code (backend/gate/reply_fit.py).
        """
        thread_hint = ""
        if post.in_reply_to_user_id:
            thread_hint = "\nThis post is a reply to another post."

        prompt = f"""Analyze this X post. Decide whether the founder of the product below should reply to it.

Post: {post.text}
Author: @{post.author.username} ({post.author.followers_count} followers){thread_hint}
Topic: {topic}

Product Brief:
{product_brief}

Return a JSON object with these keys:
- post_kind: exactly one of {", ".join(POST_KINDS)}.
    question: the poster asks for help, advice or a recommendation.
    problem: the poster describes a problem they have now.
    experience: the poster shares what happened to them or what they learned.
    opinion: the poster states a view or a take.
    announcement: the poster announces a launch, release or feature.
    promo: the poster promotes a product, service, course or event.
    roundup: a list or digest of links, tools or updates.
    news: the poster reports news about someone else.
- brief_problem_span: copy, character for character, the part of the post that shows a
  problem the product brief solves. Use null if no part of the post shows such a problem.
  Do not paraphrase, shorten or fix the text.
- in_others_thread: true if the post is a reply inside a conversation started by another account.
- is_vendor_post: true if the poster promotes their own product or company, competing or not.
- matched_topic: which topic this relates to.
- why_it_fits: one short sentence quoting the post. If it does not fit, say why.
- founder_materials: list of {{source, passage, relevance}} that a reply could use. Copy each
  passage from the brief character for character; a passage that is not in the brief is
  discarded. Use an empty list if nothing in the brief applies.

Most posts are not reply opportunities. Do not stretch a post to fit the brief."""

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a research assistant. Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            # Without JSON mode, models wrap long answers in ```json fences,
            # which json.loads rejects.
            response_format={"type": "json_object"},
        )
        
        result = json.loads(response.choices[0].message.content)
        if not isinstance(result, dict):
            # Valid JSON that is not an object: no features, so the gate
            # records "research incomplete" instead of the activity failing.
            result = {}
        
        raw_materials = result.get("founder_materials")
        if not isinstance(raw_materials, list):
            raw_materials = []
        founder_materials = []
        for i, m in enumerate(raw_materials):
            if not isinstance(m, dict) or not isinstance(m.get("passage"), str):
                continue
            founder_materials.append(FounderMaterial(
                source=str(m.get("source", "product_brief")),
                passage=m["passage"],
                relevance=str(m.get("relevance", "")),
                passage_id=f"passage_{i}",
            ))
        
        # Confidence is not computed here: the activity does it in code from
        # these proposed features (ADR-0006). Any number the model types, such
        # as a legacy topic_match_strength, is ignored.
        return Evidence(
            post_id=post.id,
            matched_topic=str(result.get("matched_topic") or topic),
            why_it_fits=str(result.get("why_it_fits") or ""),
            confidence_score=0.0,
            confidence_inputs={},
            founder_materials=founder_materials,
            reply_fit=parse_reply_fit(result),
        )
    
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
        materials_text = format_materials_for_prompt(evidence.founder_materials)
        available_phrases = format_phrases_for_prompt()

        steer_text = f"\n\nAdditional instruction: {steer}" if steer else ""

        prompt = f"""Draft a reply to this post as structured segments.

CRITICAL RULES - QA R13:
- Claims are QUOTED, not paraphrased. You reference a passage sentence by its number.
- For claims: output ONLY {{"kind": "claim", "source_passage_id": "passage_X", "sentence_index": N}}
  where N is one of the sentence numbers listed under that passage below. Use no other number.
- For non-claims: output {{"kind": "non_claim", "phrase_id": "phrase.id"}} - select from the fixed list below
- The code will insert the exact passage sentence for claims and the phrase text for non-claims.
- You do NOT write ANY free text. Claims are sentence numbers, non-claims are phrase IDs.

Available phrases (phrase_id: text):
{available_phrases}

Composition rules:
- At most 3 segments in total: one claim, one closer (an offer or a question), and
  optionally ONE lead-in, which is either an opener or a thanks phrase, never both.
- Order: lead-in, then the claim, then the closer.
- No repeated phrase_id, never an opener alone.
- Include exactly one claim: the sentence that speaks most directly to this post.
- Choose each phrase for this post; do not default to the first phrase in a category.
  For the lead-in, use "Thanks for sharing." when the poster shares their own work or
  results, a short opener otherwise, or no lead-in at all.
- For the closer, prefer a question when the poster describes a problem, shares their
  setup, or asks something open-ended. Use an offer only when the poster asks for help,
  a recommendation, or details.

Why this post fits: {evidence.why_it_fits}

Founder Materials (selectable sentences, numbered):
{materials_text}
{steer_text}

Return a JSON object with one key:
- segments: list of objects (2-3 segments, exactly 1 claim):
  - For claims: {{"kind": "claim", "source_passage_id": "passage_X", "sentence_index": N}}
  - For non-claims: {{"kind": "non_claim", "phrase_id": "exact.phrase.id"}}

Example (shape only; pick phrases and the sentence for this post):
{{"segments": [
  {{"kind": "non_claim", "phrase_id": "opener.hey"}},
  {{"kind": "claim", "source_passage_id": "passage_0", "sentence_index": 1}},
  {{"kind": "non_claim", "phrase_id": "q.current_tool"}}
]}}

Be conversational and honest. Never invent features. Reference founder material by sentence number only.
Max 280 chars total after text insertion."""

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant. Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            # JSON mode returns a bare object; see research() above.
            response_format={"type": "json_object"},
        )
        
        result = json.loads(response.choices[0].message.content)
        
        from backend.models import Segment
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
