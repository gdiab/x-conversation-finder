"""Review workflow models."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional, Literal

from .evidence import Evidence
from .reply_plan import reply_plan_card_fields
from .written import draft_mode_card_fields, sentence_check_card_fields, written_draft_card_fields
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
    # Founder-edit re-gate fields (ADR-0006 Amendment E + QA R11 B2/C)
    edited_text_gate_results: Optional[list[SentenceGateResult]] = None
    edited_text_gate_state: Optional[str] = None  # "passed", "failed", or "not_checked"
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
                        "passage_id": m.passage_id,  # issue #9: as on the stored card
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
                            "phrase_id": s.phrase_id,
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
                                "phrase_id": s.phrase_id,
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
                    "plan_conformance_failures": list(self.evidence.draft.plan_conformance_failures),  # issue #5
                    **written_draft_card_fields(self.evidence.draft),  # issue #12
                } if self.evidence.draft else None,
                "skip_reason": self.evidence.skip_reason,
                # Issue #4: should-we-reply features and checks
                "reply_fit": asdict(self.evidence.reply_fit) if self.evidence.reply_fit else None,
                **reply_plan_card_fields(self.evidence),  # issue #5
                **draft_mode_card_fields(self.evidence),  # issue #12
            },
            "status": self.status,  # R4-NN5: status is already a str (ReviewStatusStr), not enum
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "approved_by": self.approved_by,
            "posted_url": self.posted_url,
            "reply_post_id": self.reply_post_id,
            "skip_reason": self.skip_reason,
            "followup_replies": self.followup_replies,
            # An assembled re-gate result or, in written mode, a SentenceCheck (issue #12)
            "edited_text_gate_results": [
                sentence_check_card_fields(r) for r in self.edited_text_gate_results
            ] if self.edited_text_gate_results else None,
            "edited_text_gate_state": self.edited_text_gate_state,
            "post_anyway": self.post_anyway,
            "post_anyway_failing_sentences": self.post_anyway_failing_sentences,
        }
