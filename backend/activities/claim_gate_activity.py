"""
Claim gate activity - deterministic claim verification.

Per ADR-0006 Amendment 2026-11-21: Segments-based claim gate.
- Model returns structured segments {text, kind, source_passage_id?, quote?}
- Activity payloads use plain str Literals, not str-Enums
- Code joins segments into draft, re-splits into sentences, verifies mapping
- Coverage: near-verbatim support against quote, passage must exist
- Closed-set non_claim: questions, greetings/thanks, explicit first-person opinion only
- Opinion/question frames that mention product with comparatives/numbers/capabilities are claims
"""

from dataclasses import dataclass
from typing import List, Dict, Set
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


# Closed-set non_claim patterns: code decides, not the model
# A segment is non_claim ONLY if it matches one of these closed forms
NON_CLAIM_PATTERNS = [
    # Questions (simple heuristic: ends with ?)
    r'\?$',
    # Greetings and thanks (including "happy to help", "glad to help", etc.)
    r'\b(?:thanks|thank you|hi|hello|hey|greetings|appreciate|happy to|glad to|pleased to)\b',
    # Explicit first-person opinion/experience frames
    r'\b(?:I think|I believe|I feel|In my (?:opinion|experience|view)|I\'ve found|I found|I\'d say|personally)\b',
]

# Product mention patterns: if a sentence mentions the product along with these, it's a claim
# even if it looks like an opinion/question
PRODUCT_INDICATORS = [
    r'\b(?:we|our|my|it|this|the tool|the product|the platform|the service|the app|the system)\b',
]

# Claim indicators that combine with product mention to override non_claim
CLAIM_INDICATORS = [
    # Comparatives and superlatives
    r'\b(?:better|worse|faster|slower|more|less|best|worst|fastest|slowest|most|least|than|compared to)\b',
    # Numbers and quantifications
    r'\b\d+\.?\d*\s*(?:%|percent|pct|times|x|ms|sec|seconds|min|minutes|hours|days|GB|MB|KB|TB|PB|requests|ops|transactions|users|k|K|M|B)?\b',
    # Capability verbs
    r'\b(?:get|gets|give|gives|support|supports|provide|provides|handle|handles|include|includes|offer|offers|integrate|integrates|deliver|delivers|enable|enables|build|builds|create|creates|manage|manages|monitor|monitors|track|tracks|analyze|analyzes|solve|solves|fix|fixes|improve|improves|optimize|optimizes|ensure|ensures|automate|automates|cut|cuts|reduce|reduces|eliminate|eliminates|beat|beats|work|works|ship|ships|come|comes)\b',
    # Competitor/alternative mentions
    r'\b(?:Temporal|Kubernetes|AWS|Azure|GCP|Docker|Jenkins|CircleCI|GitHub Actions|Slack|Teams|Jira|Confluence)\b',
]

# Pattern backstop: these patterns override a non_claim tag (always claims, regardless of context)
BACKSTOP_PATTERNS = [
    # Any digit or unit (without opinion frame)
    r'\b\d+\.?\d*\s*(?:%|percent|pct|ms|sec|seconds|min|minutes|hours|days|GB|MB|KB|TB|PB|requests|ops|transactions|users|k|K|M|B)\b',
    # Compliance/security terms
    r'\b(?:SOC2|SSO|RBAC|HIPAA|GDPR|encrypted|encryption|compliance|compliant|certified|certification|audit|auditing|audit logs|role-based access control)\b',
    # Availability terms
    r'\b(?:24/7|uptime|availability|downtime|SLA|99\.?\d*%)\b',
    # Legacy risky patterns  
    r'\b(?:always|never|guarantee[sd]?|best|fastest|most|only|all|every|unlimited|instant(?:ly)?|zero|exactly-once)\b',
]


def is_closed_set_non_claim(text: str) -> bool:
    """
    Check if text is a closed-set non_claim (question, greeting/thanks, explicit opinion).
    Returns True only if it matches one of the closed forms.
    """
    for pattern in NON_CLAIM_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def mentions_product_with_claim_indicator(text: str) -> bool:
    """
    Check if text mentions the product along with a claim indicator.
    If yes, it's a claim even if it looks like an opinion/question.
    """
    # Check for product mention
    has_product = any(re.search(p, text, re.IGNORECASE) for p in PRODUCT_INDICATORS)
    if not has_product:
        return False
    
    # Check for claim indicator
    has_claim_indicator = any(re.search(p, text, re.IGNORECASE) for p in CLAIM_INDICATORS)
    return has_claim_indicator


def matches_backstop_pattern(text: str) -> bool:
    """
    Check if text matches any backstop pattern.
    These patterns always indicate a claim, overriding any non_claim tag.
    """
    for pattern in BACKSTOP_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def determine_effective_kind(segment_text: str, model_kind: str) -> str:
    """
    Determine the effective kind of a segment using code rules.
    
    Rules (in order):
    1. If matches backstop pattern → claim (numbers, compliance, availability, risky words)
    2. If mentions product with claim indicator → claim (overrides opinion/question frames)
    3. If closed-set non_claim pattern (and not caught by rules 1-2) → non_claim
    4. Otherwise → claim (must cite a passage)
    
    The model's tag is only a proposal; code decides.
    Per requirement B: Opinion/question frames don't launder claims.
    """
    # Rule 1: Backstop patterns always make it a claim
    if matches_backstop_pattern(segment_text):
        return "claim"
    
    # Rule 2: Product mention with claim indicator → claim
    # This MUST come before closed-set check to prevent opinion/question laundering
    if mentions_product_with_claim_indicator(segment_text):
        return "claim"
    
    # Rule 3: Closed-set non_claim patterns (only if not caught above)
    if is_closed_set_non_claim(segment_text):
        return "non_claim"
    
    # Rule 4: Default to claim (anything else must cite a passage)
    return "claim"


def normalize_text(text: str) -> str:
    """
    Normalize text for comparison: NFKC, casefold, collapse whitespace, normalize quotes.
    
    Per ADR-0006 Amendment 2026-11-21: NFKC normalization plus casefold.
    """
    # NFKC normalization (compatibility decomposition + canonical composition)
    text = unicodedata.normalize('NFKC', text)
    
    # Normalize quote characters to standard ASCII quotes
    text = text.replace('"', '"').replace('"', '"').replace(''', "'").replace(''', "'")
    
    # Casefold (more aggressive than lowercase for international text)
    text = text.casefold()
    
    # Collapse whitespace
    return ' '.join(text.split())


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


def light_stem(word: str) -> str:
    """
    Light stemming: remove common English suffixes for matching.
    Just handles basic plural/verb forms, not full Porter stemming.
    """
    # Remove trailing 's' for plurals (but not for words ending in 'ss')
    if word.endswith('s') and len(word) > 3 and not word.endswith('ss'):
        return word[:-1]
    # Remove 'ing' suffix
    if word.endswith('ing') and len(word) > 5:
        return word[:-3]
    # Remove 'ed' suffix
    if word.endswith('ed') and len(word) > 4:
        return word[:-2]
    return word


def extract_content_words(text: str) -> Set[str]:
    """
    Extract content words from text: words of 3+ chars, excluding common stopwords.
    Returns normalized and lightly stemmed forms.
    """
    # Common stopwords to exclude
    stopwords = {
        'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
        'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'were', 'be',
        'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'can',
        'could', 'will', 'would', 'should', 'may', 'might', 'must', 'shall',
        'this', 'that', 'these', 'those', 'it', 'its', 'what', 'which', 'who',
        'when', 'where', 'why', 'how', 'all', 'each', 'every', 'some', 'any',
        'you', 'your', 'we', 'our'
    }
    
    norm_text = normalize_text(text)
    words = re.findall(r'\b[a-z]{3,}\b', norm_text)
    
    # Filter stopwords and apply light stemming
    content_words = set()
    for word in words:
        if word not in stopwords:
            stemmed = light_stem(word)
            content_words.add(stemmed)
    
    return content_words


def extract_numbers(text: str) -> Set[str]:
    """Extract all numbers from text, normalized."""
    norm_text = normalize_text(text)
    # Match numbers including decimals, percentages, and common units
    numbers = re.findall(r'\d+\.?\d*', norm_text)
    return set(numbers)


def extract_negations(text: str) -> Set[str]:
    """Extract negation words from text."""
    norm_text = normalize_text(text)
    negations = {'not', 'no', 'never', 'none', 'nothing', 'nowhere', 'neither', "n't"}
    found = set()
    
    # Check for negation words
    words = set(re.findall(r'\b[a-z]+\b', norm_text))
    for neg in negations:
        if neg in words or any(w.endswith("n't") for w in words):
            found.add(neg)
    
    return found


def check_near_verbatim_support(segment_text: str, quote: str) -> tuple[bool, str]:
    """
    Near-verbatim support check against the QUOTE (not the passage).
    
    Per ADR-0006 Amendment 2026-11-21:
    - Every content word in the segment must appear in the quote (after NFKC + casefold + light stemming)
    - Every number in the segment must appear in the quote
    - Every negation in the segment must appear in the quote (and vice versa)
    
    Returns (passed, failure_reason).
    """
    segment_content = extract_content_words(segment_text)
    quote_content = extract_content_words(quote)
    
    segment_numbers = extract_numbers(segment_text)
    quote_numbers = extract_numbers(quote)
    
    segment_negations = extract_negations(segment_text)
    quote_negations = extract_negations(quote)
    
    # Check negations FIRST (must match in both directions) - this is critical
    if segment_negations != quote_negations:
        return False, f"Negation mismatch: segment has {segment_negations or 'none'}, quote has {quote_negations or 'none'}"
    
    # Check numbers
    missing_numbers = segment_numbers - quote_numbers
    if missing_numbers:
        return False, f"Numbers in segment not found in quote: {', '.join(sorted(missing_numbers))}"
    
    # Check content words
    missing_words = segment_content - quote_content
    if missing_words:
        return False, f"Content words in segment not found in quote: {', '.join(sorted(missing_words)[:5])}"
    
    return True, ""


def verify_segment_coverage(
    segment: Segment,
    materials_by_id: Dict[str, FounderMaterial]
) -> tuple[bool, str]:
    """
    Verify coverage for a claim segment.
    
    Per ADR-0006 Amendment 2026-11-21:
    1. source_passage_id must exist in founder material
    2. quote must be a normalized substring of that passage
    3. near-verbatim support: every content word, number, and negation in the segment
       must appear in the quote (after NFKC + casefold + light stemming)
    
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
    
    # Near-verbatim support check against the quote
    supported, reason = check_near_verbatim_support(segment.text, segment.quote)
    if not supported:
        return False, f"Near-verbatim support failed: {reason}"
    
    return True, ""


def verify_claim_in_passages(
    claim_text: str,
    materials: List[FounderMaterial]
) -> tuple[bool, str]:
    """
    Verify a claim sentence (without quote) appears in founder material.
    
    Per founder-edit re-gate requirement:
    A claim passes if all its content words, numbers, and negations 
    appear within a SINGLE founder passage.
    
    Returns (passed, failure_reason).
    """
    if not materials:
        return False, "No founder materials available"
    
    claim_content = extract_content_words(claim_text)
    claim_numbers = extract_numbers(claim_text)
    claim_negations = extract_negations(claim_text)
    
    # Try each passage
    for material in materials:
        passage_content = extract_content_words(material.passage)
        passage_numbers = extract_numbers(material.passage)
        passage_negations = extract_negations(material.passage)
        
        # Check if all claim elements are in this passage
        content_covered = claim_content.issubset(passage_content)
        numbers_covered = claim_numbers.issubset(passage_numbers)
        negations_match = claim_negations == passage_negations
        
        if content_covered and numbers_covered and negations_match:
            return True, ""
    
    # Failed - report what's missing
    return False, "Content words/numbers/negations not found in any single founder passage"


@dataclass
class EditedTextGateInput:
    """Input for verifying edited free text."""
    edited_text: str
    gated_segments: List[Segment]  # Previously gated segments for verbatim matching
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


@activity.defn(name="verify_edited_text")
async def verify_edited_text(input: EditedTextGateInput) -> List[SentenceGateResult]:
    """
    Verify edited free text using the strict claim gate.
    
    Per founder-edit re-gate requirement (ADR-0006 Amendment E):
    1. Split edited text into sentences
    2. For each sentence:
       - If it verbatim matches a previously gated segment, keep that verdict
       - Otherwise, classify with closed-set non_claim rules
       - Claims without quotes must have all content words/numbers/negations in a single passage
    3. Return per-sentence results (evidence trail)
    
    This is NOT a hard block - results are stored, and approve requires post_anyway=True if any fail.
    """
    activity.logger.info(
        f"Verifying edited text: {len(input.edited_text)} chars, "
        f"{len(input.gated_segments)} gated segments (key: {input.idempotency_key})"
    )
    
    # Split into sentences
    sentences = split_into_sentences(input.edited_text)
    activity.logger.info(f"Split edited text into {len(sentences)} sentences")
    
    # Build normalized segment lookup for verbatim matching
    norm_segment_map = {}
    for seg in input.gated_segments:
        norm_text = normalize_text(seg.text)
        norm_segment_map[norm_text] = seg
    
    results = []
    
    for sentence in sentences:
        norm_sentence = normalize_text(sentence)
        
        # Check if this sentence verbatim matches a gated segment
        if norm_sentence in norm_segment_map:
            matched_seg = norm_segment_map[norm_sentence]
            # Keep the segment's verdict
            results.append(SentenceGateResult(
                sentence=sentence,
                passed=True,  # Previously gated segments are trusted
                kind=matched_seg.kind,
                reason="",
                kept_segment=True
            ))
            activity.logger.info(f"Sentence '{sentence[:50]}...' kept gated segment verdict ({matched_seg.kind})")
            continue
        
        # New sentence - classify with strict rules
        effective_kind = determine_effective_kind(sentence, "claim")  # Default to claim
        
        if effective_kind == "non_claim":
            # Non-claims always pass
            results.append(SentenceGateResult(
                sentence=sentence,
                passed=True,
                kind="non_claim",
                reason="",
                kept_segment=False
            ))
            activity.logger.info(f"Sentence '{sentence[:50]}...' classified as non_claim")
        else:
            # Claim - verify coverage in founder materials
            passed, reason = verify_claim_in_passages(sentence, input.founder_materials)
            results.append(SentenceGateResult(
                sentence=sentence,
                passed=passed,
                kind="claim",
                reason=reason if not passed else "",
                kept_segment=False
            ))
            if not passed:
                activity.logger.warning(f"Sentence '{sentence[:50]}...' failed: {reason}")
            else:
                activity.logger.info(f"Sentence '{sentence[:50]}...' passed coverage check")
    
    passed_count = sum(1 for r in results if r.passed)
    activity.logger.info(
        f"Edited text gate: {passed_count}/{len(results)} sentences passed"
    )
    
    return results


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
        
        # Step 4: Determine effective kind using code rules
        # Take the first matching segment (in practice, should be 1-to-1)
        segment = matching_segments[0]
        
        # Step 5: Determine effective kind (code decides, not the model)
        effective_kind = determine_effective_kind(segment.text, segment.kind)
        
        if effective_kind != segment.kind:
            activity.logger.info(
                f"Code override: segment '{segment.text[:50]}...' "
                f"tagged as {segment.kind} but determined as {effective_kind}"
            )
        
        # Verify coverage if it's a claim
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
