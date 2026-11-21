"""
Claim gate activity - deterministic claim verification.

Per ADR-0006 and B7: Every product claim must match a passage in founder material.
This must be code, not model self-attestation.
"""

from dataclasses import dataclass
from typing import List, Dict
from temporalio import activity
import re

from backend.models import FounderMaterial, ClaimGateResult


@dataclass
class ClaimGateInput:
    """Input for claim_gate activity."""
    
    draft_text: str
    founder_materials: List[FounderMaterial]
    idempotency_key: str


def extract_claims(text: str) -> List[str]:
    """
    Extract product claims from draft text.
    
    Simple heuristic: sentences that mention "our", "we", "product", etc.
    """
    claims = []
    
    # Split into sentences
    sentences = re.split(r'[.!?]+', text)
    
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        
        # Check if it's a product claim
        lower = sentence.lower()
        if any(keyword in lower for keyword in ['our', 'we', 'product', 'system', 'tool', 'service', 'platform']):
            claims.append(sentence)
    
    return claims


def check_claim_support(claim: str, materials: List[FounderMaterial]) -> tuple[bool, str]:
    """
    Check if a claim is supported by founder materials.
    
    Returns (supported, supporting_passage).
    """
    claim_lower = claim.lower()
    
    # Extract key terms from claim
    claim_terms = set(re.findall(r'\b\w+\b', claim_lower))
    
    # Remove common words
    stop_words = {'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
                  'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
                  'should', 'may', 'might', 'can', 'our', 'we', 'us', 'this', 'that',
                  'it', 'its', 'for', 'with', 'from', 'to', 'of', 'in', 'on', 'at'}
    claim_terms = claim_terms - stop_words
    
    best_match = None
    best_score = 0
    
    for material in materials:
        passage_lower = material.passage.lower()
        passage_terms = set(re.findall(r'\b\w+\b', passage_lower))
        
        # Calculate overlap
        overlap = len(claim_terms & passage_terms)
        
        if overlap > best_score:
            best_score = overlap
            best_match = f"{material.source}: {material.passage}"
    
    # Require at least 2 matching terms for support
    if best_score >= 2:
        return True, best_match
    
    return False, ""


@activity.defn(name="claim_gate")
async def claim_gate(input: ClaimGateInput) -> ClaimGateResult:
    """
    Deterministic claim gate - verify all product claims.
    
    Per ADR-0006: This is code, not model attestation.
    Checks each claim against founder materials.
    """
    activity.logger.info(
        f"Running claim gate on draft ({len(input.draft_text)} chars, "
        f"{len(input.founder_materials)} materials) (key: {input.idempotency_key})"
    )
    
    # Extract claims
    claims = extract_claims(input.draft_text)
    
    if not claims:
        # No product claims, gate passes
        activity.logger.info("No product claims found, gate passes")
        return ClaimGateResult(
            passed=True,
            claims_checked=[],
            unsupported_claims=[],
            supported_claims={},
            reasoning="No product claims detected in draft",
        )
    
    activity.logger.info(f"Found {len(claims)} potential claims to check")
    
    # Check each claim
    supported_claims = {}
    unsupported_claims = []
    
    for claim in claims:
        supported, passage = check_claim_support(claim, input.founder_materials)
        
        if supported:
            supported_claims[claim] = passage
            activity.logger.info(f"Claim supported: {claim[:50]}...")
        else:
            unsupported_claims.append(claim)
            activity.logger.warning(f"Claim unsupported: {claim[:50]}...")
    
    passed = len(unsupported_claims) == 0
    
    reasoning = (
        "All claims supported by founder materials" if passed
        else f"{len(unsupported_claims)} unsupported claim(s)"
    )
    
    activity.logger.info(f"Claim gate: {'PASS' if passed else 'FAIL'} - {reasoning}")
    
    return ClaimGateResult(
        passed=passed,
        claims_checked=claims,
        unsupported_claims=unsupported_claims,
        supported_claims=supported_claims,
        reasoning=reasoning,
    )
