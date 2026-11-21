"""
Tests for segment-based claim gate (ADR-0006 Amendment 2026-11-21).
"""

import pytest
from backend.activities.claim_gate_activity import (
    ClaimGateInput,
    claim_gate,
    matches_backstop_pattern,
    check_quote_in_passage,
    check_near_verbatim_support,
    split_into_sentences,
    is_closed_set_non_claim,
    determine_effective_kind,
)
from backend.models import FounderMaterial, Segment


@pytest.mark.asyncio
async def test_is_closed_set_non_claim():
    """Test closed-set non_claim detection."""
    # Questions
    assert is_closed_set_non_claim("Have you tried this?")
    assert is_closed_set_non_claim("What do you think?")
    
    # Greetings and thanks
    assert is_closed_set_non_claim("Thanks for your help!")
    assert is_closed_set_non_claim("Hi there!")
    
    # Explicit first-person opinion
    assert is_closed_set_non_claim("I think this is interesting.")
    assert is_closed_set_non_claim("In my experience, this works well.")
    
    # Not closed-set non_claim
    assert not is_closed_set_non_claim("We support SSO")
    assert not is_closed_set_non_claim("Our platform is fast")


@pytest.mark.asyncio
async def test_matches_backstop_pattern_digits():
    """Test pattern backstop for digits and units."""
    assert matches_backstop_pattern("10k requests per second")
    assert matches_backstop_pattern("99.9% uptime")
    assert matches_backstop_pattern("1000 users")
    assert matches_backstop_pattern("5 GB storage")


@pytest.mark.asyncio
async def test_matches_backstop_pattern_compliance():
    """Test pattern backstop for compliance/security terms."""
    assert matches_backstop_pattern("SOC2 compliant")
    assert matches_backstop_pattern("SSO authentication")
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
async def test_determine_effective_kind_rules():
    """Test effective kind determination with all rules."""
    # Rule 1: Backstop patterns → claim
    assert determine_effective_kind("Handles 10k requests.", "non_claim") == "claim"
    assert determine_effective_kind("SOC2 compliant.", "non_claim") == "claim"
    
    # Rule 2: Product mention + claim indicator → claim
    assert determine_effective_kind("We support distributed systems.", "non_claim") == "claim"
    assert determine_effective_kind("Our tool is faster than Temporal.", "non_claim") == "claim"
    assert determine_effective_kind("I think it cuts costs in half.", "non_claim") == "claim"
    
    # Rule 3: Closed-set non_claim
    assert determine_effective_kind("Have you tried this?", "claim") == "non_claim"
    assert determine_effective_kind("Thanks for sharing!", "claim") == "non_claim"
    assert determine_effective_kind("I think this is a hard problem.", "claim") == "non_claim"
    
    # Rule 4: Default → claim
    assert determine_effective_kind("This is a great approach.", "non_claim") == "claim"


@pytest.mark.asyncio
async def test_check_quote_in_passage():
    """Test normalized quote matching."""
    passage = "Our platform provides reliable distributed systems support with high availability."
    
    assert check_quote_in_passage("distributed systems support", passage)
    assert check_quote_in_passage("DISTRIBUTED SYSTEMS", passage)
    assert check_quote_in_passage("reliable distributed systems", passage)
    assert not check_quote_in_passage("blockchain support", passage)


@pytest.mark.asyncio
async def test_check_near_verbatim_support():
    """Test near-verbatim support check against quote."""
    quote = "SSO authentication with 99.9% uptime"
    
    # Should pass: all content words and numbers in segment appear in quote
    passed, _ = check_near_verbatim_support("SSO authentication with 99.9% uptime", quote)
    assert passed
    
    # Should pass: subset of quote words
    passed, _ = check_near_verbatim_support("SSO authentication", quote)
    assert passed
    
    # Should fail: content word not in quote
    passed, reason = check_near_verbatim_support("SSO authentication with RBAC", quote)
    assert not passed
    assert "rbac" in reason.lower()
    
    # Should fail: number not in quote
    passed, reason = check_near_verbatim_support("99.8% uptime", quote)
    assert not passed
    assert "99.8" in reason.lower()


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
            quote="platform provides distributed systems support"  # Must include all content words
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
            quote="platform supports SSO authentication"  # Must include all content words
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
    QA r9 item 3a: Test that if a sentence in the joined draft doesn't map to any segment, it fails.
    
    This can occur if sentence splitting creates a sentence that doesn't map cleanly to any segment.
    """
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="Our platform is great.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    # Construct segments where the sentence splitter will create a sentence
    # that doesn't map to any segment due to punctuation placement
    segments = [
        Segment(
            text="Thanks for",  # Incomplete - will be joined with next
            kind="non_claim"
        ),
        Segment(
            text="the question. We support SSO.",  # Period creates two sentences
            kind="claim",
            source_passage_id="passage_0",
            quote="support"
        ),
    ]
    
    # Joined text: "Thanks for the question. We support SSO."
    # Sentence split: ["Thanks for the question", "We support SSO"]
    # "Thanks for the question" doesn't match either segment's text
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_unmapped",
    )
    
    result = await claim_gate(input_data)
    
    # Should fail because "Thanks for the question" doesn't map to any single segment
    assert not result.passed
    assert len(result.failed_sentences) > 0


@pytest.mark.asyncio
async def test_claim_gate_near_verbatim_support_fails():
    """
    Test that near-verbatim support check fails when segment words aren't in quote.
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
            text="We support distributed consensus with 99.9% uptime.",
            kind="claim",
            source_passage_id="passage_0",
            quote="reliable"  # Quote is in passage, but doesn't contain segment's words
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_near_verbatim_fail",
    )
    
    result = await claim_gate(input_data)
    
    # Should fail because "distributed", "consensus", "99.9", "uptime" aren't in the quote
    assert not result.passed
    assert "verbatim" in result.failed_sentences[0][1].lower()


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
            text="Our platform provides distributed systems support.",
            kind="claim",
            source_passage_id="passage_0",
            quote="platform provides distributed systems support"  # Complete quote
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
    QA r9 item 3b: Empty segment list must be REJECTED.
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
    
    # QA r9 item 3b: Empty segments must be rejected
    assert not result.passed
    assert "empty segment list" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_claim_gate_empty_segment_text():
    """
    QA r9 item 3b: Empty segment text must be REJECTED.
    """
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="Our platform is great.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    segments = [
        Segment(
            text="",  # Empty text
            kind="non_claim"
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_empty_text",
    )
    
    result = await claim_gate(input_data)
    
    # QA r9 item 3b: Empty segment text must be rejected
    assert not result.passed
    assert "empty segment text" in result.reasoning.lower()


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


@pytest.mark.asyncio
async def test_claim_gate_ships_with_blocked():
    """
    QA r9 item 3c: 'ships with' pattern blocked without evidence.
    """
    materials = []
    
    segments = [
        Segment(
            text="This ships with built-in monitoring.",
            kind="non_claim",  # Wrongly tagged
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_ships_with",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed


@pytest.mark.asyncio
async def test_claim_gate_includes_blocked():
    """
    QA r9 item 3c: 'includes' pattern blocked without evidence.
    """
    materials = []
    
    segments = [
        Segment(
            text="The platform includes SSO and RBAC.",
            kind="non_claim",  # Wrongly tagged
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_includes",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed


@pytest.mark.asyncio
async def test_claim_gate_works_with_integration():
    """
    QA r9 item 3c: Integration claims like 'works with Kafka' blocked without evidence.
    """
    materials = []
    
    segments = [
        Segment(
            text="This works with Kafka and other streaming platforms.",
            kind="non_claim",
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_works_with",
    )
    
    result = await claim_gate(input_data)
    
    assert not result.passed


# New regression tests per ADR-0006 Amendment 2026-11-21


@pytest.mark.asyncio
async def test_regression_eliminate_on_call_pain():
    """Regression: 'We eliminate on-call pain.' must be REJECTED without quote."""
    materials = []
    
    segments = [
        Segment(
            text="We eliminate on-call pain.",
            kind="claim",
            # No source_passage_id or quote
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_eliminate",
    )
    
    result = await claim_gate(input_data)
    assert not result.passed
    assert "source_passage_id" in result.failed_sentences[0][1].lower()


@pytest.mark.asyncio
async def test_regression_ship_twice_as_fast():
    """Regression: 'Teams ship twice as fast with it.' must be REJECTED without quote."""
    materials = []
    
    segments = [
        Segment(
            text="Teams ship twice as fast with it.",
            kind="claim",
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_twice_fast",
    )
    
    result = await claim_gate(input_data)
    assert not result.passed


@pytest.mark.asyncio
async def test_regression_used_by_thousands():
    """Regression: 'Used by thousands of engineers.' must be REJECTED without quote."""
    materials = []
    
    segments = [
        Segment(
            text="Used by thousands of engineers.",
            kind="claim",
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_thousands",
    )
    
    result = await claim_gate(input_data)
    assert not result.passed


@pytest.mark.asyncio
async def test_regression_cuts_infra_bill_in_half():
    """Regression: 'Cuts your infra bill in half.' must be REJECTED without quote."""
    materials = []
    
    segments = [
        Segment(
            text="Cuts your infra bill in half.",
            kind="claim",
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_cuts_bill",
    )
    
    result = await claim_gate(input_data)
    assert not result.passed


@pytest.mark.asyncio
async def test_regression_beats_temporal_on_latency():
    """Regression: 'Beats Temporal on latency.' must be REJECTED without quote."""
    materials = []
    
    segments = [
        Segment(
            text="Beats Temporal on latency.",
            kind="claim",
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_beats_temporal",
    )
    
    result = await claim_gate(input_data)
    assert not result.passed


@pytest.mark.asyncio
async def test_regression_works_on_kubernetes():
    """Regression: 'Works on Kubernetes.' must be REJECTED without quote."""
    materials = []
    
    segments = [
        Segment(
            text="Works on Kubernetes.",
            kind="claim",
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_kubernetes",
    )
    
    result = await claim_gate(input_data)
    assert not result.passed


@pytest.mark.asyncio
async def test_regression_battle_tested():
    """Regression: 'Battle-tested in production.' must be REJECTED without quote."""
    materials = []
    
    segments = [
        Segment(
            text="Battle-tested in production.",
            kind="claim",
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_battle_tested",
    )
    
    result = await claim_gate(input_data)
    assert not result.passed


@pytest.mark.asyncio
async def test_regression_free_tier():
    """Regression: 'Ships with a free tier.' must be REJECTED without quote."""
    materials = []
    
    segments = [
        Segment(
            text="Ships with a free tier.",
            kind="non_claim",  # Wrongly tagged, should be caught by backstop
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_free_tier",
    )
    
    result = await claim_gate(input_data)
    assert not result.passed


@pytest.mark.asyncio
async def test_regression_opinion_with_number():
    """Regression: 'I think it's 2x faster.' must be REJECTED (opinion launders claim)."""
    materials = []
    
    segments = [
        Segment(
            text="I think it's 2x faster.",
            kind="non_claim",  # Tagged as opinion but contains product + number
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_opinion_number",
    )
    
    result = await claim_gate(input_data)
    assert not result.passed


@pytest.mark.asyncio
async def test_regression_experience_with_number():
    """Regression: 'In my experience it cuts costs in half.' must be REJECTED."""
    materials = []
    
    segments = [
        Segment(
            text="In my experience it cuts costs in half.",
            kind="non_claim",  # Tagged as opinion but contains product + claim
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_experience_claim",
    )
    
    result = await claim_gate(input_data)
    assert not result.passed


@pytest.mark.asyncio
async def test_regression_quote_carried_invention_number():
    """Regression: Real quote + '100 times faster' must be REJECTED."""
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="Our platform is fast and reliable.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    segments = [
        Segment(
            text="Our platform is 100 times faster.",
            kind="claim",
            source_passage_id="passage_0",
            quote="fast"  # Quote is in passage but doesn't support "100 times"
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_quote_invention_number",
    )
    
    result = await claim_gate(input_data)
    assert not result.passed
    # Should fail near-verbatim check (missing "100", "times")


@pytest.mark.asyncio
async def test_regression_quote_carried_invention_certification():
    """Regression: Real quote + 'is SOC2 certified' must be REJECTED."""
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="Our platform prioritizes security.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    segments = [
        Segment(
            text="Our platform is SOC2 certified.",
            kind="claim",
            source_passage_id="passage_0",
            quote="security"  # Quote is in passage but doesn't support "SOC2 certified"
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_quote_invention_soc2",
    )
    
    result = await claim_gate(input_data)
    assert not result.passed


@pytest.mark.asyncio
async def test_regression_two_sentences_second_uncovered():
    """Regression: One segment with two sentences, second is uncovered → REJECTED."""
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
            text="Our platform is reliable. It handles 10k requests per second.",
            kind="claim",
            source_passage_id="passage_0",
            quote="reliable"  # Only covers first sentence
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_two_sentences",
    )
    
    result = await claim_gate(input_data)
    assert not result.passed
    # Second sentence should fail to map or fail coverage


@pytest.mark.asyncio
async def test_regression_negation_mismatch_pos_to_neg():
    """Regression: 'Our product is not open source' quoting 'It is open source' → REJECTED."""
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="It is open source and community-driven.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    segments = [
        Segment(
            text="Our product is not open source.",
            kind="claim",
            source_passage_id="passage_0",
            quote="It is open source"  # Negation mismatch
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_negation_mismatch_1",
    )
    
    result = await claim_gate(input_data)
    assert not result.passed
    assert "negation" in result.failed_sentences[0][1].lower()


@pytest.mark.asyncio
async def test_regression_negation_mismatch_neg_to_pos():
    """Regression: 'Our product is open source' quoting 'It is not open source' → REJECTED."""
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="It is not open source but has a permissive license.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    segments = [
        Segment(
            text="Our product is open source.",
            kind="claim",
            source_passage_id="passage_0",
            quote="is not open source"  # Negation mismatch (opposite direction)
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_negation_mismatch_2",
    )
    
    result = await claim_gate(input_data)
    assert not result.passed
    assert "negation" in result.failed_sentences[0][1].lower()


@pytest.mark.asyncio
async def test_pass_question():
    """Must PASS: a question."""
    materials = []
    
    segments = [
        Segment(
            text="Have you tried debugging it?",
            kind="non_claim",
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_question",
    )
    
    result = await claim_gate(input_data)
    assert result.passed


@pytest.mark.asyncio
async def test_pass_thanks():
    """Must PASS: a thanks."""
    materials = []
    
    segments = [
        Segment(
            text="Thanks for sharing!",
            kind="non_claim",
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_thanks",
    )
    
    result = await claim_gate(input_data)
    assert result.passed


@pytest.mark.asyncio
async def test_pass_opinion_without_product():
    """Must PASS: 'I think this is a hard problem.'"""
    materials = []
    
    segments = [
        Segment(
            text="I think this is a hard problem.",
            kind="non_claim",
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_opinion_hard_problem",
    )
    
    result = await claim_gate(input_data)
    assert result.passed


@pytest.mark.asyncio
async def test_pass_claim_with_near_verbatim_quote():
    """Must PASS: claim whose content words all sit in its quote."""
    materials = [
        FounderMaterial(
            source="product_docs",
            passage="Our platform provides distributed systems support with high reliability and fault tolerance.",
            relevance="high",
            passage_id="passage_0",
        )
    ]
    
    segments = [
        Segment(
            text="Our platform provides distributed systems support.",
            kind="claim",
            source_passage_id="passage_0",
            quote="platform provides distributed systems support"
        ),
    ]
    
    input_data = ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_near_verbatim",
    )
    
    result = await claim_gate(input_data)
    assert result.passed
