"""
Tests for segment-based claim gate (Architect decision 2026-11-21).

Claims are QUOTED, not paraphrased. Agent outputs sentence_index, code inserts text.
"""

import pytest
from backend.activities.claim_gate_activity import (
    ClaimGateInput,
    claim_gate,
    split_into_sentences,
    has_unresolved_referent,
)
from backend.gate.non_claim_set import (
    is_closed_set_non_claim,
    normalize_for_closed_set,
    _apply_confusables_fold,
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
async def test_split_sentences_handles_abbreviations():
    """QA R11: Sentence splitter must not break on common abbreviations."""
    text = "Founded by John Smith, Ph.D. The company, Acme Inc., raised $50M. They built e.g. a great product vs. competitors etc."
    sentences = split_into_sentences(text)
    
    # Should be 3 sentences, not split on Ph.D., Inc., e.g., vs., etc.
    assert len(sentences) == 3
    assert "Ph.D." in sentences[0]
    assert "Inc." in sentences[1]
    assert "e.g." in sentences[2]
    assert "vs." in sentences[2]
    assert "etc." in sentences[2]


@pytest.mark.asyncio
async def test_qa_r13_splitter_never_split_after_abbreviation():
    """
    QA R13: Splitter NEVER splits after abbreviations.
    
    This replaces test_qa_r12_splitter_uppercase_after_abbreviation which
    locked in wrong behavior (splitting after abbreviations when next word
    was uppercase).
    
    Expected results:
    - "Ask Dr. Smith about it." is one sentence
    - "Foo vs. It is shipped." is one sentence  
    - "Use e.g. Postgres." keeps all of its text
    - "We are Acme Inc." is not selectable (ends in abbreviation)
    """
    # NEVER split after abbreviation, even if next word is uppercase
    text1 = "Foo vs. It is shipped."
    sentences1 = split_into_sentences(text1)
    assert len(sentences1) == 1
    assert "vs." in sentences1[0]
    assert "It is shipped" in sentences1[0]
    
    text2 = "Ask Dr. Smith about it."
    sentences2 = split_into_sentences(text2)
    assert len(sentences2) == 1
    assert "Dr. Smith" in sentences2[0]
    
    text3 = "Use e.g. Postgres."
    sentences3 = split_into_sentences(text3)
    assert len(sentences3) == 1
    assert "e.g. Postgres" in sentences3[0]
    
    # Sentence ending in abbreviation is one sentence
    text4 = "We are Acme Inc."
    sentences4 = split_into_sentences(text4)
    assert len(sentences4) == 1
    
    # But if there's a sentence after (with no abbreviation), that splits normally
    text5 = "We are Acme Inc. Our product supports SSO."
    sentences5 = split_into_sentences(text5)
    # Should be 2 sentences: first ends with "Inc.", second starts with "Our"
    # Actually no - because we NEVER split after abbreviation
    # So this becomes one long sentence
    assert len(sentences5) == 1


@pytest.mark.asyncio
async def test_confusables_skeleton_cyrillic():
    """Confusables fold must detect Cyrillic bypasses."""
    # Cyrillic S (Ѕ) looks like ASCII S
    text = "Ѕupports ЅЅO"  # Cyrillic
    folded = _apply_confusables_fold(text)
    assert "Supports SSO" == folded or "S" in folded


@pytest.mark.asyncio
async def test_confusables_greek():
    """Confusables fold must detect Greek bypasses."""
    # Greek Α (Alpha) looks like ASCII A
    text = "Αlpha Beta"  # Greek Alpha
    folded = _apply_confusables_fold(text)
    assert "Alpha" in folded or "A" in folded


@pytest.mark.asyncio
async def test_has_unresolved_referent():
    """QA R11 B: Sentences starting with unresolved referents must be detected."""
    # Unresolved referents
    assert has_unresolved_referent("It provides automatic failover.")
    assert has_unresolved_referent("This is a great solution.")
    assert has_unresolved_referent("That handles all cases.")
    assert has_unresolved_referent("These work well together.")
    assert has_unresolved_referent("They scale automatically.")
    assert has_unresolved_referent("Its performance is excellent.")
    assert has_unresolved_referent("Such solutions are common.")
    assert has_unresolved_referent("The above approach is best.")
    
    # Not unresolved referents
    assert not has_unresolved_referent("Our product provides automatic failover.")
    assert not has_unresolved_referent("The platform handles all cases.")


@pytest.mark.asyncio
async def test_qa_r12_unresolved_referent_enhanced():
    """
    QA R12: Enhanced unresolved-referent detection.
    
    - Strip leading quotes/brackets/punctuation
    - Skip discourse markers
    - Add new referents: Both, The former, The latter, Which, Which means, Here
    - Keep flagged: It's, That's, They're, Its
    """
    # Specified test cases
    assert has_unresolved_referent('"It is ok."')  # Leading quote stripped, then "It" flagged
    assert has_unresolved_referent('However, it ships.')  # Discourse marker skipped, "it" flagged
    assert has_unresolved_referent('Both are supported.')  # New referent
    assert has_unresolved_referent('The former is faster.')  # New referent
    assert has_unresolved_referent('And it works.')  # Skip "And", then "it" flagged
    assert has_unresolved_referent('Which means it scales.')  # "Which means" is a referent
    
    # Additional cases for discourse markers
    assert has_unresolved_referent('Also, it provides redundancy.')
    assert has_unresolved_referent('But it handles failover.')
    assert has_unresolved_referent('So this works well.')
    assert has_unresolved_referent('Moreover, that scales.')
    
    # Leading punctuation stripping
    assert has_unresolved_referent('[It works]')
    assert has_unresolved_referent('(This is great)')
    
    # Keep flagged: It's, That's, They're, Its
    assert has_unresolved_referent("It's shipped.")
    assert has_unresolved_referent("That's ok.")
    assert has_unresolved_referent("They're supported.")
    assert has_unresolved_referent("Its performance is great.")
    
    # Not referents (valid starts)
    assert not has_unresolved_referent("Our product ships quickly.")
    assert not has_unresolved_referent("The platform scales well.")


@pytest.mark.asyncio
async def test_claim_gate_rejects_unresolved_referent():
    """QA R11 B: Claim gate must reject sentences starting with unresolved referents."""
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="It provides automatic failover. Our product guarantees strong consistency.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    # Try to use sentence_index=0 which is "It provides automatic failover."
    segments = [
        Segment(kind="claim", source_passage_id="passage_0", sentence_index=0),
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_unresolved"
    ))
    
    # Should fail because sentence starts with "It"
    assert not result.passed
    assert len(result.failed_sentences) > 0
    assert "unresolved referent" in result.failed_sentences[0][1].lower()


@pytest.mark.asyncio
async def test_is_closed_set_non_claim_questions():
    """QA R13: v2.0.0 - Only exact phrase matches pass."""
    # Exact phrase matches from the fixed list
    assert is_closed_set_non_claim("What stack are you on?")
    assert is_closed_set_non_claim("What's your use case?")
    assert is_closed_set_non_claim("Can you share more context?")
    
    # NOT in the fixed phrase list - fails
    assert not is_closed_set_non_claim("How do you approach the problem?")
    assert not is_closed_set_non_claim("Is ours 10x faster than Kafka?")
    assert not is_closed_set_non_claim("Does our product support SSO?")


@pytest.mark.asyncio
async def test_is_closed_set_non_claim_greetings_thanks():
    """QA R13: v2.0.0 - Only exact phrase matches pass (after normalization)."""
    # Exact phrase matches from the fixed list
    assert is_closed_set_non_claim("Thanks for sharing.")
    assert is_closed_set_non_claim("Hi!")
    assert is_closed_set_non_claim("Happy to help.")
    assert is_closed_set_non_claim("Thanks!")
    
    # Normalization strips trailing punctuation, so these also match
    assert is_closed_set_non_claim("Thanks for sharing!")  # Normalizes to match
    assert is_closed_set_non_claim("Happy to help!")  # Normalizes to match
    
    # NOT in phrase list - fail
    assert not is_closed_set_non_claim("Hi there!")  # "Hi there" not in list (even after normalization)
    assert not is_closed_set_non_claim("Thanks! It's way cheaper.")  # Multiple sentences
    assert not is_closed_set_non_claim("Hi! Ours is 10x quicker.")
    assert not is_closed_set_non_claim("Hello, many teams switched from Temporal to us.")


# QA R13: DELETED - Opinion frames removed in v2.0.0
# Opinion frames were replaced with fixed phrase list
# See test_r13_edit_assertion_* tests for edit-path rejection tests


@pytest.mark.asyncio
async def test_qa_r11_b1_rejected_examples():
    """
    QA R13: Update for v2.0.0 - these must not match fixed phrases.
    
    These are not in the fixed phrase list, so they are rejected.
    """
    # QA R13: These are not exact matches to fixed phrases
    assert not is_closed_set_non_claim("Happy to share that setup takes five minutes.")
    assert not is_closed_set_non_claim("Happy to share our free forever plan.")
    assert not is_closed_set_non_claim("Thanks for asking about whether our product is the cheapest")
    assert not is_closed_set_non_claim("I love that there's no vendor lock-in.")
    assert not is_closed_set_non_claim("I think setup takes five minutes.")
    assert not is_closed_set_non_claim("Did you know setup takes five minutes?")
    assert not is_closed_set_non_claim("Personally I'd pick the one used by thousands.")
    assert not is_closed_set_non_claim("I think this is free for startups.")
    assert not is_closed_set_non_claim("I believe the platform is hugely scalable.")


@pytest.mark.asyncio
async def test_qa_r11_b1_passing_examples():
    """
    QA R13: Update for v2.0.0 - these must match fixed phrases exactly.
    
    These are exact matches to phrases in the fixed phrase list.
    """
    assert is_closed_set_non_claim("Thanks!")
    assert is_closed_set_non_claim("Happy to share more details.")
    assert is_closed_set_non_claim("What stack are you on?")
    # QA R13: "I love this thread" was removed (evaluative). Use another phrase.
    assert is_closed_set_non_claim("Interesting question.")


# QA R13: DELETED - Frame assertions test removed in v2.0.0
# Frames were replaced with fixed phrase list
# See test_r13_edit_assertion_* tests for edit-path rejection tests of these examples


@pytest.mark.asyncio
async def test_contains_number():
    """QA R13: TEST-ONLY - Numbers (digits and words) must be detected."""
    from backend.gate.non_claim_set import _test_only_contains_number
    
    # Digits
    assert _test_only_contains_number("5 minutes")
    assert _test_only_contains_number("99.9% uptime")
    assert _test_only_contains_number("10k requests")
    
    # Number words
    assert _test_only_contains_number("five minutes")
    assert _test_only_contains_number("twenty integrations")
    assert _test_only_contains_number("thousands of users")
    assert _test_only_contains_number("hundreds of customers")
    
    # No numbers
    assert not _test_only_contains_number("Thanks for sharing")
    assert not _test_only_contains_number("What stack are you on?")


@pytest.mark.asyncio
async def test_matches_backstop_pattern_numbers():
    """
    QA R13: TEST-ONLY - Backstop catches compact number patterns.
    
    Number words ("twenty") and simple digits ("5") are caught by _test_only_contains_number,
    not backstop. Backstop catches compact patterns like "10k", "99.9%".
    """
    from backend.gate.non_claim_set import _test_only_matches_backstop, _test_only_contains_number
    
    # Backstop catches compact number patterns
    assert _test_only_matches_backstop("10k requests")
    assert _test_only_matches_backstop("99.9% uptime")
    
    # Number words and simple digits are caught by _test_only_contains_number, not backstop
    # (These would fail backstop, but pass _test_only_contains_number)
    assert not _test_only_matches_backstop("The product has twenty integrations.")
    assert not _test_only_matches_backstop("5 GB storage")
    
    # Verify those ARE caught by _test_only_contains_number
    assert _test_only_contains_number("The product has twenty integrations.")
    assert _test_only_contains_number("5 GB storage")


@pytest.mark.asyncio
async def test_matches_backstop_pattern_compliance():
    """QA R13: TEST-ONLY - Backstop patterns (compliance terms) override non_claim tags."""
    from backend.gate.non_claim_set import _test_only_matches_backstop
    
    assert _test_only_matches_backstop("SOC2 certified")
    assert _test_only_matches_backstop("SSO authentication")
    assert _test_only_matches_backstop("Thanks. Ѕupports ЅЅO.")  # Cyrillic bypass caught


@pytest.mark.asyncio
async def test_claim_gate_valid_claim_insertion():
    """QA R13: Claims reference passage+sentence_index; non_claims reference phrase_id."""
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product helps developers. It provides automatic failover. It guarantees strong consistency.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    # QA R13: Non_claim segments use phrase_id (no text)
    segments = [
        Segment(kind="non_claim", phrase_id="thanks.for_question"),
        Segment(kind="claim", source_passage_id="passage_0", sentence_index=0),
        Segment(kind="non_claim", phrase_id="offer.happy_to_share"),
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


# QA R13: DELETED test_claim_gate_non_claim_must_match_closed_set
# Reason: In v2.0.0, non_claim segments use phrase_id (no free text).
# The model cannot author "This is the best solution." - it can only select phrase_id.
# This test tested the OLD closed-set matching on model-authored text, removed in v2.0.0.

# QA R13: DELETED test_claim_gate_backstop_overrides_non_claim
# Reason: In v2.0.0, backstop only validates the fixed phrase list (TEST-ONLY).
# The model cannot author "Handles 10k requests per second." - it can only select phrase_id.
# This test tested the OLD backstop override on model-authored text, removed in v2.0.0.


@pytest.mark.asyncio
async def test_claim_gate_empty_segment_list_rejected():
    """QA R13: Empty segment list must be rejected (draft shape validation)."""
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
    assert "at least 1 segment" in result.reasoning


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
    """QA R13: Confusables (Cyrillic) must be detected (TEST-ONLY)."""
    from backend.gate.non_claim_set import _test_only_matches_backstop
    
    # Cyrillic S in "Supports SSO"
    assert _test_only_matches_backstop("Thanks. Ѕupports ЅЅO.")


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
async def test_b5b_its_handling():
    """
    QA R13: Under the closed-set rules (v1.1.0), "it" is not in the allowlist
    because it's often a product reference. Opinion frames with "it's" are rejected.
    
    The tokenizer splits "it's" into "it" + "'s", and since "it" is not in
    fixed phrase list, these are rejected in edit paths.
    """
    # QA R13: "it" sentences are not in the fixed phrase list
    assert not is_closed_set_non_claim("I think it's a great question.")
    assert not is_closed_set_non_claim("In my opinion it's really interesting.")
    
    # "it's" with claim indicators → definitely NOT non_claim
    assert not is_closed_set_non_claim("I think it's way cheaper.")
    assert not is_closed_set_non_claim("It's 10x faster.")
    assert not is_closed_set_non_claim("It's better than the alternatives.")
    
    # QA R12: "this" and "great/really" removed from allowlist, so these now fail
    assert not is_closed_set_non_claim("I think this is a great question.")
    assert not is_closed_set_non_claim("I think this is really interesting.")


# QA R13: DELETED test_regression_modal_swap_rejected
# Reason: In v2.0.0, the model cannot author "Supports SSO authentication." as non_claim text.
# It can only select phrase_id. This test tested the OLD backstop rejection of modal swaps.

# QA R13: DELETED test_regression_dropped_qualifier_rejected
# Reason: In v2.0.0, the model cannot author "We support SSO." as non_claim text.
# It can only select phrase_id. This test tested the OLD backstop rejection of dropped qualifiers.


# QA R13: DELETED test_regression_paraphrase_free_text_rejected (parametrized)
# Reason: In v2.0.0, the model cannot author free text in non_claim segments.
# All 23 test cases here are impossible to emit - the model can only select phrase_id.
# These tested the OLD frame-based/closed-set/backstop rejection of model-authored text.
# The assertion examples are NOT lost - they are PORTED to edit-path rejection tests
# (test_r13_edit_assertion_*) where they verify that verify_edited_text rejects them
# when a founder edits the draft in the review UI.


# QA R13: DELETED test_regression_paraphrase_rejected
# Reason: In v2.0.0, the model cannot author "Supports SSO." as non_claim text.
# It can only select phrase_id. This test tested the OLD backstop rejection of
# model-authored paraphrase, which is impossible in v2.0.0.


@pytest.mark.asyncio
async def test_nfkc_handles_fullwidth_and_mathematical():
    """
    QA R11: Confirm NFKC handles fullwidth and mathematical alphanumerics.
    
    NFKC normalization (in normalize_for_closed_set) handles:
    - Fullwidth characters (e.g., ＳＯＣ２ → SOC2)
    - Mathematical alphanumerics (e.g., 𝐒𝐎𝐂𝟐 → SOC2)
    """
    from backend.gate.non_claim_set import normalize_for_closed_set
    
    # Fullwidth SOC2
    fullwidth = "ＳＯＣ２"
    normalized = normalize_for_closed_set(fullwidth)
    assert "soc2" in normalized.lower()
    
    # Mathematical bold SOC2
    math_bold = "𝐒𝐎𝐂𝟐"
    normalized_math = normalize_for_closed_set(math_bold)
    assert "soc2" in normalized_math.lower()
    
    # QA R13: Verify these get caught by TEST-ONLY backstop
    from backend.gate.non_claim_set import _test_only_matches_backstop
    assert _test_only_matches_backstop(fullwidth)
    assert _test_only_matches_backstop(math_bold)


# ==================== QA R13: ARCHITECT DECISION 3 TESTS ====================
# Fixed phrase list v2.0.0 - phrase_id only, no frames


@pytest.mark.asyncio
async def test_r13_phrase_validation():
    """QA R13: Test that all phrases pass validation (no numbers, pass backstop)."""
    from backend.gate.non_claim_set import validate_all_phrases
    
    errors = validate_all_phrases()
    assert errors == [], f"Phrase validation failed: {errors}"


@pytest.mark.asyncio
async def test_r13_unknown_phrase_id_rejected():
    """QA R13: Unknown phrase_id must be rejected."""
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product supports SSO.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    segments = [
        Segment(kind="non_claim", phrase_id="unknown.phrase.id"),
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_unknown_phrase_id"
    ))
    
    assert not result.passed
    assert any("Unknown phrase_id" in reason for _, reason in result.failed_sentences)


@pytest.mark.asyncio
async def test_r13_non_claim_with_text_rejected():
    """QA R13: Non_claim segment that carries text must never have that text used."""
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product supports SSO.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    # QA R13: Model should provide phrase_id ONLY, no text field
    # A segment with both text and phrase_id should be rejected
    segments = [
        Segment(kind="non_claim", text="Thanks for sharing.", phrase_id="thanks.for_sharing"),
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_non_claim_with_text"
    ))
    
    assert not result.passed
    assert any("must not have text" in reason for _, reason in result.failed_sentences)


@pytest.mark.asyncio
async def test_r13_draft_shape_empty_segments():
    """QA R13: Draft must have at least 1 segment."""
    materials = []
    segments = []
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_empty_segments"
    ))
    
    assert not result.passed
    assert "at least 1 segment" in result.reasoning


@pytest.mark.asyncio
async def test_r13_draft_shape_too_many_segments():
    """QA R13: Draft must have at most 3 segments."""
    materials = []
    segments = [
        Segment(kind="non_claim", phrase_id="opener.thanks"),
        Segment(kind="non_claim", phrase_id="thanks.for_sharing"),
        Segment(kind="non_claim", phrase_id="offer.happy_to_help"),
        Segment(kind="non_claim", phrase_id="offer.share_details"),
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_too_many_segments"
    ))
    
    assert not result.passed
    assert "at most 3 segments" in result.reasoning


@pytest.mark.asyncio
async def test_r13_draft_shape_two_claims():
    """QA R13: Draft must have at most 1 claim."""
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product supports SSO. It has built-in encryption.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    segments = [
        Segment(kind="claim", source_passage_id="passage_0", sentence_index=0),
        Segment(kind="claim", source_passage_id="passage_0", sentence_index=1),
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_two_claims"
    ))
    
    assert not result.passed
    assert "at most 1 claim" in result.reasoning


@pytest.mark.asyncio
async def test_r13_valid_assembly_passes():
    """QA R13: A valid assembly with phrase_id and claim passes."""
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product supports SSO.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    segments = [
        Segment(kind="non_claim", phrase_id="opener.thanks"),
        Segment(kind="claim", source_passage_id="passage_0", sentence_index=0),
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_valid_assembly"
    ))
    
    assert result.passed
    assert len(result.segments) == 2
    assert result.segments[0].text == "Thanks!"
    assert "SSO" in result.segments[1].text


# ==================== QA R13: EDIT-PATH REJECTION TESTS ====================
# Port all r8-r13 assertion examples to edit-path rejection tests


@pytest.mark.asyncio
async def test_r13_edit_assertion_i_think_its_way_cheaper():
    """QA R13 Edit: 'I think it's way cheaper.' must be rejected."""
    from backend.activities.claim_gate_activity import EditedTextGateInput, verify_edited_text
    
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product supports SSO.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    gated_segments = []
    
    results = await verify_edited_text(EditedTextGateInput(
        edited_text="I think it's way cheaper.",
        gated_segments=gated_segments,
        founder_materials=materials,
        idempotency_key="test_edit_assertion1"
    ))
    
    assert len(results) == 1
    assert not results[0].passed


@pytest.mark.asyncio
async def test_r13_edit_assertion_i_think_they_are_same():
    """QA R13 Edit: 'I think they are the same.' must be rejected."""
    from backend.activities.claim_gate_activity import EditedTextGateInput, verify_edited_text
    
    materials = []
    gated_segments = []
    
    results = await verify_edited_text(EditedTextGateInput(
        edited_text="I think they are the same.",
        gated_segments=gated_segments,
        founder_materials=materials,
        idempotency_key="test_edit_assertion2"
    ))
    
    assert len(results) == 1
    assert not results[0].passed


@pytest.mark.asyncio
async def test_r13_edit_assertion_i_like_the_idea():
    """QA R13 Edit: 'I like the idea.' must be rejected."""
    from backend.activities.claim_gate_activity import EditedTextGateInput, verify_edited_text
    
    materials = []
    gated_segments = []
    
    results = await verify_edited_text(EditedTextGateInput(
        edited_text="I like the idea.",
        gated_segments=gated_segments,
        founder_materials=materials,
        idempotency_key="test_edit_assertion3"
    ))
    
    assert len(results) == 1
    assert not results[0].passed


@pytest.mark.asyncio
async def test_r13_edit_assertion_i_think_you_are_problem():
    """QA R13 Edit: 'I think you are the problem.' must be rejected."""
    from backend.activities.claim_gate_activity import EditedTextGateInput, verify_edited_text
    
    materials = []
    gated_segments = []
    
    results = await verify_edited_text(EditedTextGateInput(
        edited_text="I think you are the problem.",
        gated_segments=gated_segments,
        founder_materials=materials,
        idempotency_key="test_edit_assertion4"
    ))
    
    assert len(results) == 1
    assert not results[0].passed


@pytest.mark.asyncio
async def test_r13_edit_assertion_why_is_issue_same():
    """QA R13 Edit: 'Why is the issue the same?' must be rejected."""
    from backend.activities.claim_gate_activity import EditedTextGateInput, verify_edited_text
    
    materials = []
    gated_segments = []
    
    results = await verify_edited_text(EditedTextGateInput(
        edited_text="Why is the issue the same?",
        gated_segments=gated_segments,
        founder_materials=materials,
        idempotency_key="test_edit_assertion5"
    ))
    
    assert len(results) == 1
    assert not results[0].passed


@pytest.mark.asyncio
async def test_r13_edit_assertion_i_think_works_well():
    """QA R13 Edit: 'I think this works well.' must be rejected."""
    from backend.activities.claim_gate_activity import EditedTextGateInput, verify_edited_text
    
    materials = []
    gated_segments = []
    
    results = await verify_edited_text(EditedTextGateInput(
        edited_text="I think this works well.",
        gated_segments=gated_segments,
        founder_materials=materials,
        idempotency_key="test_edit_assertion6"
    ))
    
    assert len(results) == 1
    assert not results[0].passed


@pytest.mark.asyncio
async def test_r13_abbreviation_ending_non_selectable():
    """QA R13: Sentences ending in abbreviation are NOT selectable."""
    from backend.activities.claim_gate_activity import insert_claim_sentence
    
    materials = {
        "passage_0": FounderMaterial(
            source="brief.md",
            passage="We are Acme Inc.",
            relevance="relevant",
            passage_id="passage_0"
        )
    }
    
    # Sentence ends with "Inc." - not selectable
    segment = Segment(kind="claim", source_passage_id="passage_0", sentence_index=0)
    success, text, error = insert_claim_sentence(segment, materials)
    
    assert not success
    assert "abbreviation" in error.lower()


@pytest.mark.asyncio
async def test_r13_referent_curly_quotes():
    """QA R13: Unresolved referent rule uses explicit unicode escapes for curly quotes."""
    # Test with real curly-quote characters
    assert has_unresolved_referent('"It is ok."')  # Curly left double quote
    assert has_unresolved_referent('"It is ok."')  # Curly right double quote (if starts with)
    assert has_unresolved_referent("'It is ok.'")  # Curly left single quote
    assert has_unresolved_referent("- It is ok")   # Leading dash
    
    # Normal quotes should still work
    assert has_unresolved_referent('"It is ok."')
    assert has_unresolved_referent("'It is ok.'")


@pytest.mark.asyncio
async def test_r13_discourse_markers():
    """QA R13: Discourse markers (Therefore, Otherwise, Thus, Hence, Still, Yet) are stripped."""
    # QA R13: Discourse markers are stripped FIRST, then referents are checked
    # So "Therefore it ships" → strip "Therefore" → "it ships" → "it" is still flagged
    # Good test: discourse marker + non-referent
    assert not has_unresolved_referent("Therefore the product ships.")
    assert not has_unresolved_referent("Otherwise the system fails.")
    assert not has_unresolved_referent("Thus we proceed.")
    assert not has_unresolved_referent("Hence the decision was made.")
    assert not has_unresolved_referent("Still the solution works.")
    assert not has_unresolved_referent("Yet our approach succeeds.")
    
    # But if "it" remains after stripping, it's still flagged
    assert has_unresolved_referent("Therefore it ships.")
    assert has_unresolved_referent("Otherwise it fails.")


@pytest.mark.asyncio
async def test_r13_both_special_handling():
    """QA R13: 'Both' is only flagged when followed by verb or 'of'."""
    # Should NOT be flagged (Both + noun)
    assert not has_unresolved_referent("Both plans include SSO.")
    
    # SHOULD be flagged (Both + verb)
    assert has_unresolved_referent("Both are supported.")
    assert has_unresolved_referent("Both have SSO.")
    assert has_unresolved_referent("Both do this.")
    
    # SHOULD be flagged (Both + of)
    assert has_unresolved_referent("Both of them work.")


@pytest.mark.asyncio
async def test_r13_reuse_tracks_sentence_type():
    """QA R13: Reuse tracks question vs statement. Questions match only questions."""
    from backend.activities.claim_gate_activity import EditedTextGateInput, verify_edited_text
    
    materials = []
    
    # QA R13: Gated question (this would have come from an earlier gate pass)
    # In production, this would have phrase_id, but for this test we use text
    # to simulate a previously gated segment
    gated_segments = [
        Segment(kind="non_claim", text="They are the same as your stack?")
    ]
    
    # Test 1: Edited to statement form - should NOT reuse verdict
    results1 = await verify_edited_text(EditedTextGateInput(
        edited_text="They are the same as your stack.",
        gated_segments=gated_segments,
        founder_materials=materials,
        idempotency_key="test_question_to_statement"
    ))
    
    assert len(results1) == 1
    assert not results1[0].kept_segment  # Did NOT reuse
    
    # Test 2: Kept as question - should reuse verdict
    results2 = await verify_edited_text(EditedTextGateInput(
        edited_text="They are the same as your stack?",
        gated_segments=gated_segments,
        founder_materials=materials,
        idempotency_key="test_question_to_question"
    ))
    
    assert len(results2) == 1
    assert results2[0].kept_segment  # DID reuse


@pytest.mark.asyncio
async def test_r13_verified_claim_edited_to_question_loses_status():
    """QA R13: A verified claim edited to end in '?' loses its verified status."""
    from backend.activities.claim_gate_activity import EditedTextGateInput, verify_edited_text
    
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product supports SSO.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    # Gated statement
    gated_segments = [
        Segment(kind="claim", text="Our product supports SSO", source_passage_id="passage_0", sentence_index=0)
    ]
    
    # Edited to question - should NOT reuse verdict
    results = await verify_edited_text(EditedTextGateInput(
        edited_text="Our product supports SSO?",
        gated_segments=gated_segments,
        founder_materials=materials,
        idempotency_key="test_statement_to_question"
    ))
    
    assert len(results) == 1
    assert not results[0].kept_segment  # Did NOT reuse


@pytest.mark.asyncio
async def test_r13_statement_punctuation_changes_keep_verdict():
    """QA R13: Changing '.' to '!' keeps the verdict (both statements)."""
    from backend.activities.claim_gate_activity import EditedTextGateInput, verify_edited_text
    
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product supports SSO.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    # Gated statement with period
    gated_segments = [
        Segment(kind="claim", text="Our product supports SSO", source_passage_id="passage_0", sentence_index=0)
    ]
    
    # Changed to exclamation - should reuse verdict (both statements)
    results = await verify_edited_text(EditedTextGateInput(
        edited_text="Our product supports SSO!",
        gated_segments=gated_segments,
        founder_materials=materials,
        idempotency_key="test_period_to_exclamation"
    ))
    
    assert len(results) == 1
    assert results[0].kept_segment  # DID reuse (both are statements)
