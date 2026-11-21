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
    
    Per ADR-0006 and NB2: This is code, not model attestation.
    Checks:
    1. Each quoted span appears in the cited passage
    2. Every number/percent/superlative/guarantee word is backed
    """
    activity.logger.info(
        f"Running claim gate on {len(input.claims)} claim(s), "
        f"{len(input.founder_materials)} materials (key: {input.idempotency_key})"
    )
    
    if not input.claims:
        activity.logger.info("No claims to check, gate passes")
        return ClaimGateResult(
            passed=True,
            claims=[],
            failed_claims=[],
            reasoning="No product claims in draft",
        )
    
    materials_by_id = {}
    for i, material in enumerate(input.founder_materials):
        passage_id = material.passage_id or f"passage_{i}"
        materials_by_id[passage_id] = material
    
    activity.logger.info(f"Checking {len(input.claims)} claims against {len(materials_by_id)} passages")
    
    failed_claims = []
    
    for claim in input.claims:
        passed, reason = verify_claim(claim, materials_by_id)
        
        if not passed:
            failed_claims.append((claim, reason))
            activity.logger.warning(
                f"Claim failed: '{claim.text[:50]}...' - {reason}"
            )
        else:
            activity.logger.info(f"Claim passed: '{claim.text[:50]}...'")
    
    gate_passed = len(failed_claims) == 0
    
    if gate_passed:
        reasoning = f"All {len(input.claims)} claim(s) verified against founder materials"
    else:
        reasoning = f"{len(failed_claims)} of {len(input.claims)} claim(s) failed verification"
    
    activity.logger.info(f"Claim gate: {'PASS' if gate_passed else 'FAIL'} - {reasoning}")
    
    return ClaimGateResult(
        passed=gate_passed,
        claims=input.claims,
        failed_claims=failed_claims,
        reasoning=reasoning,
    )
