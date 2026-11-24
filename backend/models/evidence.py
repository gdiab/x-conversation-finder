"""Evidence and claim models."""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Literal


@dataclass
class FounderMaterial:
    """Founder material passage supporting a claim."""
    
    source: str
    passage: str
    relevance: str
    passage_id: Optional[str] = None  # Unique ID for this passage


@dataclass
class Segment:
    """
    A segment of reply text with its classification.
    
    Per Architect decision 3 (2026-11-21):
    Drafts are assembled, not written. The model emits segments by reference only:
    - For claims: {kind: 'claim', source_passage_id, sentence_index} with NO text
    - For non_claims: {kind: 'non_claim', phrase_id} with NO text
    
    Code inserts text:
    - Claims: split the founder passage into sentences and insert the exact sentence
    - Non-claims: look up phrase_id in the fixed phrase list and insert that text
    
    The model authors no free text in a draft.
    
    Fields:
    - kind: 'claim' or 'non_claim' (str literal, not enum)
    - text: segment text (inserted by code, NOT provided by model)
    - source_passage_id: passage ID (for claims only)
    - sentence_index: 0-based index into passage sentences (for claims only)
    - phrase_id: fixed phrase ID (for non_claims only)
    """
    
    kind: Literal["claim", "non_claim"]
    text: Optional[str] = None
    source_passage_id: Optional[str] = None
    sentence_index: Optional[int] = None
    phrase_id: Optional[str] = None  # QA R13: Fixed phrase ID for non_claims


@dataclass
class Claim:
    """A product claim with cited evidence (legacy structure for backwards compatibility)."""
    
    text: str
    cited_passage_ids: List[str]
    quoted_spans: List[str]


@dataclass
class ClaimGateResult:
    """Result of claim gate validation."""
    
    passed: bool
    segments: List[Segment]  # New segment-based structure (processed, with text filled in)
    failed_sentences: List[tuple[str, str]]  # (sentence, reason)
    reasoning: str
    draft_text: str = ""  # Constructed draft text from joined segments (properly punctuated)
    # Legacy fields for backwards compatibility
    claims: List[Claim] = field(default_factory=list)
    failed_claims: List[tuple[Claim, str]] = field(default_factory=list)


@dataclass
class Draft:
    """Draft reply with metadata."""
    
    text: str
    segments: List[Segment]  # Structured segments from the model
    claim_gate: ClaimGateResult
    steers_applied: List[str] = field(default_factory=list)
    token_count: int = 0
    model_used: str = ""
    # S-2: Real token usage from model
    input_tokens: int = 0
    output_tokens: int = 0


# --- Issue #4: "should we reply?" features (self-contained block) ---
@dataclass
class ReplyFit:
    """Should-we-reply features for a post (issue #4, ADR-0006).

    The first four fields are what the research model proposed, stored as
    received after type checks; code never trusts them as they are. The rest
    are set by code in backend/gate/reply_fit.py.
    """

    # Proposed by the research model
    post_kind: Optional[str] = None  # one of backend.config.confidence.POST_KINDS
    brief_problem_span: Optional[str] = None  # exact span of the post, or None
    in_others_thread: Optional[bool] = None
    is_vendor_post: Optional[bool] = None
    # Set by code
    span_check: str = "not_checked"  # ok | missing | not_in_post | too_short
    thread_source: str = "not_checked"  # metadata | model | missing
    in_others_thread_used: Optional[bool] = None  # the value the rules used
    skip_rule: Optional[str] = None  # rule id when a skip rule fired
    rule_version: str = ""  # backend.config.confidence.CONFIDENCE_RULE_VERSION
# --- end issue #4 block ---


@dataclass
class Evidence:
    """Evidence trail for a post."""
    
    post_id: str
    matched_topic: str
    why_it_fits: str
    confidence_score: float
    confidence_inputs: Dict[str, Any]
    founder_materials: List[FounderMaterial]
    draft: Optional[Draft] = None
    skip_reason: Optional[str] = None
    # Issue #4: should-we-reply features and the code's checks of them
    reply_fit: Optional[ReplyFit] = None
    
    def compute_confidence(self) -> float:
        """
        Compute confidence from evidence shape.
        Per ADR-0006: confidence is computed, never asserted by the model.

        Rule v2 (backend/config/confidence.py, dated there):
        - gates (lookup_succeeded, replies_open, has_founder_material):
          any gate at 0 makes the score 0;
        - weighted sum of kind_score, asks_question, brief_overlap,
          author_reach and direct_post;
        - multiplied by a freshness factor that can only lower the score.

        Every input is set by code (backend/gate/reply_fit.py); a missing
        input counts as 0.
        """
        from backend.config.confidence import (
            CONFIDENCE_GATES,
            CONFIDENCE_WEIGHTS,
            FRESHNESS_FLOOR,
        )

        inputs = self.confidence_inputs

        def value(key: str) -> float:
            v = inputs.get(key, 0.0)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return 0.0
            return min(1.0, max(0.0, float(v)))

        if any(value(g) <= 0.0 for g in CONFIDENCE_GATES):
            return 0.0

        score = sum(value(k) * w for k, w in CONFIDENCE_WEIGHTS.items())
        score *= FRESHNESS_FLOOR + (1.0 - FRESHNESS_FLOOR) * value("freshness")
        return round(min(1.0, max(0.0, score)), 4)
