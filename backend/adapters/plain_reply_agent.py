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
        Draft a reply with structured claims.
        
        The LLM outputs claims with cited passage IDs and quoted spans.
        The actual gate verification is done by claim_gate activity.
        """
        materials_text = "\n\n".join(
            f"[{m.passage_id}] Source: {m.source}\n{m.passage}"
            for m in evidence.founder_materials
        )
        
        steer_text = f"\n\nAdditional instruction: {steer}" if steer else ""
        
        prompt = f"""Draft a reply to this post. Every product claim must be supported by the founder materials below.

Why this post fits: {evidence.why_it_fits}

Founder Materials:
{materials_text}
{steer_text}

Return JSON with:
- text: the draft reply (280 chars max)
- claims: list of objects with:
  - text: the product claim being made
  - cited_passage_ids: list of passage IDs (e.g., ["passage_0", "passage_1"])
  - quoted_spans: list of exact quoted phrases from those passages that support the claim

Example claim:
{{
  "text": "Our platform handles distributed systems",
  "cited_passage_ids": ["passage_0"],
  "quoted_spans": ["distributed systems architecture"]
}}

Be conversational, honest, and never invent features. Every claim must cite specific passages and quote supporting spans."""

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant. Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
        )
        
        result = json.loads(response.choices[0].message.content)
        
        claims = [
            Claim(
                text=c["text"],
                cited_passage_ids=c.get("cited_passage_ids", []),
                quoted_spans=c.get("quoted_spans", []),
            )
            for c in result.get("claims", [])
        ]
        
        claim_gate = ClaimGateResult(
            passed=True,
            claims=claims,
            failed_claims=[],
            reasoning="Gate not yet run (will be verified by claim_gate activity)",
        )
        
        steers = evidence.draft.steers_applied if evidence.draft else []
        if steer:
            steers = steers + [steer]
        
        evidence.draft = Draft(
            text=result["text"],
            claim_gate=claim_gate,
            steers_applied=steers,
            token_count=len(result["text"].split()),
            model_used=self.model,
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
