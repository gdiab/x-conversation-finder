"""
Claim gate activity - deterministic claim verification.

Per ADR-0006 Amendment 2026-11-21: Segments-based claim gate.
- Model returns structured segments {text, kind, source_passage_id?, quote?}
- Activity payloads use plain str Literals, not str-Enums
- Code joins segments into draft, re-splits into sentences, verifies mapping
- Coverage: source_passage_id exists, quote is substring, cheap support check
- Pattern backstop overrides non_claim tags
"""

from dataclasses import dataclass
from typing import List, Dict
from temporalio import activity
import re

from backend.models import FounderMaterial, Segment, ClaimGateResult


@dataclass
class ClaimGateInput:
    """Input for claim_gate activity."""
    
    segments: List[Segment]
    founder_materials: List[FounderMaterial]
    idempotency_key: str


# Pattern backstop: these patterns override a non_claim tag
# Per ADR-0006 Amendment: widen beyond keywords to capability verbs, digits, compliance terms
BACKSTOP_PATTERNS = [
    # First/second person + capability verbs (including "gives")
    r'\b(?:we|our|my|you|your)\s+(?:get|gets|give|gives|support|supports|provide|provides|handle|handles|include|includes|offer|offers|integrate|integrates|deliver|delivers|enable|enables|build|builds|create|creates|manage|manages|monitor|monitors|track|tracks|analyze|analyzes|solve|solves|fix|fixes|improve|improves|optimize|optimizes|ensure|ensures|automate|automates)\b',
    # "it" or "this" + capability verbs
    r'\b(?:it|this)\s+(?:gets|gives|supports|provides|handles|includes|offers|integrates|delivers|enables|builds|creates|manages|monitors|tracks|analyzes|solves|fixes|improves|optimizes|ensures|automates)\b',
    # Bare capability verbs at start of sentence (common in product descriptions)
    r'^(?:Gets|Gives|Supports|Provides|Handles|Includes|Offers|Integrates|Delivers|Enables|Builds|Creates|Manages|Monitors|Tracks|Analyzes|Solves|Fixes|Improves|Optimizes|Ensures|Automates)\b',
    # "integrates with", "works with", "ships with", "comes with", "includes" patterns
    r'\b(?:integrates?|works?|ships?|comes?)\s+with\b',
    # Any digit or unit
    r'\b\d+\.?\d*\s*(?:%|percent|pct|ms|sec|seconds|min|minutes|hours|days|GB|MB|KB|TB|PB|requests|ops|transactions|users|k|K|M|B)?\b',
    # Compliance/security terms
    r'\b(?:SOC2|SSO|RBAC|HIPAA|GDPR|encrypted|encryption|compliance|compliant|certified|certification|audit|auditing|audit logs|role-based access control)\b',
    # Availability terms
    r'\b(?:24/7|uptime|availability|downtime|SLA|99\.?\d*%)\b',
    # Legacy risky patterns  
    r'\b(?:always|never|guarantee[sd]?|best|fastest|most|only|all|every|unlimited|instant(?:ly)?|zero|exactly-once)\b',
]


def matches_backstop_pattern(text: str) -> bool:
    """Check if text matches any backstop pattern."""
    for pattern in BACKSTOP_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def normalize_text(text: str) -> str:
    """
    Normalize text for comparison: NFKC, lowercase, collapse whitespace, normalize quotes.
    
    Per QA r9 item 4: NFKC normalization plus whitespace/case/quote-char normalization.
    """
    import unicodedata
    
    # NFKC normalization (compatibility decomposition + canonical composition)
    text = unicodedata.normalize('NFKC', text)
    
    # Normalize quote characters to standard ASCII quotes
    text = text.replace('"', '"').replace('"', '"').replace(''', "'").replace(''', "'")
    
    # Lowercase and collapse whitespace
    return ' '.join(text.lower().split())


def split_into_sentences(text: str) -> List[str]:
    """
    Split text into sentences.
    
    Simple split on . ! ? followed by space or end of string.
    Returns non-empty sentences.
    """
    # Split on sentence boundaries
    sentences = re.split(r'[.!?]+\s*', text)
    return [s.strip() for s in sentences if s.strip()]


def check_quote_in_passage(quote: str, passage: str) -> bool:
    """Check if quote appears as a normalized substring in passage."""
    norm_quote = normalize_text(quote)
    norm_passage = normalize_text(passage)
    return norm_quote in norm_passage


def check_cheap_support(segment_text: str, passage: str) -> bool:
    """
    Cheap support check: key numbers, entities, nouns in segment appear in passage.
    
    Extracts:
    - Numbers (e.g., "99", "24/7")
    - Capitalized terms (e.g., "SSO", "RBAC")
    - Common nouns (simple heuristic: words longer than 4 chars)
    
    Returns True if at least 50% of extracted terms appear in passage.
    """
    norm_segment = normalize_text(segment_text)
    norm_passage = normalize_text(passage)
    
    # Extract terms
    terms = []
    
    # Numbers (including decimals and percentages)
    numbers = re.findall(r'\d+\.?\d*%?', segment_text)
    terms.extend(numbers)
    
    # Capitalized terms (SSO, RBAC, etc.)
    cap_terms = re.findall(r'\b[A-Z][A-Z0-9]+\b', segment_text)
    terms.extend([t.lower() for t in cap_terms])
    
    # Words longer than 4 chars (rough noun filter)
    words = re.findall(r'\b[a-z]{5,}\b', norm_segment)
    terms.extend(words)
    
    if not terms:
        # No terms to check, pass by default
        return True
    
    # Check how many terms appear in passage
    found = sum(1 for term in terms if term.lower() in norm_passage)
    support_ratio = found / len(terms)
    
    return support_ratio >= 0.5


def verify_segment_coverage(
    segment: Segment,
    materials_by_id: Dict[str, FounderMaterial]
) -> tuple[bool, str]:
    """
    Verify coverage for a claim segment.
    
    Per ADR-0006 Amendment:
    1. source_passage_id must exist in founder material
    2. quote must be a normalized substring of that passage
    3. cheap support check (key numbers, entities, nouns appear in passage)
    
    Returns (passed, failure_reason).
    """
    if segment.kind != "claim":
        # Non-claims don't need coverage
        return True, ""
    
    # Check source_passage_id exists
    if not segment.source_passage_id:
        return False, "No source_passage_id provided"
    
    if segment.source_passage_id not in materials_by_id:
        return False, f"source_passage_id '{segment.source_passage_id}' not found in founder materials"
    
    passage = materials_by_id[segment.source_passage_id].passage
    
    # Check quote is provided
    if not segment.quote:
        return False, "No quote provided for claim"
    
    # Check quote appears in passage (normalized)
    if not check_quote_in_passage(segment.quote, passage):
        return False, f"Quote '{segment.quote}' not found in cited passage"
    
    # Cheap support check
    if not check_cheap_support(segment.text, passage):
        return False, f"Insufficient support: key terms in segment not found in cited passage"
    
    return True, ""


@activity.defn(name="claim_gate")
async def claim_gate(input: ClaimGateInput) -> ClaimGateResult:
    """
    Segment-based claim gate (ADR-0006 Amendment 2026-11-21).
    
    1. Join segments into draft text
    2. Re-split into sentences
    3. Verify each sentence maps to a segment
    4. Verify coverage for claim segments
    5. Apply pattern backstop (overrides non_claim tag)
    6. Return per-sentence failures
    """
    activity.logger.info(
        f"Running segment-based claim gate: "
        f"{len(input.segments)} segments, "
        f"{len(input.founder_materials)} materials (key: {input.idempotency_key})"
    )
    
    # QA r9 item 3b: Reject empty segment list
    if not input.segments:
        return ClaimGateResult(
            passed=False,
            segments=[],
            failed_sentences=[],
            reasoning="Empty segment list rejected",
        )
    
    # QA r9 item 3b: Reject empty segment text
    for seg in input.segments:
        if not seg.text or not seg.text.strip():
            return ClaimGateResult(
                passed=False,
                segments=input.segments,
                failed_sentences=[],
                reasoning="Empty segment text rejected",
            )
    
    # Build materials lookup
    materials_by_id = {}
    for i, material in enumerate(input.founder_materials):
        passage_id = material.passage_id or f"passage_{i}"
        materials_by_id[passage_id] = material
    
    # Step 1: Join segments to construct draft
    draft_text = ' '.join(seg.text for seg in input.segments)
    activity.logger.info(f"Constructed draft: {len(draft_text)} chars")
    
    # Step 2: Re-split into sentences
    sentences = split_into_sentences(draft_text)
    activity.logger.info(f"Split into {len(sentences)} sentences")
    
    # Step 3: Verify each sentence maps to a segment
    failed_sentences = []
    
    for sentence in sentences:
        # Find which segment(s) contain this sentence
        norm_sentence = normalize_text(sentence)
        
        # Find segments that contain this sentence (or vice versa)
        matching_segments = []
        for seg in input.segments:
            norm_seg = normalize_text(seg.text)
            # Sentence maps to segment if either contains the other (fuzzy match)
            if norm_sentence in norm_seg or norm_seg in norm_sentence:
                matching_segments.append(seg)
        
        if not matching_segments:
            failed_sentences.append((sentence, "Sentence does not map to any segment"))
            activity.logger.warning(f"Unmapped sentence: '{sentence[:50]}...'")
            continue
        
        # Step 4: Check coverage for claim segments
        # Take the first matching segment (in practice, should be 1-to-1)
        segment = matching_segments[0]
        
        # Step 5: Pattern backstop - override non_claim if pattern matches
        effective_kind = segment.kind
        if segment.kind == "non_claim" and matches_backstop_pattern(segment.text):
            effective_kind = "claim"
            activity.logger.info(
                f"Pattern backstop triggered for segment: '{segment.text[:50]}...' "
                f"(tagged non_claim but matches backstop pattern)"
            )
        
        # Verify coverage if it's a claim (or backstop-promoted)
        if effective_kind == "claim":
            # Create a segment with effective kind for verification
            verify_seg = Segment(
                text=segment.text,
                kind="claim",
                source_passage_id=segment.source_passage_id,
                quote=segment.quote
            )
            passed, reason = verify_segment_coverage(verify_seg, materials_by_id)
            if not passed:
                failed_sentences.append((sentence, reason))
                activity.logger.warning(
                    f"Sentence failed coverage: '{sentence[:50]}...' - {reason}"
                )
    
    # Determine overall pass/fail
    gate_passed = len(failed_sentences) == 0
    
    if gate_passed:
        reasoning = f"All {len(sentences)} sentences verified and mapped to segments"
    else:
        reasoning = f"{len(failed_sentences)} of {len(sentences)} sentences failed: {failed_sentences[0][1] if failed_sentences else 'unknown'}"
    
    activity.logger.info(f"Claim gate: {'PASS' if gate_passed else 'FAIL'} - {reasoning}")
    
    return ClaimGateResult(
        passed=gate_passed,
        segments=input.segments,
        failed_sentences=failed_sentences,
        reasoning=reasoning,
    )
