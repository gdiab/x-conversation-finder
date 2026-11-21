"""
Tests for claim_gate activity.

Per NB2 and R3-B1: Tests use the REAL gate that scans draft.text itself.
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
async def test_claim_gate_no_claims_no_risky_words():
    """Test claim gate with no claims and no risky words (R3-B1)."""
    draft_text = "This is interesting. The platform has good features."
    
    input_data = ClaimGateInput(
        draft_text=draft_text,
        claims=[],
        founder_materials=[],
        idempotency_key="test_no_claims",
    )
    
    result = await claim_gate(input_data)
    
    assert result.passed
    assert len(result.claims) == 0
    assert len(result.failed_claims) == 0


@pytest.mark.asyncio
async def test_claim_gate_no_claims_with_risky_words_fails():
    """Test R3-B1: '99.99% uptime guaranteed' with zero declared claims FAILS."""
    draft_text = "Automatic failover with 99.99% uptime guaranteed."
    
    input_data = ClaimGateInput(
        draft_text=draft_text,
        claims=[],
        founder_materials=[],
        idempotency_key="test_uncovered_risky",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert "risky words" in result.reasoning.lower()
    assert "99.99%" in result.reasoning or "guarantee" in result.reasoning


@pytest.mark.asyncio
async def test_claim_gate_all_pass():
    """Test claim gate when all claims pass verification and cover all risky words."""
    draft_text = "Our platform supports distributed systems."
    
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
        draft_text=draft_text,
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
    Test claim gate with unsupported claim (per NB2 requirement, R3-B1).
    
    This is the regenerate test that uses the REAL gate with a fake agent
    making an unsupported claim like '99.99% uptime guaranteed'.
    """
    draft_text = "We guarantee 99.99% uptime with zero downtime for your critical applications."
    
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
        draft_text=draft_text,
        claims=[unsupported_claim],
        founder_materials=materials,
        idempotency_key="test_unsupported_guarantee",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert len(result.failed_claims) >= 1
    
    # Should fail because risky words aren't backed by passages
    failed_claim, reason = result.failed_claims[0]
    assert any(
        word in reason.lower() 
        for word in ["99.99%", "guarantee", "zero"]
    )


@pytest.mark.asyncio
async def test_claim_gate_multiple_claims_some_fail():
    """Test claim gate with multiple claims where some fail."""
    draft_text = "Our platform supports distributed systems. We have the fastest performance."
    
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
        draft_text=draft_text,
        claims=claims,
        founder_materials=materials,
        idempotency_key="test_mixed",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert len(result.failed_claims) >= 1
    assert any("fastest" in fc[0].text for fc in result.failed_claims)


@pytest.mark.asyncio
async def test_claim_gate_claim_text_not_in_draft_fails():
    """Test R3-B1: claim text must appear in draft."""
    draft_text = "Our platform is reliable."
    
    materials = [
        FounderMaterial(
            source="product_brief",
            passage="We provide excellent distributed systems support.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    claim = Claim(
        text="We provide excellent distributed systems support",
        cited_passage_ids=["passage_0"],
        quoted_spans=["distributed systems support"],
    )
    
    input_data = ClaimGateInput(
        draft_text=draft_text,
        claims=[claim],
        founder_materials=materials,
        idempotency_key="test_claim_not_in_draft",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert any("not found in draft" in reason.lower() for _, reason in result.failed_claims)


@pytest.mark.asyncio
async def test_claim_gate_uncovered_risky_word_in_draft_fails():
    """Test R3-B1: every risky word in draft must be covered by a verified claim."""
    draft_text = "Our platform supports distributed systems with 99.99% reliability."
    
    materials = [
        FounderMaterial(
            source="product_brief",
            passage="Our platform provides distributed systems support.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    # Claim covers "distributed systems" but not "99.99%"
    claims = [
        Claim(
            text="Our platform supports distributed systems",
            cited_passage_ids=["passage_0"],
            quoted_spans=["distributed systems"],
        )
    ]
    
    input_data = ClaimGateInput(
        draft_text=draft_text,
        claims=claims,
        founder_materials=materials,
        idempotency_key="test_uncovered_risky",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert "uncovered risky words" in result.reasoning.lower()
    assert "99.99%" in result.reasoning


@pytest.mark.asyncio
async def test_capability_sentence_without_claim_blocked():
    """Test R4-SF1: capability sentence without claim is blocked."""
    draft_text = "Our tool helps teams export to Notion seamlessly."
    
    materials = [
        FounderMaterial(
            source="product_brief",
            passage="We build productivity tools for teams.",
            relevance="medium",
            passage_id="passage_0",
        )
    ]
    
    # No claims covering the capability
    claims = []
    
    input_data = ClaimGateInput(
        draft_text=draft_text,
        claims=claims,
        founder_materials=materials,
        idempotency_key="test_capability_no_claim",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert "product capability" in result.reasoning.lower()
    assert "no claims" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_capability_sentence_with_matching_claim_allowed():
    """Test R4-SF1: capability sentence with matching claim is allowed."""
    draft_text = "Our tool helps teams export to Notion seamlessly."
    
    materials = [
        FounderMaterial(
            source="product_brief",
            passage="Our tool helps teams export data to Notion and other platforms.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    # Claim covers the capability
    claims = [
        Claim(
            text="Our tool helps teams export to Notion",
            cited_passage_ids=["passage_0"],
            quoted_spans=["helps teams export"],
        )
    ]
    
    input_data = ClaimGateInput(
        draft_text=draft_text,
        claims=claims,
        founder_materials=materials,
        idempotency_key="test_capability_with_claim",
    )
    
    result = await claim_gate(input_data)
    
    assert result.passed
    assert "1 product capability" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_capability_sentence_with_pronoun_subject_blocked():
    """Test R5-S1: pronoun/implicit subject capability sentence blocked without claim."""
    draft_text = "It supports SSO and audit logs."
    
    materials = [
        FounderMaterial(
            source="product_brief",
            passage="We build security-focused software.",
            relevance="medium",
            passage_id="passage_0",
        )
    ]
    
    # No claims covering the capability
    claims = []
    
    input_data = ClaimGateInput(
        draft_text=draft_text,
        claims=claims,
        founder_materials=materials,
        idempotency_key="test_pronoun_capability_no_claim",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert "product capability" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_third_party_claim_blocked():
    """SF-5: Third-party claims like 'They offer a free tier' are blocked without evidence."""
    draft_text = "They offer a free tier for developers."
    
    materials = [
        FounderMaterial(
            source="product_brief",
            passage="We build great tools.",
            relevance="medium",
            passage_id="passage_0",
        )
    ]
    
    # No claims covering the third-party statement
    claims = []
    
    input_data = ClaimGateInput(
        draft_text=draft_text,
        claims=claims,
        founder_materials=materials,
        idempotency_key="test_third_party_free_tier",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert "product capability" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_integration_claim_blocked():
    """SF-5: Integration claims like 'This works with Kafka' are blocked without evidence."""
    draft_text = "This works with Kafka and other streaming platforms."
    
    materials = [
        FounderMaterial(
            source="product_brief",
            passage="We integrate with many systems.",
            relevance="medium",
            passage_id="passage_0",
        )
    ]
    
    # No specific claim about Kafka
    claims = []
    
    input_data = ClaimGateInput(
        draft_text=draft_text,
        claims=claims,
        founder_materials=materials,
        idempotency_key="test_works_with_kafka",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert "product capability" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_third_party_claim_allowed_with_evidence():
    """SF-5: Third-party claims are allowed when backed by founder material."""
    draft_text = "They offer a free tier for startups."
    
    materials = [
        FounderMaterial(
            source="product_website",
            passage="We offer a free tier for startups with up to 10 users.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    claims = [
        Claim(
            text="They offer a free tier for startups",
            cited_passage_ids=["passage_0"],
            quoted_spans=["free tier for startups"],
        )
    ]
    
    input_data = ClaimGateInput(
        draft_text=draft_text,
        claims=claims,
        founder_materials=materials,
        idempotency_key="test_third_party_with_evidence",
    )
    
    result = await claim_gate(input_data)
    
    assert result.passed
    assert "1 product capability" in result.reasoning.lower()
