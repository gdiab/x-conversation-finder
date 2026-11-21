"""
Claim gate activity - deterministic claim verification.

Per Architect decision 2026-11-21:
Claims are QUOTED, not paraphrased. A claim segment references a founder passage
and a sentence index; code inserts that sentence verbatim (trailing punctuation aside).
The model never authors claim wording. All other text must match the closed set of
non-claim forms defined in backend.gate.non_claim_set, with the keyword backstop
overriding any non_claim tag.

Quoted claim sentences must stand alone: sentences starting with unresolved
referents (It/This/That/These/They/Its/Such/"The above") are not selectable.
"""

from dataclasses import dataclass
from typing import List, Dict, Set, Optional
from temporalio import activity
import re
import unicodedata

from backend.models import FounderMaterial, Segment, ClaimGateResult
from backend.gate.non_claim_set import is_closed_set_non_claim, normalize_for_closed_set


@dataclass
class ClaimGateInput:
    """Input for claim_gate activity."""
    
    segments: List[Segment]
    founder_materials: List[FounderMaterial]
    idempotency_key: str


def split_into_sentences(text: str) -> List[str]:
    """
    Split text into sentences.
    
    Fixed to not break on:
    - Decimals like 99.99%
    - Common abbreviations: Inc., Ltd., e.g., i.e., Mr., Dr., vs., etc., Ph.D.
    
    Splits on sentence-ending punctuation (. ! ?) followed by space or end,
    but not on decimals or abbreviations.
    Returns non-empty sentences.
    """
    # Common abbreviations that should not trigger sentence splits
    # Map pattern to replacement text
    abbreviations = [
        (r'\bInc\.', 'Inc.'),
        (r'\bLtd\.', 'Ltd.'),
        (r'\bCo\.', 'Co.'),
        (r'\bCorp\.', 'Corp.'),
        (r'\be\.g\.', 'e.g.'),
        (r'\bi\.e\.', 'i.e.'),
        (r'\bvs\.', 'vs.'),
        (r'\bMr\.', 'Mr.'),
        (r'\bMrs\.', 'Mrs.'),
        (r'\bMs\.', 'Ms.'),
        (r'\bDr\.', 'Dr.'),
        (r'\bProf\.', 'Prof.'),
        (r'\betc\.', 'etc.'),
        (r'\bapprox\.', 'approx.'),
        (r'\bca\.', 'ca.'),
        (r'\bPh\.D\.', 'Ph.D.'),
    ]
    
    # Replace abbreviations with placeholders temporarily
    placeholders = {}
    for i, (pattern, replacement) in enumerate(abbreviations):
        placeholder = f'___ABBR{i}___'
        text = re.sub(pattern, placeholder, text, flags=re.IGNORECASE)
        placeholders[placeholder] = replacement
    
    # Use negative lookbehind and lookahead to avoid splitting decimals
    # Don't split if period is between digits
    sentences = re.split(r'(?<!\d)[.!?]+(?!\d)\s+', text)
    
    # Restore abbreviations in each sentence
    result = []
    for sent in sentences:
        if sent.strip():
            # Restore abbreviations by replacing placeholders back
            for placeholder, abbr_text in placeholders.items():
                sent = sent.replace(placeholder, abbr_text)
            result.append(sent.strip())
    
    return result


def normalize_text(text: str) -> str:
    """
    Normalize text for comparison: NFKC, confusables, casefold, collapse whitespace.
    Uses the same normalization as the closed-set module for consistency.
    """
    # Use the closed-set normalization (but keep trailing punctuation for this usage)
    # NFKC normalization (compatibility decomposition + canonical composition)
    text = unicodedata.normalize('NFKC', text)
    
    # Apply confusables fold
    from backend.gate.non_claim_set import _apply_confusables_fold
    text = _apply_confusables_fold(text)
    
    # Normalize quote characters to standard ASCII quotes
    text = text.replace('"', '"').replace('"', '"').replace(''', "'").replace(''', "'")
    
    # Casefold (more aggressive than lowercase for international text)
    text = text.casefold()
    
    # Collapse whitespace
    return ' '.join(text.split())


def has_unresolved_referent(text: str) -> bool:
    """
    Check if a sentence starts with an unresolved referent.
    
    Per Architect decision 2026-11-21 (B):
    Quoted claim sentences must stand alone. A passage sentence that opens
    with an unresolved referent (It/This/That/These/They/Its/Such/"The above")
    is NOT selectable.
    
    Returns True if the sentence starts with an unresolved referent.
    """
    # Normalize for checking
    normalized = text.strip().lower()
    
    # Unresolved referent patterns at the start of a sentence
    unresolved_patterns = [
        r'^it\b',
        r'^its\b',
        r'^this\b',
        r'^that\b',
        r'^these\b',
        r'^those\b',
        r'^they\b',
        r'^them\b',
        r'^their\b',
        r'^such\b',
        r'^the above\b',
    ]
    
    for pattern in unresolved_patterns:
        if re.match(pattern, normalized):
            return True
    
    return False


def insert_claim_sentence(
    segment: Segment,
    materials_by_id: Dict[str, FounderMaterial]
) -> tuple[bool, str, str]:
    """
    Insert the claim sentence from the founder passage.
    
    Per Architect decision 2026-11-21 (B):
    Quoted claim sentences must stand alone. Sentences starting with
    unresolved referents are not selectable.
    
    Returns (success, inserted_text, error_reason).
    """
    if not segment.source_passage_id:
        return False, "", "No source_passage_id provided"
    
    if segment.source_passage_id not in materials_by_id:
        return False, "", f"source_passage_id '{segment.source_passage_id}' not found"
    
    if segment.sentence_index is None:
        return False, "", "No sentence_index provided"
    
    passage = materials_by_id[segment.source_passage_id].passage
    sentences = split_into_sentences(passage)
    
    if segment.sentence_index < 0 or segment.sentence_index >= len(sentences):
        return False, "", f"sentence_index {segment.sentence_index} out of range (0-{len(sentences)-1})"
    
    sentence = sentences[segment.sentence_index]
    
    # (B) Check for unresolved referents - sentence must stand alone
    if has_unresolved_referent(sentence):
        return False, "", "Sentence starts with an unresolved referent (it/this/that/these/they/its/such/the above)"
    
    # Only normalization: trim trailing punctuation for flow
    sentence = sentence.rstrip('.!?')
    
    return True, sentence, ""


@dataclass
class EditedTextGateInput:
    """Input for verifying edited free text."""
    edited_text: str
    gated_segments: List[Segment]  # Previously gated segments
    founder_materials: List[FounderMaterial]
    idempotency_key: str


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


@activity.defn(name="verify_edited_text")
async def verify_edited_text(input: EditedTextGateInput) -> List[SentenceGateResult]:
    """
    Verify edited free text using the strict claim gate.
    
    Per founder-edit re-gate requirement:
    1. Split edited text into sentences
    2. For each sentence:
       - If it verbatim matches a previously gated segment (B4: exact match after normalization), keep verdict
       - If it's a verbatim passage sentence, pass as claim
       - If it's a closed-set non_claim, pass
       - Otherwise, fail
    3. Return per-sentence results (evidence trail)
    
    This is NOT a hard block - results are stored, approve requires post_anyway=True if any fail.
    """
    activity.logger.info(
        f"Verifying edited text: {len(input.edited_text)} chars, "
        f"{len(input.gated_segments)} gated segments (key: {input.idempotency_key})"
    )
    
    # Build materials lookup
    materials_by_id = {}
    for i, material in enumerate(input.founder_materials):
        passage_id = material.passage_id or f"passage_{i}"
        materials_by_id[passage_id] = material
    
    # Build passage sentence lookup for verbatim matching
    passage_sentences = {}
    for passage_id, material in materials_by_id.items():
        sentences = split_into_sentences(material.passage)
        for i, sent in enumerate(sentences):
            # Normalize for comparison (B4: NFKC+casefold+whitespace)
            norm_sent = normalize_text(sent.rstrip('.!?'))
            passage_sentences[norm_sent] = (passage_id, i, sent.rstrip('.!?'))
    
    # Split edited text into sentences
    sentences = split_into_sentences(input.edited_text)
    activity.logger.info(f"Split edited text into {len(sentences)} sentences")
    
    results = []
    
    for sentence in sentences:
        norm_sentence = normalize_text(sentence)
        
        # B4: Check if this sentence exactly matches a previously gated segment (exact after normalization)
        matched_seg = None
        for seg in input.gated_segments:
            if seg.text:
                norm_seg = normalize_text(seg.text)
                if norm_sentence == norm_seg:
                    matched_seg = seg
                    break
        
        if matched_seg:
            # Keep the segment's verdict
            results.append(SentenceGateResult(
                sentence=sentence,
                passed=True,
                kind=matched_seg.kind,
                reason="",
                kept_segment=True,
                source_passage_id=matched_seg.source_passage_id,
                sentence_index=matched_seg.sentence_index
            ))
            activity.logger.info(f"Sentence '{sentence[:50]}...' kept gated segment verdict ({matched_seg.kind})")
            continue
        
        # Check if it's a verbatim passage sentence
        if norm_sentence in passage_sentences:
            passage_id, sent_idx, sent_text = passage_sentences[norm_sentence]
            results.append(SentenceGateResult(
                sentence=sentence,
                passed=True,
                kind="claim",
                reason="",
                kept_segment=False,
                source_passage_id=passage_id,
                sentence_index=sent_idx
            ))
            activity.logger.info(f"Sentence '{sentence[:50]}...' matched passage sentence")
            continue
        
        # Check if it's a closed-set non_claim
        if is_closed_set_non_claim(sentence):
            results.append(SentenceGateResult(
                sentence=sentence,
                passed=True,
                kind="non_claim",
                reason="",
                kept_segment=False
            ))
            activity.logger.info(f"Sentence '{sentence[:50]}...' is closed-set non_claim")
            continue
        
        # Failed - not a passage sentence, not a gated segment, not a non_claim
        results.append(SentenceGateResult(
            sentence=sentence,
            passed=False,
            kind="claim",
            reason="Not a verbatim passage sentence, not a closed-set non_claim",
            kept_segment=False
        ))
        activity.logger.warning(f"Sentence '{sentence[:50]}...' failed gate")
    
    passed_count = sum(1 for r in results if r.passed)
    activity.logger.info(
        f"Edited text gate: {passed_count}/{len(results)} sentences passed"
    )
    
    return results


@activity.defn(name="claim_gate")
async def claim_gate(input: ClaimGateInput) -> ClaimGateResult:
    """
    Segment-based claim gate (Architect decision 2026-11-21).
    
    1. For each claim segment: insert the exact sentence from the passage
    2. For each non_claim segment: verify it passes closed-set rules (with backstop override)
    3. Join segments into draft text
    4. Re-split into sentences and verify mapping
    5. Return per-sentence failures
    """
    activity.logger.info(
        f"Running claim gate: "
        f"{len(input.segments)} segments, "
        f"{len(input.founder_materials)} materials (key: {input.idempotency_key})"
    )
    
    # Reject empty segment list
    if not input.segments:
        return ClaimGateResult(
            passed=False,
            segments=[],
            failed_sentences=[],
            reasoning="Empty segment list rejected",
        )
    
    # Build materials lookup
    materials_by_id = {}
    for i, material in enumerate(input.founder_materials):
        passage_id = material.passage_id or f"passage_{i}"
        materials_by_id[passage_id] = material
    
    # Process segments: insert claim sentences, validate non_claims
    processed_segments = []
    failed_sentences = []
    
    for seg in input.segments:
        if seg.kind == "claim":
            # Insert sentence from passage
            success, inserted_text, error = insert_claim_sentence(seg, materials_by_id)
            if not success:
                failed_sentences.append((f"[claim segment]", error))
                activity.logger.warning(f"Failed to insert claim sentence: {error}")
                continue
            
            # Create processed segment with inserted text
            processed_seg = Segment(
                kind="claim",
                text=inserted_text,
                source_passage_id=seg.source_passage_id,
                sentence_index=seg.sentence_index
            )
            processed_segments.append(processed_seg)
            
        else:  # non_claim
            # Validate non_claim text exists
            if not seg.text or not seg.text.strip():
                failed_sentences.append((f"[non_claim segment]", "Empty non_claim text"))
                activity.logger.warning("Empty non_claim segment text")
                continue
            
            # Validate it passes closed-set rules (with backstop override)
            if not is_closed_set_non_claim(seg.text):
                failed_sentences.append(
                    (seg.text, "Does not match closed-set non_claim patterns (or matches backstop)")
                )
                activity.logger.warning(f"Non_claim segment failed closed-set: '{seg.text[:50]}...'")
                continue
            
            processed_segments.append(seg)
    
    # Join processed segments to construct draft
    if not processed_segments:
        return ClaimGateResult(
            passed=False,
            segments=input.segments,
            failed_sentences=failed_sentences,
            reasoning="No valid segments after processing",
        )
    
    # Join segments, ensuring each ends with punctuation for proper sentence splitting
    draft_parts = []
    for seg in processed_segments:
        if seg.text:
            text = seg.text
            # Add period if text doesn't end with sentence-ending punctuation
            if text and not text.rstrip()[-1:] in '.!?':
                text = text + '.'
            draft_parts.append(text)
    
    draft_text = ' '.join(draft_parts)
    activity.logger.info(f"Constructed draft: {len(draft_text)} chars from {len(processed_segments)} segments")
    
    # Re-split into sentences and verify mapping
    sentences = split_into_sentences(draft_text)
    activity.logger.info(f"Split into {len(sentences)} sentences")
    
    for sentence in sentences:
        # Normalize and strip trailing punctuation for comparison
        norm_sentence = normalize_text(sentence.rstrip('.!?'))
        
        # Find which segment contains this sentence
        found = False
        for seg in processed_segments:
            if seg.text:
                # Also strip trailing punctuation from segment for comparison
                norm_seg = normalize_text(seg.text.rstrip('.!?'))
                # Exact normalized match only (not substring containment)
                if norm_sentence == norm_seg:
                    found = True
                    break
        
        if not found:
            failed_sentences.append((sentence, "Sentence does not map to any segment"))
            activity.logger.warning(f"Unmapped sentence: '{sentence[:50]}...'")
    
    # Determine overall pass/fail
    gate_passed = len(failed_sentences) == 0
    
    if gate_passed:
        reasoning = f"All {len(sentences)} sentences verified and mapped to segments"
    else:
        reasoning = f"{len(failed_sentences)} failures: {failed_sentences[0][1] if failed_sentences else 'unknown'}"
    
    activity.logger.info(f"Claim gate: {'PASS' if gate_passed else 'FAIL'} - {reasoning}")
    
    return ClaimGateResult(
        passed=gate_passed,
        segments=processed_segments,  # Return processed segments with inserted text
        failed_sentences=failed_sentences,
        reasoning=reasoning,
        draft_text=draft_text,  # Constructed draft with proper punctuation
    )
