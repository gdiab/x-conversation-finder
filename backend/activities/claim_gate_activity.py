"""
Claim gate activity - deterministic claim verification.

Per Architect decision 2026-11-21:
Claims are QUOTED, not paraphrased. A claim segment references a founder passage
and a sentence index; code inserts that sentence verbatim (trailing punctuation aside).
The model never authors claim wording. All other text must match the closed set of
non-claim forms (questions, greetings or thanks, explicit first-person opinion or
experience that does not mention the product with a comparative, number, capability
or competitor), with the keyword backstop overriding any non_claim tag.
"""

from dataclasses import dataclass
from typing import List, Dict, Set, Optional
from temporalio import activity
import re
import unicodedata

from backend.models import FounderMaterial, Segment, ClaimGateResult


@dataclass
class ClaimGateInput:
    """Input for claim_gate activity."""
    
    segments: List[Segment]
    founder_materials: List[FounderMaterial]
    idempotency_key: str


def split_into_sentences(text: str) -> List[str]:
    """
    Split text into sentences.
    
    S-5: Fixed to not break on decimals like 99.99%.
    Splits on sentence-ending punctuation (. ! ?) followed by space or end,
    but not on decimals (digit before and after the period).
    Returns non-empty sentences.
    """
    # Use negative lookbehind and lookahead to avoid splitting decimals
    # Don't split if period is between digits
    sentences = re.split(r'(?<!\d)[.!?]+(?!\d)\s*', text)
    return [s.strip() for s in sentences if s.strip()]


def normalize_text(text: str) -> str:
    """
    Normalize text for comparison: NFKC, casefold, collapse whitespace.
    """
    # NFKC normalization (compatibility decomposition + canonical composition)
    text = unicodedata.normalize('NFKC', text)
    
    # Normalize quote characters to standard ASCII quotes
    text = text.replace('"', '"').replace('"', '"').replace(''', "'").replace(''', "'")
    
    # Casefold (more aggressive than lowercase for international text)
    text = text.casefold()
    
    # Collapse whitespace
    return ' '.join(text.split())


def apply_confusables_skeleton(text: str) -> str:
    """
    B5a: Apply confusables skeleton fold to detect homoglyphs.
    
    Replaces common Cyrillic/Greek lookalikes with ASCII equivalents.
    This catches bypasses like "Ѕupports ЅЅO" (Cyrillic S).
    """
    # Common Cyrillic confusables (this is a basic set; expand as needed)
    confusables = {
        'А': 'A', 'В': 'B', 'Е': 'E', 'К': 'K', 'М': 'M', 'Н': 'H',
        'О': 'O', 'Р': 'P', 'С': 'C', 'Т': 'T', 'Х': 'X', 'Ѕ': 'S',
        'а': 'a', 'е': 'e', 'о': 'o', 'р': 'p', 'с': 'c', 'х': 'x',
    }
    
    result = []
    for char in text:
        result.append(confusables.get(char, char))
    
    return ''.join(result)


# B5a: Closed-set non_claim patterns (WHOLE sentence)
# A sentence is non_claim ONLY if, after stripping leading greeting/thanks,
# the REMAINDER is:
# - Empty
# - A question with no product+comparative/number/capability/competitor
# - An explicit first-person opinion that doesn't launder a claim

GREETING_THANKS_PATTERNS = [
    r'^(?:thanks|thank you|hi|hello|hey|greetings|appreciate|happy to|glad to|pleased to)[,!.\s]*',
]

OPINION_PATTERNS = [
    r'^(?:I think|I believe|I feel|I love|I like|I appreciate|I agree|I disagree|In my (?:opinion|experience|view)|I\'ve found|I found|I\'d say|personally)\b',
]

# B5a: Product words (expanded to include ours/us/our/we)
# Be explicit: "we/our/ours/us" and "it's/it is" (but not "this" alone)
PRODUCT_INDICATORS = [
    r'\b(?:we|our|ours|us)\b',
    r'\b(?:my|the)\s+(?:product|tool|platform|service|app|system)\b',
    r'\bit\'?s?\b',  # "it's" or "it" or "its"
]

# Claim indicators that combine with product mention to make it a claim
CLAIM_INDICATORS = [
    # Comparatives and superlatives
    r'\b(?:better|worse|faster|slower|more|less|best|worst|fastest|slowest|most|least|than|compared to|cheaper|quicker|way cheaper)\b',
    # Numbers and quantifications (including "10x", "twenty")
    r'\b(?:\d+\.?\d*\s*(?:%|percent|pct|times|x|ms|sec|seconds|min|minutes|hours|days|GB|MB|KB|TB|PB|requests|ops|transactions|users|integrations|k|K|M|B)?|ten|twenty|thirty|hundred|thousand)\b',
    # Capability verbs
    r'\b(?:get|gets|give|gives|support|supports|provide|provides|handle|handles|include|includes|has|have|offer|offers|integrate|integrates|deliver|delivers|enable|enables|build|builds|create|creates|manage|manages|monitor|monitors|track|tracks|analyze|analyzes|solve|solves|fix|fixes|improve|improves|optimize|optimizes|ensure|ensures|automate|automates|cut|cuts|reduce|reduces|eliminate|eliminates|beat|beats|work|works|ship|ships|come|comes|switch|switched)\b',
    # Competitor/alternative mentions
    r'\b(?:Temporal|Kubernetes|Kafka|AWS|Azure|GCP|Docker|Jenkins|CircleCI|GitHub Actions|Slack|Teams|Jira|Confluence)\b',
]

# Pattern backstop: these patterns always classify a segment as a claim
# (overriding any non_claim tag from the model)
BACKSTOP_PATTERNS = [
    # Any digit or unit (including word numbers like "twenty")
    r'\b(?:\d+\.?\d*\s*(?:%|percent|pct|ms|sec|seconds|min|minutes|hours|days|GB|MB|KB|TB|PB|requests|ops|transactions|users|integrations|k|K|M|B)?|ten|twenty|thirty|forty|fifty|hundred|thousand|million|billion)\b',
    # Compliance/security terms
    r'\b(?:SOC2|SSO|RBAC|HIPAA|GDPR|encrypted|encryption|compliance|compliant|certified|certification|audit|auditing|audit logs|role-based access control)\b',
    # Availability terms
    r'\b(?:24/7|uptime|availability|downtime|SLA|99\.?\d*%)\b',
    # Risky absolutes
    r'\b(?:always|never|guarantee[sd]?|best|fastest|most|only|all|every|unlimited|instant(?:ly)?|zero|exactly-once)\b',
]


def strip_leading_greeting_thanks(text: str) -> str:
    """
    B5a: Strip leading greeting/thanks clause from text.
    Returns the remainder.
    """
    for pattern in GREETING_THANKS_PATTERNS:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    return text.strip()


def is_pure_question(text: str) -> bool:
    """
    B5a: Check if text is a pure question (ends with ?) with no claim indicators.
    A trailing '?' must not make a claim-bearing sentence a question non_claim.
    """
    if not text.endswith('?'):
        return False
    
    # Check if it has product + claim indicator (makes it a claim, not a non_claim question)
    has_product = any(re.search(p, text, re.IGNORECASE) for p in PRODUCT_INDICATORS)
    if not has_product:
        return True  # No product mention, pure question
    
    # Has product - check for claim indicators
    has_claim_indicator = any(re.search(p, text, re.IGNORECASE) for p in CLAIM_INDICATORS)
    return not has_claim_indicator  # Pure question only if no claim indicators


def is_opinion_without_product_claim(text: str) -> bool:
    """
    B5a: Check if text is an explicit first-person opinion that doesn't launder a claim.
    Opinion frames that mention product with comparatives/numbers/capabilities are claims.
    """
    # Check for opinion pattern
    has_opinion = any(re.search(p, text, re.IGNORECASE) for p in OPINION_PATTERNS)
    if not has_opinion:
        return False
    
    # Check if it mentions product with claim indicator (makes it a claim)
    has_product = any(re.search(p, text, re.IGNORECASE) for p in PRODUCT_INDICATORS)
    if not has_product:
        return True  # Opinion without product mention
    
    # Has product - check for claim indicators
    has_claim_indicator = any(re.search(p, text, re.IGNORECASE) for p in CLAIM_INDICATORS)
    return not has_claim_indicator  # Opinion non_claim only if no claim indicators


def matches_backstop_pattern(text: str) -> bool:
    """
    Check if text matches any backstop pattern.
    These patterns always indicate a claim, overriding any non_claim tag.
    """
    # B5a: Apply confusables skeleton before matching
    text_skeleton = apply_confusables_skeleton(text)
    
    for pattern in BACKSTOP_PATTERNS:
        if re.search(pattern, text_skeleton, re.IGNORECASE):
            return True
    return False


def is_closed_set_non_claim(text: str) -> bool:
    """
    B5a: Check if text is a closed-set non_claim (WHOLE sentence).
    
    A sentence is non_claim ONLY if, after stripping a leading greeting/thanks clause,
    the REMAINDER is:
    - Empty (pure greeting/thanks like "Thanks for sharing!")
    - A pure question (ends with ?, no product+claim_indicator)
    - An explicit first-person opinion that doesn't mention product with claim indicators
    
    AND it doesn't match any backstop pattern (which overrides non_claim).
    
    The key is: "for sharing!", "for your help" etc. are considered empty remainders
    in the context of greetings (they're part of the greeting phrase).
    """
    # First check backstop (overrides everything)
    if matches_backstop_pattern(text):
        return False
    
    # Check if it's a pure greeting/thanks phrase
    # These patterns should match the WHOLE text for pure greetings
    pure_greeting_patterns = [
        r'^(?:thanks|thank you)(?:\s+for\s+[\w\s]+)?[!.]*$',  # "Thanks", "Thanks for the question!"
        r'^(?:hi|hello|hey)(?:\s+there)?[!.]*$',  # "Hi!", "Hello there!"
        r'^(?:would be\s+)?(?:happy|glad|pleased)\s+to\s+(?:help|share|chat)[\w\s]*[!.]*$',  # "Happy to help!", "Would be happy to share more details if useful!"
    ]
    
    for pattern in pure_greeting_patterns:
        if re.match(pattern, text, re.IGNORECASE):
            return True
    
    # Strip leading greeting/thanks for compound sentences
    remainder = strip_leading_greeting_thanks(text)
    
    # Empty remainder after stripping greeting
    if not remainder:
        return True
    
    # Pure question (no product+claim_indicator)
    if is_pure_question(remainder):
        return True
    
    # Opinion without product claim
    if is_opinion_without_product_claim(remainder):
        return True
    
    return False


def insert_claim_sentence(
    segment: Segment,
    materials_by_id: Dict[str, FounderMaterial]
) -> tuple[bool, str, str]:
    """
    Insert the claim sentence from the founder passage.
    
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
    
    draft_text = ' '.join(seg.text for seg in processed_segments if seg.text)
    activity.logger.info(f"Constructed draft: {len(draft_text)} chars from {len(processed_segments)} segments")
    
    # Re-split into sentences and verify mapping
    sentences = split_into_sentences(draft_text)
    activity.logger.info(f"Split into {len(sentences)} sentences")
    
    for sentence in sentences:
        norm_sentence = normalize_text(sentence)
        
        # Find which segment contains this sentence
        found = False
        for seg in processed_segments:
            if seg.text:
                norm_seg = normalize_text(seg.text)
                # Sentence maps to segment if either contains the other
                if norm_sentence in norm_seg or norm_seg in norm_sentence:
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
    )
