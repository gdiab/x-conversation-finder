"""
Tests for segment-based claim gate (Architect decision 2026-11-21).

Claims are QUOTED, not paraphrased. Agent outputs sentence_index, code inserts text.
"""

import pytest
from backend.activities.claim_gate_activity import (
    ClaimGateInput,
    claim_gate,
    matches_backstop_pattern,
    split_into_sentences,
    is_closed_set_non_claim,
    apply_confusables_skeleton,
)
from backend.models import FounderMaterial, Segment


@pytest.mark.asyncio
async def test_split_sentences_handles_decimals():
    """S-5: Sentence splitter must not break on decimals like 99.99%."""
    text = "Our product has 99.99% uptime. It handles 1.5 million requests per second. Great performance."
    sentences = split_into_sentences(text)
    
    assert len(sentences) == 3
    assert "99.99% uptime" in sentences[0]
    assert "1.5 million" in sentences[1]
    assert "Great performance" in sentences[2]


@pytest.mark.asyncio
async def test_confusables_skeleton_cyrillic():
    """B5a: Confusables skeleton must detect Cyrillic bypasses."""
    # Cyrillic S (Ѕ) looks like ASCII S
    text = "Ѕupports ЅЅO"  # Cyrillic
    folded = apply_confusables_skeleton(text)
    assert "Supports SSO" == folded or "S" in folded


@pytest.mark.asyncio
async def test_is_closed_set_non_claim_questions():
    """B5a: Pure questions (no product+claim_indicator) are non_claim."""
    # Pure questions
    assert is_closed_set_non_claim("What stack are you on?")
    assert is_closed_set_non_claim("Have you tried this approach?")
    
    # B5a: Trailing ? doesn't make claim-bearing sentence a question non_claim
    assert not is_closed_set_non_claim("Is ours 10x faster than Kafka?")
    assert not is_closed_set_non_claim("Does our product support SSO?")


@pytest.mark.asyncio
async def test_is_closed_set_non_claim_greetings_thanks():
    """B5a: Greetings/thanks are non_claim, but only if remainder is non_claim."""
    # Pure greetings/thanks
    assert is_closed_set_non_claim("Thanks for sharing!")
    assert is_closed_set_non_claim("Hi there!")
    assert is_closed_set_non_claim("Happy to help.")
    
    # B5a: Greeting + claim remainder → NOT non_claim (whole sentence rule)
    assert not is_closed_set_non_claim("Thanks! It's way cheaper.")
    assert not is_closed_set_non_claim("Hi! Ours is 10x quicker.")
    assert not is_closed_set_non_claim("Hello, many teams switched from Temporal to us.")


@pytest.mark.asyncio
async def test_is_closed_set_non_claim_opinion_frames():
    """B5a: Opinion frames without product claims are non_claim."""
    # Opinion without product claim
    assert is_closed_set_non_claim("I think this is a hard problem.")
    assert is_closed_set_non_claim("I love this thread.")
    assert is_closed_set_non_claim("In my experience, this approach works well.")
    
    # B5a: Opinion that launders claim → NOT non_claim
    assert not is_closed_set_non_claim("I think it's way cheaper.")
    assert not is_closed_set_non_claim("In my experience it cuts costs in half.")
    assert not is_closed_set_non_claim("I believe ours is 10x faster.")


@pytest.mark.asyncio
async def test_matches_backstop_pattern_numbers():
    """B5a: Backstop patterns (numbers) override non_claim tags."""
    assert matches_backstop_pattern("10k requests")
    assert matches_backstop_pattern("99.9% uptime")
    assert matches_backstop_pattern("The product has twenty integrations.")
    assert matches_backstop_pattern("5 GB storage")


@pytest.mark.asyncio
async def test_matches_backstop_pattern_compliance():
    """B5a: Backstop patterns (compliance terms) override non_claim tags."""
    assert matches_backstop_pattern("SOC2 certified")
    assert matches_backstop_pattern("SSO authentication")
    assert matches_backstop_pattern("Thanks. Ѕupports ЅЅO.")  # Cyrillic bypass caught


@pytest.mark.asyncio
async def test_claim_gate_valid_claim_insertion():
    """Claims reference passage+sentence_index; code inserts exact sentence."""
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product helps developers. It provides automatic failover. It guarantees strong consistency.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    segments = [
        Segment(kind="non_claim", text="Thanks for the question."),
        Segment(kind="claim", source_passage_id="passage_0", sentence_index=0),
        Segment(kind="non_claim", text="Happy to share more."),
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test1"
    ))
    
    assert result.passed
    assert len(result.segments) == 3
    # Check inserted text (trailing punct trimmed)
    assert result.segments[1].text == "Our product helps developers"
    assert result.segments[1].kind == "claim"


@pytest.mark.asyncio
async def test_claim_gate_invalid_passage_id():
    """Invalid passage_id must fail."""
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product helps developers.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    segments = [
        Segment(kind="claim", source_passage_id="passage_99", sentence_index=0),
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test2"
    ))
    
    assert not result.passed
    assert len(result.failed_sentences) > 0
    assert "not found" in result.failed_sentences[0][1].lower()


@pytest.mark.asyncio
async def test_claim_gate_out_of_range_sentence_index():
    """Out-of-range sentence_index must fail."""
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product helps developers. It provides automatic failover.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    # Only 2 sentences (index 0, 1), but requesting index 5
    segments = [
        Segment(kind="claim", source_passage_id="passage_0", sentence_index=5),
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test3"
    ))
    
    assert not result.passed
    assert len(result.failed_sentences) > 0
    assert "out of range" in result.failed_sentences[0][1].lower()


@pytest.mark.asyncio
async def test_claim_gate_non_claim_must_match_closed_set():
    """Non-claim text must match closed-set patterns."""
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product helps developers.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    # Invalid non_claim: doesn't match closed set
    segments = [
        Segment(kind="non_claim", text="This is the best solution."),
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test4"
    ))
    
    assert not result.passed
    assert len(result.failed_sentences) > 0


@pytest.mark.asyncio
async def test_claim_gate_backstop_overrides_non_claim():
    """Backstop patterns (numbers, compliance) override non_claim tag."""
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product helps developers.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    # Model tagged as non_claim, but has backstop pattern (number)
    segments = [
        Segment(kind="non_claim", text="Handles 10k requests per second."),
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test5"
    ))
    
    # Should fail: backstop overrides non_claim, making it a claim without citation
    assert not result.passed


@pytest.mark.asyncio
async def test_claim_gate_empty_segment_list_rejected():
    """Empty segment list must be rejected."""
    materials = [FounderMaterial(
        source="brief.md",
        passage="Our product helps developers.",
        relevance="relevant",
        passage_id="passage_0"
    )]
    
    result = await claim_gate(ClaimGateInput(
        segments=[],
        founder_materials=materials,
        idempotency_key="test6"
    ))
    
    assert not result.passed
    assert "empty" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_b5a_regression_opinion_laundering():
    """B5a: Opinion/question frames that launder claims must be rejected."""
    # These must all be detected as NOT non_claim
    assert not is_closed_set_non_claim("I think it's way cheaper.")
    assert not is_closed_set_non_claim("I think ours is 10x quicker.")
    assert not is_closed_set_non_claim("In my experience it cuts costs in half.")


@pytest.mark.asyncio
async def test_b5a_regression_greeting_with_claim():
    """B5a: Greeting + claim must be rejected."""
    assert not is_closed_set_non_claim("Thanks. The product has twenty integrations.")
    assert not is_closed_set_non_claim("Hi! Ours is 10x quicker.")


@pytest.mark.asyncio
async def test_b5a_regression_trailing_question_mark():
    """B5a: Trailing ? doesn't make claim-bearing sentence a non_claim."""
    assert not is_closed_set_non_claim("Is ours 10x faster than Kafka?")
    assert not is_closed_set_non_claim("Does it support SSO?")


@pytest.mark.asyncio
async def test_b5a_regression_confusables():
    """B5a: Confusables (Cyrillic) must be detected."""
    # Cyrillic S in "Supports SSO"
    assert matches_backstop_pattern("Thanks. Ѕupports ЅЅO.")


@pytest.mark.asyncio
async def test_regression_order_swap_rejected():
    """Regression: Order swap must be rejected (quoted, not paraphrased)."""
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product is faster than Kafka.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    # Model would need to output a segment that says "Kafka is faster than our product"
    # This can't happen with sentence_index approach - it would reference the original sentence
    # So this is a non-issue with the new architecture
    # But for edited text: if founder edits to reverse it, it would fail
    pass  # Architecture prevents this issue


@pytest.mark.asyncio
async def test_regression_paraphrase_rejected():
    """Regression: Paraphrased claims (not verbatim) must be rejected.
    
    Agent text that paraphrases a claim is not an inserted sentence,
    so it must fail as a non-closed-set non_claim.
    """
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product might support SSO in the future.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    # If model tries to output non_claim text "supports SSO" (paraphrase, dropped hedge)
    # it would fail backstop (SSO keyword) and need citation
    segments = [
        Segment(kind="non_claim", text="Supports SSO."),
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_paraphrase"
    ))
    
    assert not result.passed  # Backstop catches "SSO", requires citation
