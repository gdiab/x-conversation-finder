"""Review workflow models."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Literal

from .evidence import Evidence
from .post import Post

# R4-B1: Status as Literal for boundary crossing (temporalio enum serialization issue)
ReviewStatusStr = Literal["pending", "approved", "approved_not_confirmed", "skipped", "expired", "followup_due"]


@dataclass
class SentenceGateResult:
    """Per-sentence gate result for edited text."""
    sentence: str
    passed: bool
    kind: str  # "claim" or "non_claim"
    reason: str  # Failure reason or empty if passed
    kept_segment: bool  # True if verbatim match to gated segment
    source_passage_id: Optional[str] = None
    sentence_index: Optional[int] = None


@dataclass
class ReviewCard:
    """Complete review card shown in the UI."""
    
    workflow_id: str
    post: Post
    evidence: Evidence
    status: ReviewStatusStr  # R4-B1: Plain str at boundary, not enum
    created_at: str
    updated_at: str
    approved_by: Optional[str] = None
    posted_url: Optional[str] = None
    reply_post_id: Optional[str] = None
    skip_reason: Optional[str] = None
    client_request_id: Optional[str] = None
    followup_replies: Optional[list] = None
    # Founder-edit re-gate fields (ADR-0006 Amendment E)
    edited_text_gate_results: Optional[list[SentenceGateResult]] = None
    post_anyway: Optional[bool] = None
    post_anyway_failing_sentences: Optional[list[str]] = None
    
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
                "post_id": self.evidence.post_id,
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
                    "segments": [
                        {
                            "kind": s.kind,
                            "text": s.text,
                            "source_passage_id": s.source_passage_id,
                            "sentence_index": s.sentence_index,
                        }
                        for s in self.evidence.draft.segments
                    ],
                    "claim_gate": {
                        "passed": self.evidence.draft.claim_gate.passed,
                        "segments": [
                            {
                                "kind": s.kind,
                                "text": s.text,
                                "source_passage_id": s.source_passage_id,
                                "sentence_index": s.sentence_index,
                            }
                            for s in self.evidence.draft.claim_gate.segments
                        ],
                        "failed_sentences": [
                            {
                                "sentence": sentence,
                                "reason": reason,
                            }
                            for sentence, reason in self.evidence.draft.claim_gate.failed_sentences
                        ],
                        "reasoning": self.evidence.draft.claim_gate.reasoning,
                        # Legacy fields for backwards compatibility
                        "claims": [
                            {
                                "text": c.text,
                                "cited_passage_ids": c.cited_passage_ids,
                                "quoted_spans": c.quoted_spans,
                            }
                            for c in self.evidence.draft.claim_gate.claims
                        ],
                        "failed_claims": [
                            {
                                "claim": {
                                    "text": c.text,
                                    "cited_passage_ids": c.cited_passage_ids,
                                    "quoted_spans": c.quoted_spans,
                                },
                                "reason": reason,
                            }
                            for c, reason in self.evidence.draft.claim_gate.failed_claims
                        ],
                    },
                    "steers_applied": self.evidence.draft.steers_applied,
                    "token_count": self.evidence.draft.token_count,
                    "model_used": self.evidence.draft.model_used,
                    "input_tokens": self.evidence.draft.input_tokens,
                    "output_tokens": self.evidence.draft.output_tokens,
                } if self.evidence.draft else None,
                "skip_reason": self.evidence.skip_reason,
            },
            "status": self.status,  # R4-NN5: status is already a str (ReviewStatusStr), not enum
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "approved_by": self.approved_by,
            "posted_url": self.posted_url,
            "reply_post_id": self.reply_post_id,
            "skip_reason": self.skip_reason,
            "followup_replies": self.followup_replies,
            "edited_text_gate_results": [
                {
                    "sentence": r.sentence,
                    "passed": r.passed,
                    "kind": r.kind,
                    "reason": r.reason,
                    "kept_segment": r.kept_segment,
                    "source_passage_id": r.source_passage_id,
                    "sentence_index": r.sentence_index,
                }
                for r in self.edited_text_gate_results
            ] if self.edited_text_gate_results else None,
            "post_anyway": self.post_anyway,
            "post_anyway_failing_sentences": self.post_anyway_failing_sentences,
        }
