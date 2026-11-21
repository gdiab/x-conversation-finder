"""Tests for claim_gate activity."""

import pytest
from backend.activities.claim_gate_activity import (
    ClaimGateInput,
    claim_gate,
    extract_claims,
    check_claim_support,
)
from backend.models import FounderMaterial


@pytest.mark.asyncio
async def test_extract_claims():
    """Test claim extraction from draft text."""
    draft = "Our product uses advanced AI. We have 10,000 users. The CEO is John Smith."
    
    claims = extract_claims(draft)
    
    assert len(claims) >= 2  # Should extract factual claims
    assert any("AI" in c or "advanced" in c for c in claims)


@pytest.mark.asyncio
async def test_check_claim_support_supported():
    """Test checking a supported claim."""
    claim = "Our product uses AI for content generation"
    materials = [
        FounderMaterial(
            source="product_brief",
            passage="We build an AI-powered content generation platform.",
            relevance="high",
        )
    ]
    
    is_supported = check_claim_support(claim, materials)
    
    assert is_supported is True


@pytest.mark.asyncio
async def test_check_claim_support_unsupported():
    """Test checking an unsupported claim."""
    claim = "We have 50,000 paying customers"
    materials = [
        FounderMaterial(
            source="product_brief",
            passage="We are a new startup building AI tools.",
            relevance="high",
        )
    ]
    
    is_supported = check_claim_support(claim, materials)
    
    assert is_supported is False


@pytest.mark.asyncio
async def test_claim_gate_all_supported():
    """Test claim gate with all claims supported."""
    input_data = ClaimGateInput(
        draft_text="Your product sounds interesting. Can you tell me more about your AI features?",
        founder_materials=[
            FounderMaterial(
                source="product_brief",
                passage="We offer AI-powered content generation with GPT-4.",
                relevance="high",
            )
        ],
        idempotency_key="test_all_supported",
    )
    
    result = await claim_gate(input_data)
    
    assert result.passed is True
    assert len(result.unsupported_claims) == 0


@pytest.mark.asyncio
async def test_claim_gate_unsupported_claims():
    """Test claim gate with unsupported claims."""
    input_data = ClaimGateInput(
        draft_text="Our product has 100,000 users and we've raised $50M in funding.",
        founder_materials=[
            FounderMaterial(
                source="product_brief",
                passage="We are a new startup building AI tools.",
                relevance="low",
            )
        ],
        idempotency_key="test_unsupported",
    )
    
    result = await claim_gate(input_data)
    
    assert result.passed is False
    assert len(result.unsupported_claims) > 0


@pytest.mark.asyncio
async def test_claim_gate_no_factual_claims():
    """Test claim gate with no factual claims (question only)."""
    input_data = ClaimGateInput(
        draft_text="What problem are you trying to solve?",
        founder_materials=[
            FounderMaterial(
                source="product_brief",
                passage="We help creators with AI tools.",
                relevance="high",
            )
        ],
        idempotency_key="test_no_claims",
    )
    
    result = await claim_gate(input_data)
    
    assert result.passed is True  # No claims to fail
    assert len(result.claims_checked) == 0
