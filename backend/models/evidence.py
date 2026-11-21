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
    
    Per Architect decision 2026-11-21:
    Claims are QUOTED, not paraphrased. The reply agent returns:
    - For claims: {kind: 'claim', source_passage_id, sentence_index} with NO text
    - For non_claims: {kind: 'non_claim', text}
    
    Code splits the founder passage into sentences and inserts the exact sentence
    as the segment text (trailing punctuation trimmed for flow).
    
    Fields:
    - kind: 'claim' or 'non_claim' (str literal, not enum)
    - text: segment text (inserted by code for claims, provided for non_claims)
    - source_passage_id: passage ID (for claims)
    - sentence_index: 0-based index into passage sentences (for claims)
    """
    
    kind: Literal["claim", "non_claim"]
    text: Optional[str] = None
    source_passage_id: Optional[str] = None
    sentence_index: Optional[int] = None


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
    
    def compute_confidence(self) -> float:
        """
        Compute confidence from evidence shape.
        Per ADR-0006: confidence is computed, never asserted by the model.
        
        Features:
        - lookup_succeeded: 1.0 if we got the post
        - replies_open: 1.0 if anyone can reply
        - topic_match_strength: 0.0-1.0 from keyword/semantic match
        - freshness: 1.0 if < 24h, 0.5 if < 48h, 0.25 if < 7d
        - author_reach: log10(followers)/6.0, capped at 1.0
        - has_founder_material: 1.0 if we have supporting passages
        
        Weighted sum, threshold 0.6
        """
        inputs = self.confidence_inputs
        
        weights = {
            "lookup_succeeded": 0.15,
            "replies_open": 0.15,
            "topic_match_strength": 0.25,
            "freshness": 0.15,
            "author_reach": 0.10,
            "has_founder_material": 0.20,
        }
        
        score = sum(inputs.get(k, 0.0) * w for k, w in weights.items())
        return min(1.0, max(0.0, score))
