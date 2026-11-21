"""
Tests for claim_gate activity.

Per NB2: Tests use the REAL gate with structured claims (cited passages, quoted spans).
"""

import pytest
from backend.activities.claim_gate_activity import (
    ClaimGateInput,
    claim_gate,
    find_risky_words,
    check_span_in_passage,
    check_risky_word_backed,
    verify_claim,
)
from backend.models import FounderMaterial, Claim


@pytest.mark.asyncio
async def test_find_risky_words():
    """Test risky word detection."""
    text = "We guarantee 99.99% uptime always with zero downtime. Best in class."
    risky = find_risky_words(text)
    
    assert len(risky) > 0
    assert "99.99%" in risky
    assert any("guarantee" in w.lower() for w in risky)
    assert any("always" in w.lower() for w in risky)
    assert any("zero" in w.lower() for w in risky)
    assert any("best" in w.lower() for w in risky)


@pytest.mark.asyncio
async def test_check_span_in_passage():
    """Test span matching in passage."""
    passage = "Our platform provides reliable distributed systems support."
    
    assert check_span_in_passage("reliable distributed systems", passage)
    assert check_span_in_passage("RELIABLE DISTRIBUTED", passage)
    assert not check_span_in_passage("99.99% uptime", passage)


@pytest.mark.asyncio
async def test_check_risky_word_backed():
    """Test risky word backing."""
    passages = [
        "We offer 99% reliability based on our testing.",
        "The platform handles distributed systems."
    ]
    
    assert check_risky_word_backed("99%", passages)
    assert not check_risky_word_backed("100%", passages)
    assert not check_risky_word_backed("guaranteed", passages)


@pytest.mark.asyncio
async def test_verify_claim_all_checks_pass():
    """Test claim verification when all checks pass."""
    materials = {
        "passage_0": FounderMaterial(
            source="product_brief",
            passage="Our platform provides high reliability for distributed systems.",
            relevance="high",
            passage_id="passage_0",
        )
    }
    
    claim = Claim(
        text="Our platform supports distributed systems",
        cited_passage_ids=["passage_0"],
        quoted_spans=["distributed systems"],
    )
    
    passed, reason = verify_claim(claim, materials)
    
    assert passed
    assert reason == ""


@pytest.mark.asyncio
async def test_verify_claim_missing_passage():
    """Test claim verification with missing cited passage."""
    materials = {
        "passage_0": FounderMaterial(
            source="product_brief",
            passage="Our platform is reliable.",
            relevance="high",
            passage_id="passage_0",
        )
    }
    
    claim = Claim(
        text="Our platform is great",
        cited_passage_ids=["passage_999"],
        quoted_spans=["great"],
    )
    
    passed, reason = verify_claim(claim, materials)
    
    assert not passed
    assert "not found" in reason


@pytest.mark.asyncio
async def test_verify_claim_span_not_in_passage():
    """Test claim verification when quoted span doesn't appear in passage."""
    materials = {
        "passage_0": FounderMaterial(
            source="product_brief",
            passage="Our platform is reliable.",
            relevance="high",
            passage_id="passage_0",
        )
    }
    
    claim = Claim(
        text="Our platform is the fastest",
        cited_passage_ids=["passage_0"],
        quoted_spans=["fastest"],
    )
    
    passed, reason = verify_claim(claim, materials)
    
    assert not passed
    assert "not found" in reason


@pytest.mark.asyncio
async def test_verify_claim_risky_word_not_backed():
    """Test claim verification when risky word is not backed."""
    materials = {
        "passage_0": FounderMaterial(
            source="product_brief",
            passage="Our platform is reliable.",
            relevance="high",
            passage_id="passage_0",
        )
    }
    
    claim = Claim(
        text="We guarantee 99.99% uptime",
        cited_passage_ids=["passage_0"],
        quoted_spans=["reliable"],
    )
    
    passed, reason = verify_claim(claim, materials)
    
    assert not passed
    assert "99.99%" in reason or "guarantee" in reason


@pytest.mark.asyncio
async def test_claim_gate_no_claims():
    """Test claim gate with no claims."""
    input_data = ClaimGateInput(
        claims=[],
        founder_materials=[],
        idempotency_key="test_no_claims",
    )
    
    result = await claim_gate(input_data)
    
    assert result.passed
    assert len(result.claims) == 0
    assert len(result.failed_claims) == 0


@pytest.mark.asyncio
async def test_claim_gate_all_pass():
    """Test claim gate when all claims pass verification."""
    materials = [
        FounderMaterial(
            source="product_brief",
            passage="Our platform provides high reliability for distributed systems.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    claims = [
        Claim(
            text="Our platform supports distributed systems",
            cited_passage_ids=["passage_0"],
            quoted_spans=["distributed systems"],
        )
    ]
    
    input_data = ClaimGateInput(
        claims=claims,
        founder_materials=materials,
        idempotency_key="test_all_pass",
    )
    
    result = await claim_gate(input_data)
    
    assert result.passed
    assert len(result.claims) == 1
    assert len(result.failed_claims) == 0


@pytest.mark.asyncio
async def test_claim_gate_unsupported_claim_with_fake_guarantee():
    """
    Test claim gate with unsupported claim (per NB2 requirement).
    
    This is the regenerate test that uses the REAL gate with a fake agent
    making an unsupported claim like '99.99% uptime guaranteed'.
    """
    materials = [
        FounderMaterial(
            source="product_brief",
            passage="Our platform is designed for reliability.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    unsupported_claim = Claim(
        text="We guarantee 99.99% uptime with zero downtime",
        cited_passage_ids=["passage_0"],
        quoted_spans=["reliability"],
    )
    
    input_data = ClaimGateInput(
        claims=[unsupported_claim],
        founder_materials=materials,
        idempotency_key="test_unsupported_guarantee",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert len(result.failed_claims) == 1
    
    failed_claim, reason = result.failed_claims[0]
    assert failed_claim.text == unsupported_claim.text
    assert any(
        word in reason.lower() 
        for word in ["99.99%", "guarantee", "zero"]
    )


@pytest.mark.asyncio
async def test_claim_gate_multiple_claims_some_fail():
    """Test claim gate with multiple claims where some fail."""
    materials = [
        FounderMaterial(
            source="product_brief",
            passage="Our platform provides distributed systems support.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    claims = [
        Claim(
            text="Our platform supports distributed systems",
            cited_passage_ids=["passage_0"],
            quoted_spans=["distributed systems"],
        ),
        Claim(
            text="We have the fastest performance",
            cited_passage_ids=["passage_0"],
            quoted_spans=["fastest"],
        ),
    ]
    
    input_data = ClaimGateInput(
        claims=claims,
        founder_materials=materials,
        idempotency_key="test_mixed",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert len(result.failed_claims) == 1
    assert result.failed_claims[0][0].text == "We have the fastest performance"
