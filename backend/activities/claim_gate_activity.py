"""
Claim gate activity - deterministic claim verification.

Per ADR-0006 and NB2: Every product claim must match a passage in founder material.
This is code, not model self-attestation.

The gate checks:
1. Each quoted span appears in the cited passage
2. Every number/percent/superlative/guarantee word is backed by a cited passage
"""

from dataclasses import dataclass
from typing import List, Dict
from temporalio import activity
import re

from backend.models import FounderMaterial, Claim, ClaimGateResult


@dataclass
class ClaimGateInput:
    """Input for claim_gate activity."""
    
    draft_text: str
    claims: List[Claim]
    founder_materials: List[FounderMaterial]
    idempotency_key: str


RISKY_PATTERNS = [
    r'\b\d+\.?\d*%',
    r'\b\d+\.?\d*\s*(?:percent|pct)',
    r'\balways\b',
    r'\bnever\b',
    r'\bguarantee[sd]?\b',
    r'\b100%\b',
    r'\b99\.?\d*%',
    r'\bbest\b',
    r'\bfastest\b',
    r'\bmost\b',
    r'\bonly\b',
    r'\ball\b',
    r'\bevery\b',
    r'\bunlimited\b',
    r'\binstant(?:ly)?\b',
    r'\bzero\b',
]


def find_risky_words(text: str) -> List[str]:
    """
    Find numbers, percents, superlatives, and guarantee words.
    
    Returns list of risky word spans found.
    """
    risky_words = []
    for pattern in RISKY_PATTERNS:
        matches = re.finditer(pattern, text, re.IGNORECASE)
        for match in matches:
            risky_words.append(match.group())
    return risky_words


def check_span_in_passage(span: str, passage: str) -> bool:
    """
    Check if a quoted span appears in the cited passage.
    
    Case-insensitive substring match.
    """
    return span.lower() in passage.lower()


def check_risky_word_backed(risky_word: str, cited_passages: List[str]) -> bool:
    """
    Check if a risky word is backed by at least one cited passage.
    
    The word must appear in at least one of the cited passages.
    """
    risky_lower = risky_word.lower()
    for passage in cited_passages:
        if risky_lower in passage.lower():
            return True
    return False


def verify_claim(
    claim: Claim, 
    materials_by_id: Dict[str, FounderMaterial]
) -> tuple[bool, str]:
    """
    Verify a claim against founder materials.
    
    Returns (passed, failure_reason).
    """
    cited_passages = []
    for passage_id in claim.cited_passage_ids:
        if passage_id not in materials_by_id:
            return False, f"Cited passage {passage_id} not found in founder materials"
        cited_passages.append(materials_by_id[passage_id].passage)
    
    if not cited_passages:
        return False, "No passages cited"
    
    for span in claim.quoted_spans:
        found = False
        for passage in cited_passages:
            if check_span_in_passage(span, passage):
                found = True
                break
        if not found:
            return False, f"Quoted span '{span}' not found in any cited passage"
    
    risky_words = find_risky_words(claim.text)
    for risky_word in risky_words:
        if not check_risky_word_backed(risky_word, cited_passages):
            return False, f"Risky word '{risky_word}' not backed by cited passages"
    
    return True, ""


@activity.defn(name="claim_gate")
async def claim_gate(input: ClaimGateInput) -> ClaimGateResult:
    """
    Deterministic claim gate - verify all product claims.
    
    Per ADR-0006, NB2, and R3-B1: This is code, not model attestation.
    Scans draft.text itself and checks:
    1. Every risky word in draft_text is covered by a declared, verified claim
    2. Each declared claim's text appears in draft_text
    3. Each quoted span appears in the cited passage
    4. Every risky word in claim.text is backed by cited passages
    """
    activity.logger.info(
        f"Running claim gate on draft ({len(input.draft_text)} chars), "
        f"{len(input.claims)} claim(s), "
        f"{len(input.founder_materials)} materials (key: {input.idempotency_key})"
    )
    
    # Extract all risky words from the draft text itself
    draft_risky_words = find_risky_words(input.draft_text)
    activity.logger.info(f"Found {len(draft_risky_words)} risky words in draft: {draft_risky_words}")
    
    # Build materials lookup
    materials_by_id = {}
    for i, material in enumerate(input.founder_materials):
        passage_id = material.passage_id or f"passage_{i}"
        materials_by_id[passage_id] = material
    
    # If no claims declared
    if not input.claims:
        if draft_risky_words:
            activity.logger.warning(
                f"No claims declared but draft contains {len(draft_risky_words)} risky words: {draft_risky_words}"
            )
            return ClaimGateResult(
                passed=False,
                claims=[],
                failed_claims=[],
                reasoning=f"Draft contains risky words but no claims declared: {', '.join(draft_risky_words[:3])}",
            )
        else:
            activity.logger.info("No claims and no risky words, gate passes")
            return ClaimGateResult(
                passed=True,
                claims=[],
                failed_claims=[],
                reasoning="No risky words in draft",
            )
    
    activity.logger.info(f"Verifying {len(input.claims)} claims against {len(materials_by_id)} passages")
    
    failed_claims = []
    verified_claim_texts = []
    
    # Verify each declared claim
    for claim in input.claims:
        # Check that claim text appears in draft
        if claim.text not in input.draft_text:
            failed_claims.append((claim, f"Claim text not found in draft: '{claim.text}'"))
            activity.logger.warning(f"Claim text not in draft: '{claim.text[:50]}...'")
            continue
        
        # Verify claim against founder materials
        passed, reason = verify_claim(claim, materials_by_id)
        
        if not passed:
            failed_claims.append((claim, reason))
            activity.logger.warning(
                f"Claim failed verification: '{claim.text[:50]}...' - {reason}"
            )
        else:
            verified_claim_texts.append(claim.text)
            activity.logger.info(f"Claim passed: '{claim.text[:50]}...'")
    
    # Check that every risky word in draft is covered by a verified claim
    uncovered_risky_words = []
    for risky_word in draft_risky_words:
        covered = False
        for claim_text in verified_claim_texts:
            if risky_word.lower() in claim_text.lower():
                covered = True
                break
        if not covered:
            uncovered_risky_words.append(risky_word)
    
    if uncovered_risky_words:
        activity.logger.warning(
            f"Draft contains {len(uncovered_risky_words)} uncovered risky words: {uncovered_risky_words}"
        )
        return ClaimGateResult(
            passed=False,
            claims=input.claims,
            failed_claims=failed_claims,
            reasoning=f"Draft contains uncovered risky words: {', '.join(uncovered_risky_words[:3])}",
        )
    
    gate_passed = len(failed_claims) == 0
    
    if gate_passed:
        reasoning = f"All {len(input.claims)} claim(s) verified and all {len(draft_risky_words)} risky words covered"
    else:
        reasoning = f"{len(failed_claims)} of {len(input.claims)} claim(s) failed verification"
    
    activity.logger.info(f"Claim gate: {'PASS' if gate_passed else 'FAIL'} - {reasoning}")
    
    return ClaimGateResult(
        passed=gate_passed,
        claims=input.claims,
        failed_claims=failed_claims,
        reasoning=reasoning,
    )
