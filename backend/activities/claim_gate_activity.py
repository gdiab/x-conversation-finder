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
# ends_with_abbreviation is re-exported: callers import it from this module.
from backend.gate.sentence_boundary import (
    END,
    SPLITTER_VERSION,
    ends_with_abbreviation,
    LIKELY_END,
    period_boundary,
    sentence_has_unclear_boundary,
    sentence_terminator,
)
from backend.gate.legacy_split_v1 import split_into_sentences_v1

REFERENT_REASON = "Sentence starts with an unresolved referent (it/this/that/these/they/its/such/the above)"
ABBREVIATION_REASON = "Sentence ends with an abbreviation (not selectable)"
# No "abbreviation" in this text: the review card maps that word to the
# ends-with-an-abbreviation explanation.
UNCLEAR_BOUNDARY_REASON = (
    "Sentence starts, ends or contains an unclear sentence boundary (a period after "
    "U.S., Inc., an initial or a list number, before a capitalized word), so it may be "
    "part of a longer sentence or two sentences (not selectable)"
)


@dataclass
class ClaimGateInput:
    """Input for claim_gate activity."""
    
    segments: List[Segment]
    founder_materials: List[FounderMaterial]
    idempotency_key: str


def split_into_sentences(text: str) -> List[str]:
    """
    Split text into sentences (ADR-0006 claim gate contract).

    Deterministic rule: a word ending in '!' or '?' (before any closing quotes
    or brackets) ends a sentence when another word follows. A word ending in
    '.' ends one only where
    backend.gate.sentence_boundary.period_boundary says END or LIKELY_END:
    - QA R14: company suffixes (Inc. Ltd. Co. Corp.) end a sentence when the
      next word is capitalized (LIKELY_END).
    - QA R13: prefix abbreviations (Dr. Mr. Mrs. Ms. Prof. vs. e.g. i.e.
      approx.) never end a sentence before a lowercase word or a name
      ("Dr. Smith"); an initial (J.) never ends one before a lowercase word.
    - Issue #10: etc., St., Ph.D. and dotted initialisms (U.S.A.) end a
      sentence before a sentence-starter word (LIKELY_END); numbers (2021.)
      before any capitalized word. Decimals (S-5: 99.99%) never split; a list
      marker ("1.") at the start of a sentence stays with it.
    claim_selectability_error rejects any sentence touching an unclear
    (LIKELY_*) boundary, as it rejects one that ends in an abbreviation.

    Sentence indices are positions in this split (SPLITTER_VERSION). Changing
    a split point changes the indices claim segments reference; see ADR-0006
    (2026-11-21 amendment, item 2).

    Returns non-empty sentences.
    """
    result = []
    current = []

    # Words and the whitespace between them alternate in tokens.
    tokens = re.findall(r'\S+|\s+', text)

    # Whether the current sentence already has a word (constant time per token).
    has_word = False

    for i, token in enumerate(tokens):
        current.append(token)
        if token.isspace():
            continue
        sentence_start = not has_word
        has_word = True
        terminator = sentence_terminator(token)
        if not terminator:
            continue
        next_word = tokens[i + 2] if i + 2 < len(tokens) else None
        if next_word is None:
            # End of text: the remainder is added below.
            continue
        if terminator == '.' and period_boundary(token, next_word, sentence_start=sentence_start) not in (END, LIKELY_END):
            continue
        sentence = ''.join(current).strip()
        if sentence:
            result.append(sentence)
        current = []
        has_word = False

    # Add remaining
    if current:
        sentence = ''.join(current).strip()
        if sentence:
            result.append(sentence)

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
    
    QA R13: Use explicit unicode escapes for curly quotes, strip leading dashes,
    add discourse markers (Therefore, Otherwise, Thus, Hence, Still, Yet),
    fix "Both" to only flag when followed by verb or 'of'.
    
    Per Architect decision 2026-11-21 (B):
    Quoted claim sentences must stand alone. A passage sentence that opens
    with an unresolved referent (It/This/That/These/They/Its/Such/"The above")
    is NOT selectable.
    
    Returns True if the sentence starts with an unresolved referent.
    """
    # Strip leading straight/curly quotes, brackets, and punctuation
    normalized = text.strip()
    # QA R13: Use explicit unicode escapes for curly quotes
    # \u201c = " (left double quote), \u201d = " (right double quote)
    # \u2018 = ' (left single quote), \u2019 = ' (right single quote)
    # \u00ab = « (left guillemet), \u00bb = » (right guillemet)
    # Also strip leading dash, bullet, en dash (\u2013), em dash (\u2014)
    normalized = re.sub(r'^["\'\[\(\{<\u201c\u201d\u2018\u2019\u00ab\u00bb\-\u2022\u2013\u2014]+', '', normalized)
    normalized = normalized.strip().lower()
    
    # Skip leading conjunctions and discourse markers (with optional comma)
    # QA R13: Add Therefore, Otherwise, Thus, Hence, Still, Yet
    # Pattern: word + optional comma + space
    discourse_markers = [
        r'^and\s*,?\s+',
        r'^also\s*,?\s+',
        r'^but\s*,?\s+',
        r'^so\s*,?\s+',
        r'^however\s*,?\s+',
        r'^plus\s*,?\s+',
        r'^then\s*,?\s+',
        r'^additionally\s*,?\s+',
        r'^moreover\s*,?\s+',
        r'^therefore\s*,?\s+',
        r'^otherwise\s*,?\s+',
        r'^thus\s*,?\s+',
        r'^hence\s*,?\s+',
        r'^still\s*,?\s+',
        r'^yet\s*,?\s+',
    ]
    for marker in discourse_markers:
        normalized = re.sub(marker, '', normalized, count=1)
    
    # Unresolved referent patterns at the start of a sentence
    # Note: "It's", "That's", "They're" and "Its" remain flagged
    # QA R13: "Both" is special - only flag when followed by verb or 'of'
    unresolved_patterns = [
        r'^it\b',
        r'^it\'s\b',
        r'^its\b',
        r'^this\b',
        r'^that\b',
        r'^that\'s\b',
        r'^these\b',
        r'^those\b',
        r'^they\b',
        r'^they\'re\b',
        r'^them\b',
        r'^their\b',
        r'^such\b',
        r'^the above\b',
        r'^the former\b',
        r'^the latter\b',
        r'^which\b',
        r'^which means\b',
        r'^here\b',
    ]
    
    for pattern in unresolved_patterns:
        if re.match(pattern, normalized):
            return True
    
    # QA R13: Special handling for "Both"
    # Only flag when followed by: are/is/do/does/have/has/were/was/of
    if re.match(r'^both\s+(are|is|do|does|have|has|were|was|of)\b', normalized):
        return True
    
    return False


def claim_selectability_error(sentences: List[str], index: int) -> str:
    """Why sentences[index] can never be quoted as a claim; empty if it can.

    `sentences` is split_into_sentences(passage): the unclear-boundary check
    needs the neighbouring sentences. The same checks on every path: model
    drafts (insert_claim_sentence), the founder-edit re-gate's passage match,
    and the numbered list in the drafting prompt.
    """
    sentence = sentences[index]
    if has_unresolved_referent(sentence):
        return REFERENT_REASON
    if ends_with_abbreviation(sentence):
        return ABBREVIATION_REASON
    if sentence_has_unclear_boundary(sentences, index):
        return UNCLEAR_BOUNDARY_REASON
    return ""


def _splitter_version_error(segment: Segment, passage: str, sentence: str) -> str:
    """Refuse an index numbered under a different sentence split (issue #10).

    A segment drafted before splitter versions existed (None) is accepted only
    if the frozen pre-#10 split gives its index the same text. Otherwise the
    gate would quote a different founder sentence from the one drafted.
    """
    if segment.splitter_version == SPLITTER_VERSION:
        return ""
    if segment.splitter_version is None:
        legacy = split_into_sentences_v1(passage)
        idx = segment.sentence_index
        if 0 <= idx < len(legacy) and normalize_text(legacy[idx]) == normalize_text(sentence):
            return ""
    return (
        f"sentence_index was numbered under sentence splitter version "
        f"{segment.splitter_version or 1}, and the current version {SPLITTER_VERSION} "
        f"numbers this passage differently; redraft"
    )


def insert_claim_sentence(
    segment: Segment,
    materials_by_id: Dict[str, FounderMaterial]
) -> tuple[bool, str, str]:
    """
    Insert the claim sentence from the founder passage.
    
    Per Architect decision 2026-11-21 (B):
    Quoted claim sentences must stand alone. Sentences starting with
    unresolved referents are not selectable.
    
    QA R13: Sentences ending in known abbreviations are not selectable.
    Issue #10: nor are sentences with an unclear boundary inside, and the
    index must count in the current split (_splitter_version_error).
    
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
    
    version_error = _splitter_version_error(segment, passage, sentence)
    if version_error:
        return False, "", version_error
    
    # (B) referent, QA R13 abbreviation ending, issue #10 unclear boundary
    error = claim_selectability_error(sentences, segment.sentence_index)
    if error:
        return False, "", error
    
    # S1 (QA R14): Trim trailing punctuation for flow, but preserve '?' for questions
    # A question "Is it fast?" should be inserted as "Is it fast?", not "Is it fast."
    # A terminator inside closing quotes ('"We ship today."') is kept verbatim.
    if sentence.rstrip().endswith('?'):
        # Keep the question mark
        sentence = sentence.rstrip()
    else:
        # Trim trailing '.!' for flow
        sentence = sentence.rstrip('.!')
    
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
    phrase_id: Optional[str] = None  # QA R13: Fixed phrase ID for non_claims


def _verdict_reusable(
    seg: Segment, passage_sentences: Dict[str, tuple], passage_splits: Dict[str, List[str]]
) -> bool:
    """May the edit re-gate keep this gated segment's verdict (issue #10)?

    A claim gated under another sentence split may be a fragment of a founder
    sentence: the pre-#10 split made "We sell in the U.S.A" out of "We sell in
    the U.S.A. market today." Its verdict is kept only if its text is still a
    whole, selectable sentence in the current split; otherwise the edited
    sentence is checked like any other.
    """
    if seg.kind != "claim" or seg.splitter_version == SPLITTER_VERSION:
        return True
    if not seg.text:
        return False
    match = passage_sentences.get(normalize_text(seg.text.rstrip('.!?')))
    if match is None:
        return False
    passage_id, index, _ = match
    return not claim_selectability_error(passage_splits[passage_id], index)


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
    passage_splits = {}  # issue #10: selectability needs neighbouring sentences
    for passage_id, material in materials_by_id.items():
        sentences = split_into_sentences(material.passage)
        passage_splits[passage_id] = sentences
        for i, sent in enumerate(sentences):
            # Normalize for comparison (B4: NFKC+casefold+whitespace)
            norm_sent = normalize_text(sent.rstrip('.!?'))
            # S2 (QA R14): Store original sentence for abbreviation and question checks
            passage_sentences[norm_sent] = (passage_id, i, sent)
    
    # Split edited text into sentences
    sentences = split_into_sentences(input.edited_text)
    activity.logger.info(f"Split edited text into {len(sentences)} sentences")
    
    # QA R15: Apply same 1-3 segment cap as model path
    if len(sentences) < 1:
        # Empty text - return single failure result
        return [SentenceGateResult(
            sentence="[edited text]",
            passed=False,
            kind="claim",
            reason="Edited text must have at least 1 sentence",
            kept_segment=False
        )]
    
    if len(sentences) > 3:
        # Too many segments - mark all as failed with specific reason
        return [SentenceGateResult(
            sentence=sent,
            passed=False,
            kind="claim",
            reason=f"Edited text must have at most 3 sentences (got {len(sentences)})",
            kept_segment=False
        ) for sent in sentences]
    
    results = []
    
    for sentence in sentences:
        # QA R13: Track sentence type (question vs statement) for reuse
        # Don't fold '?' together with '.' and '!'. A question matches only a question.
        is_question = sentence.rstrip().endswith('?')
        
        # QA R12: Strip trailing punctuation before normalizing for comparison
        # Segment texts are stored without trailing punctuation (from insert_claim_sentence)
        norm_sentence = normalize_text(sentence.rstrip('.!?'))
        
        # B4: Check if this sentence exactly matches a previously gated segment (exact after normalization)
        # QA R13: Also check sentence type - questions match only questions, statements match statements
        matched_seg = None
        for seg in input.gated_segments:
            if not _verdict_reusable(seg, passage_sentences, passage_splits):
                continue
            if seg.text:
                # Check if the segment is a question
                seg_is_question = seg.text.rstrip().endswith('?')
                
                # Only reuse if sentence types match
                if is_question != seg_is_question:
                    continue
                
                norm_seg = normalize_text(seg.text.rstrip('.!?'))
                if norm_sentence == norm_seg:
                    matched_seg = seg
                    break
        
        if matched_seg:
            # Keep the segment's verdict (S2: preserve phrase_id for composition validation)
            results.append(SentenceGateResult(
                sentence=sentence,
                passed=True,
                kind=matched_seg.kind,
                reason="",
                kept_segment=True,
                source_passage_id=matched_seg.source_passage_id,
                sentence_index=matched_seg.sentence_index,
                phrase_id=matched_seg.phrase_id
            ))
            activity.logger.info(f"Sentence '{sentence[:50]}...' kept gated segment verdict ({matched_seg.kind})")
            continue
        
        # Check if it's a verbatim passage sentence
        # S2 (QA R14): Apply same checks as model path on passage-match branch
        if norm_sentence in passage_sentences:
            passage_id, sent_idx, sent_text = passage_sentences[norm_sentence]
            
            # S2: Check terminal punctuation type (question vs statement)
            # A question matches only a question. A passage question keeps its '?'.
            passage_is_question = sent_text.rstrip().endswith('?')
            if is_question != passage_is_question:
                results.append(SentenceGateResult(
                    sentence=sentence,
                    passed=False,
                    kind="claim",
                    reason="Terminal punctuation type mismatch (question vs statement)",
                    kept_segment=False
                ))
                activity.logger.warning(f"Sentence '{sentence[:50]}...' terminal punctuation mismatch")
                continue
            
            # S2: Apply the model path's selectability rules (referent,
            # abbreviation ending, issue #10 unclear boundary)
            error = claim_selectability_error(passage_splits[passage_id], sent_idx)
            if error:
                results.append(SentenceGateResult(
                    sentence=sentence,
                    passed=False,
                    kind="claim",
                    reason=error,
                    kept_segment=False
                ))
                activity.logger.warning(f"Sentence '{sentence[:50]}...' not selectable: {error}")
                continue
            
            # S2: Passed all checks
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
        
        # QA R13: Check if it's a fixed phrase (exact match only, no frames)
        # S2 (QA R14): Also check terminal punctuation type matches
        from backend.gate.non_claim_set import find_phrase_by_text, get_phrase_text
        phrase = find_phrase_by_text(sentence)
        if phrase:
            # S2 (QA R14): Check if punctuation type matches
            phrase_text = get_phrase_text(phrase.id)
            phrase_is_question = phrase_text and phrase_text.rstrip().endswith('?')
            
            if is_question == phrase_is_question:
                # Punctuation types match
                results.append(SentenceGateResult(
                    sentence=sentence,
                    passed=True,
                    kind="non_claim",
                    reason="",
                    kept_segment=False,
                    phrase_id=phrase.id
                ))
                activity.logger.info(f"Sentence '{sentence[:50]}...' matched phrase {phrase.id}")
                continue
            else:
                # Punctuation types don't match
                activity.logger.warning(f"Sentence '{sentence[:50]}...' matched phrase text but wrong punctuation type")
                # Fall through to failed
        
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
    
    # QA R15: Apply at-most-1-claim rule (same as model path)
    # QA R16: This check is defense in depth and duplicates composition rule 2.1.0-rule-2.
    if all(r.passed for r in results):
        claim_count = sum(1 for r in results if r.kind == "claim")
        if claim_count > 1:
            # Mark all results as failed with specific reason
            for r in results:
                r.passed = False
                r.reason = f"Edited text must have at most 1 claim (got {claim_count})"
            activity.logger.warning(f"Edited text has {claim_count} claims (max 1)")
            return results
    
    # S2 (QA R14): Apply composition rules to edited text
    # Build segments from results to check composition
    if all(r.passed for r in results):
        segments_dict = []
        for r in results:
            seg_dict = {"kind": r.kind}
            if r.kind == "non_claim":
                seg_dict["phrase_id"] = r.phrase_id
            elif r.kind == "claim":
                seg_dict["source_passage_id"] = r.source_passage_id
            segments_dict.append(seg_dict)
        
        from backend.gate.non_claim_set import validate_draft_composition
        composition_violation = validate_draft_composition(segments_dict)
        if composition_violation:
            # Mark all results as failed due to composition violation
            for r in results:
                r.passed = False
                r.reason = f"Composition rule violation: {composition_violation.reason}"
            activity.logger.warning(f"Edited text composition violation: {composition_violation.reason}")
    
    return results


@activity.defn(name="claim_gate")
async def claim_gate(input: ClaimGateInput) -> ClaimGateResult:
    """
    Segment-based claim gate (Architect decision 3, 2026-11-21).
    
    1. Validate draft shape: 1-3 segments, at most 1 claim, at least 1 segment
    2. For each claim segment: insert the exact sentence from the passage
    3. For each non_claim segment: look up phrase_id and insert text
    4. Join segments into draft text
    5. Re-split into sentences and verify mapping
    6. Return per-sentence failures
    """
    activity.logger.info(
        f"Running claim gate: "
        f"{len(input.segments)} segments, "
        f"{len(input.founder_materials)} materials (key: {input.idempotency_key})"
    )
    
    # QA R13: Validate draft shape - 1-3 segments, at most 1 claim, at least 1 segment
    if len(input.segments) < 1:
        return ClaimGateResult(
            passed=False,
            segments=[],
            failed_sentences=[],
            reasoning="Draft must have at least 1 segment",
        )
    
    if len(input.segments) > 3:
        return ClaimGateResult(
            passed=False,
            segments=[],
            failed_sentences=[],
            reasoning=f"Draft must have at most 3 segments (got {len(input.segments)})",
        )
    
    claim_count = sum(1 for seg in input.segments if seg.kind == "claim")
    if claim_count > 1:
        return ClaimGateResult(
            passed=False,
            segments=[],
            failed_sentences=[],
            reasoning=f"Draft must have at most 1 claim segment (got {claim_count})",
        )
    
    # B1 (QA R14): Validate composition rules (v2.1.0)
    from backend.gate.non_claim_set import validate_draft_composition
    
    segments_dict = []
    for seg in input.segments:
        seg_dict = {"kind": seg.kind}
        if seg.kind == "non_claim":
            seg_dict["phrase_id"] = seg.phrase_id
        elif seg.kind == "claim":
            seg_dict["source_passage_id"] = seg.source_passage_id
        segments_dict.append(seg_dict)
    
    composition_violation = validate_draft_composition(segments_dict)
    if composition_violation:
        # QA R14 fix: Report in failed_sentences so evidence and redraft logic keep working
        return ClaimGateResult(
            passed=False,
            segments=[],
            failed_sentences=[("[draft composition]", composition_violation.reason)],
            reasoning=f"Composition rule violation: {composition_violation.reason}",
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
        # S3 (QA R14): Validate kind - only 'claim' and 'non_claim' are allowed
        if seg.kind not in ("claim", "non_claim"):
            failed_sentences.append(
                (f"[segment]", f"Invalid segment kind: '{seg.kind}' (must be 'claim' or 'non_claim')")
            )
            activity.logger.warning(f"Invalid segment kind: {seg.kind}")
            continue
        
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
                sentence_index=seg.sentence_index,
                # The index was checked against the current split
                splitter_version=SPLITTER_VERSION,
            )
            processed_segments.append(processed_seg)
            
        else:  # non_claim
            # QA R13: Non_claim segments are {phrase_id} with NO text
            # A non_claim segment that carries text should never have that text used
            if seg.text:
                failed_sentences.append(
                    (f"[non_claim segment]", "Non_claim segment must not have text (phrase_id only)")
                )
                activity.logger.warning("Non_claim segment has text field (should be phrase_id only)")
                continue
            
            # Validate phrase_id exists
            if not seg.phrase_id:
                failed_sentences.append((f"[non_claim segment]", "No phrase_id provided"))
                activity.logger.warning("Non_claim segment missing phrase_id")
                continue
            
            # Look up phrase and insert text
            from backend.gate.non_claim_set import get_phrase_text
            phrase_text = get_phrase_text(seg.phrase_id)
            if not phrase_text:
                failed_sentences.append(
                    (f"[non_claim segment]", f"Unknown phrase_id: {seg.phrase_id}")
                )
                activity.logger.warning(f"Unknown phrase_id: {seg.phrase_id}")
                continue
            
            # Create processed segment with inserted text
            processed_seg = Segment(
                kind="non_claim",
                text=phrase_text,
                phrase_id=seg.phrase_id
            )
            processed_segments.append(processed_seg)
    
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
            # (issue #10: also before closing quotes, '"We ship today."')
            if text and not sentence_terminator(text.rstrip()):
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
