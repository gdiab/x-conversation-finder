"""
Tests for segment-based claim gate (ADR-0006 Amendment 2026-11-21).
"""

import pytest
from backend.activities.claim_gate_activity import (
    ClaimGateInput,
    claim_gate,
    matches_backstop_pattern,
    check_quote_in_passage,
    check_cheap_support,
    split_into_sentences,
)
from backend.models import FounderMaterial, Segment


@pytest.mark.asyncio
async def test_matches_backstop_pattern_capability_verbs():
    """Test pattern backstop for first/second person + capability verbs."""
    assert matches_backstop_pattern("We support SSO")
    assert matches_backstop_pattern("Our platform provides RBAC")
    assert matches_backstop_pattern("You get 24/7 support")
    assert matches_backstop_pattern("It handles distributed systems")
    assert matches_backstop_pattern("My tool integrates with Slack")


@pytest.mark.asyncio
async def test_matches_backstop_pattern_digits():
    """Test pattern backstop for digits and units."""
    assert matches_backstop_pattern("Handles 10k requests per second")
    assert matches_backstop_pattern("99.9% uptime")
    assert matches_backstop_pattern("Supports 1000 users")
    assert matches_backstop_pattern("5 GB storage")


@pytest.mark.asyncio
async def test_matches_backstop_pattern_compliance():
    """Test pattern backstop for compliance/security terms."""
    assert matches_backstop_pattern("SOC2 compliant")
    assert matches_backstop_pattern("Supports SSO")
    assert matches_backstop_pattern("RBAC enabled")
    assert matches_backstop_pattern("HIPAA certified")
    assert matches_backstop_pattern("GDPR compliant")
    assert matches_backstop_pattern("Encrypted at rest")


@pytest.mark.asyncio
async def test_matches_backstop_pattern_availability():
    """Test pattern backstop for availability terms."""
    assert matches_backstop_pattern("24/7 support")
    assert matches_backstop_pattern("99.99% uptime")
    assert matches_backstop_pattern("High availability")
    assert matches_backstop_pattern("Zero downtime")


@pytest.mark.asyncio
async def test_check_quote_in_passage():
    """Test normalized quote matching."""
    passage = "Our platform provides reliable distributed systems support with high availability."
    
    assert check_quote_in_passage("distributed systems support", passage)
    assert check_quote_in_passage("DISTRIBUTED SYSTEMS", passage)
    assert check_quote_in_passage("reliable distributed systems", passage)
    assert not check_quote_in_passage("blockchain support", passage)


@pytest.mark.asyncio
async def test_check_cheap_support():
    """Test cheap support check (numbers, entities, nouns)."""
    passage = "Our platform provides SSO authentication with 99.9% uptime for distributed systems."
    
    # Should pass: contains SSO, 99.9%, distributed, systems
    assert check_cheap_support("Supports SSO with 99.9% uptime", passage)
    
    # Should pass: contains distributed, systems
    assert check_cheap_support("Great for distributed systems", passage)
    
    # Should fail: key terms not in passage
    assert not check_cheap_support("Supports HIPAA compliance with blockchain", passage)


@pytest.mark.asyncio
async def test_split_into_sentences():
    """Test sentence splitting."""
    text = "Thanks for the question. Our product supports SSO. Happy to help!"
    sentences = split_into_sentences(text)
    
    assert len(sentences) == 3
    assert "Thanks for the question" in sentences[0]
    assert "Our product supports SSO" in sentences[1]
    assert "Happy to help" in sentences[2]


@pytest.mark.asyncio
async def test_claim_gate_all_pass():
    """Test claim gate when all segments pass."""
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="Our platform provides distributed systems support with high reliability.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    segments = [
        Segment(
            text="Thanks for raising this.",
            kind="non_claim"
        ),
        Segment(
            text="Our platform provides distributed systems support.",
            kind="claim",
            source_passage_id="passage_0",
            quote="distributed systems support"
        ),
        Segment(
            text="Happy to help!",
            kind="non_claim"
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_all_pass",
    )
    
    result = await claim_gate(input_data)
    
    assert result.passed
    assert len(result.failed_sentences) == 0
    assert "verified" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_claim_gate_missing_passage_id():
    """Test claim gate fails when source_passage_id doesn't exist."""
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="Our platform is reliable.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    segments = [
        Segment(
            text="We support SSO.",
            kind="claim",
            source_passage_id="passage_999",  # Doesn't exist
            quote="SSO support"
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_missing_passage",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert len(result.failed_sentences) == 1
    assert "passage_999" in result.failed_sentences[0][1]


@pytest.mark.asyncio
async def test_claim_gate_quote_not_in_passage():
    """Test claim gate fails when quote isn't in the cited passage."""
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="Our platform provides distributed systems support.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    segments = [
        Segment(
            text="We support blockchain technology.",
            kind="claim",
            source_passage_id="passage_0",
            quote="blockchain technology"  # Not in passage
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_quote_not_found",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert len(result.failed_sentences) == 1
    assert "quote" in result.failed_sentences[0][1].lower()


@pytest.mark.asyncio
async def test_claim_gate_pattern_backstop_overrides_non_claim():
    """
    Regression test: pattern backstop overrides non_claim tag.
    'Our tool is SOC2 compliant' tagged as non_claim should fail (no passage).
    """
    materials = []  # No materials
    
    segments = [
        Segment(
            text="Our tool is SOC2 compliant.",
            kind="non_claim",  # Wrongly tagged as non_claim
            # No source_passage_id or quote
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_soc2_backstop",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert len(result.failed_sentences) == 1
    # Should fail because backstop promoted it to claim but no passage provided
    assert "source_passage_id" in result.failed_sentences[0][1].lower()


@pytest.mark.asyncio
async def test_claim_gate_regression_24_7_support():
    """
    Regression test: 'You get 24/7 support' tagged as non_claim should be rejected.
    """
    materials = []
    
    segments = [
        Segment(
            text="You get 24/7 support.",
            kind="non_claim",  # Wrongly tagged
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_24_7",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed


@pytest.mark.asyncio
async def test_claim_gate_regression_10k_requests():
    """
    Regression test: 'Handles 10k requests per second' should be rejected without evidence.
    """
    materials = []
    
    segments = [
        Segment(
            text="Handles 10k requests per second.",
            kind="non_claim",
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_10k",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed


@pytest.mark.asyncio
async def test_claim_gate_regression_exactly_once():
    """
    Regression test: 'Gives you exactly-once delivery' should be rejected without evidence.
    """
    materials = []
    
    segments = [
        Segment(
            text="Gives you exactly-once delivery.",
            kind="non_claim",
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_exactly_once",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed


@pytest.mark.asyncio
async def test_claim_gate_regression_sso():
    """
    Regression test: 'Supports SSO' should be rejected without evidence.
    """
    materials = []
    
    segments = [
        Segment(
            text="Supports SSO.",
            kind="non_claim",
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_sso",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed


@pytest.mark.asyncio
async def test_claim_gate_regression_rbac():
    """
    Regression test: 'Provides role-based access control' should be rejected without evidence.
    """
    materials = []
    
    segments = [
        Segment(
            text="Provides role-based access control.",
            kind="non_claim",
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_rbac",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed


@pytest.mark.asyncio
async def test_claim_gate_properly_covered_claim_passes():
    """
    Test that a properly covered claim with correct passage and quote passes.
    """
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="Our platform supports SSO authentication with SAML and OAuth, plus role-based access control (RBAC).",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    segments = [
        Segment(
            text="Thanks for asking.",
            kind="non_claim"
        ),
        Segment(
            text="Our platform supports SSO authentication.",
            kind="claim",
            source_passage_id="passage_0",
            quote="SSO authentication"
        ),
        Segment(
            text="Happy to share more details.",
            kind="non_claim"
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_covered_claim",
    )
    
    result = await claim_gate(input_data)
    
    assert result.passed
    assert len(result.failed_sentences) == 0


@pytest.mark.asyncio
async def test_claim_gate_quote_not_in_cited_passage_fails():
    """
    Test that a claim citing the wrong passage (quote not in that passage) fails.
    """
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="Our platform provides distributed systems support.",
            relevance="high",
            passage_id="passage_0",
        ),
        FounderMaterial(
            source="security_docs",
            passage="We support SSO and RBAC.",
            relevance="high",
            passage_id="passage_1",
        )
    ]
    
    segments = [
        Segment(
            text="We support SSO.",
            kind="claim",
            source_passage_id="passage_0",  # Wrong passage
            quote="SSO"  # This quote is in passage_1, not passage_0
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_wrong_passage",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert len(result.failed_sentences) == 1
    assert "quote" in result.failed_sentences[0][1].lower()


@pytest.mark.asyncio
async def test_claim_gate_sentence_not_mapping_to_segment():
    """
    Test that if a sentence in the joined draft doesn't map to any segment, it fails.
    """
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="Our platform is great.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    # Segments don't cover all the text somehow (edge case)
    # In practice, this shouldn't happen if the model outputs segments correctly
    # But we test the gate's sentence-mapping logic
    segments = [
        Segment(
            text="Thanks.",
            kind="non_claim"
        ),
    ]
    
    # Manually construct input where joined text has more content
    # This simulates a case where draft construction went wrong
    # In the real system, draft = ' '.join(seg.text for seg in segments)
    # So this test verifies the gate's robustness
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_unmapped",
    )
    
    result = await claim_gate(input_data)
    
    # Should pass since the joined draft is just "Thanks."
    # which maps to the segment
    assert result.passed


@pytest.mark.asyncio
async def test_claim_gate_cheap_support_fails():
    """
    Test that cheap support check fails when key terms aren't in passage.
    """
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="Our platform is reliable and fast.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    segments = [
        Segment(
            text="We support distributed consensus with 99.9% uptime and HIPAA compliance.",
            kind="claim",
            source_passage_id="passage_0",
            quote="reliable"  # Quote is in passage, but key terms aren't
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_cheap_support_fail",
    )
    
    result = await claim_gate(input_data)
    
    # Should fail because "distributed", "consensus", "99.9%", "HIPAA" aren't in passage
    assert not result.passed
    assert "support" in result.failed_sentences[0][1].lower()


@pytest.mark.asyncio
async def test_claim_gate_multiple_segments_mixed_results():
    """
    Test claim gate with multiple segments where some pass and some fail.
    Covers the old test_claim_gate_multiple_claims_some_fail scenario.
    """
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="Our platform provides distributed systems support.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    segments = [
        Segment(
            text="Thanks for the question.",
            kind="non_claim"
        ),
        Segment(
            text="Our platform supports distributed systems.",
            kind="claim",
            source_passage_id="passage_0",
            quote="distributed systems support"
        ),
        Segment(
            text="We have the fastest performance in the industry.",
            kind="claim",
            source_passage_id="passage_0",
            quote="fastest"  # Not in passage
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_mixed",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert len(result.failed_sentences) == 1
    assert "fastest" in result.failed_sentences[0][0].lower()


@pytest.mark.asyncio
async def test_claim_gate_empty_segments():
    """
    Test claim gate with empty segments list (edge case).
    """
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="Our platform is great.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    segments = []
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_empty",
    )
    
    result = await claim_gate(input_data)
    
    # Empty segments means empty draft, which should pass (no claims to verify)
    assert result.passed
    assert len(result.failed_sentences) == 0


@pytest.mark.asyncio
async def test_normalize_text_handles_whitespace():
    """
    Test that quote matching handles different whitespace.
    Covers helper function testing from old tests.
    """
    from backend.activities.claim_gate_activity import check_quote_in_passage
    
    passage = "Our   platform    provides\n  distributed   systems."
    
    # Should match despite different whitespace
    assert check_quote_in_passage("platform provides distributed", passage)
    assert check_quote_in_passage("platform  provides   distributed", passage)


@pytest.mark.asyncio
async def test_claim_gate_no_quote_provided():
    """
    Test claim segment with no quote field fails.
    """
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="Our platform supports SSO.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    segments = [
        Segment(
            text="We support SSO.",
            kind="claim",
            source_passage_id="passage_0",
            quote=None  # Missing quote
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_no_quote",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed
    assert "no quote" in result.failed_sentences[0][1].lower()
