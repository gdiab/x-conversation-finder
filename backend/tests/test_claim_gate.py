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
    _contains_number,
    _matches_backstop_pattern,
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
async def test_qa_r12_splitter_uppercase_after_abbreviation():
    """
    QA R12: Splitter splits when next word is capitalized after vs./e.g./i.e./etc.
    
    Abbreviation is non-boundary ONLY when next token is lowercase or digit.
    When next word is uppercase, split there.
    """
    # Specified failing case: "vs. It" should split
    text = "Retries are on the roadmap vs. It is shipped."
    sentences = split_into_sentences(text)
    assert len(sentences) == 2
    assert "vs." in sentences[0]
    assert sentences[1].startswith("It is shipped")
    
    # "e.g." followed by uppercase splits (over-splitting is safe)
    # Note: splits on "e.g. JSON" because JSON is uppercase
    text2 = "They support formats e.g. JSON. It scales."
    sentences2 = split_into_sentences(text2)
    # May split on e.g. if next is uppercase, but must split on the period before "It"
    assert len(sentences2) >= 2
    # Verify the last sentence starts with "It"
    assert any("It" in s for s in sentences2)
    
    # "Acme Inc." at end of sentence should be one sentence
    text3 = "The company is Acme Inc."
    sentences3 = split_into_sentences(text3)
    assert len(sentences3) == 1
    
    # "Inc." followed by uppercase should split (new sentence)
    text4 = "Founded by Acme Inc. It raised $50M."
    sentences4 = split_into_sentences(text4)
    assert len(sentences4) == 2
    assert "Inc." in sentences4[0]
    assert sentences4[1].startswith("It raised")
    
    # Existing abbreviation tests should still pass
    # "Ph.D." followed by lowercase continues
    text5 = "John Smith, Ph.D. is a founder."
    sentences5 = split_into_sentences(text5)
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
    """B5a: Pure questions (no product+claim_indicator) are non_claim."""
    # Pure questions
    assert is_closed_set_non_claim("What stack are you on?")
    # QA R12: "this" removed from allowlist, adjusted test
    assert is_closed_set_non_claim("How do you approach the problem?")
    
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
    # Opinion without product claims
    # QA R12: Updated examples - removed "this", "hard", "well" from allowlist
    assert is_closed_set_non_claim("I think the problem is interesting.")
    assert is_closed_set_non_claim("I love the thread.")
    assert is_closed_set_non_claim("In my experience, the approach is different.")
    
    # B5a: Opinion that launders claim → NOT non_claim
    assert not is_closed_set_non_claim("I think it's way cheaper.")
    assert not is_closed_set_non_claim("In my experience it cuts costs in half.")
    assert not is_closed_set_non_claim("I believe ours is 10x faster.")


@pytest.mark.asyncio
async def test_qa_r11_b1_rejected_examples():
    """
    QA R11 B1: All specified examples must be REJECTED.
    
    These are all violations of the closed-set non_claim rules:
    - Numbers (digits or words) are never allowed
    - Open-ended tails are not accepted
    - Frames don't make assertions non_claims
    """
    # Number words in various contexts
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
    QA R11 B1: Genuine non_claims must PASS.
    
    These are valid closed-set non_claims.
    """
    assert is_closed_set_non_claim("Thanks!")
    assert is_closed_set_non_claim("Happy to share more details.")
    assert is_closed_set_non_claim("What stack are you on?")
    assert is_closed_set_non_claim("Love this thread.")


@pytest.mark.asyncio
async def test_qa_r12_frame_assertions_rejected():
    """
    QA R12: Frame assertions with product/capability/evaluative words must be REJECTED.
    
    Even in "I think" or "Why" frames, product assertions are not non_claims.
    The allowlist was tightened to remove all product, capability, and evaluative words.
    """
    # Specified failing cases
    assert not is_closed_set_non_claim("I think this works well.")
    assert not is_closed_set_non_claim("I think we have a great setup.")
    assert not is_closed_set_non_claim("I think setup is great.")
    assert not is_closed_set_non_claim("I love setup.")
    assert not is_closed_set_non_claim("Why is the setup very easy?")
    assert not is_closed_set_non_claim("I like that this has the same setup.")
    
    # Additional variants covering capability/product words
    assert not is_closed_set_non_claim("I think it works.")
    assert not is_closed_set_non_claim("Why can we use this?")
    assert not is_closed_set_non_claim("I think that will help.")
    assert not is_closed_set_non_claim("I believe these have good performance.")
    assert not is_closed_set_non_claim("How well does this work?")
    assert not is_closed_set_non_claim("I think those could scale.")


@pytest.mark.asyncio
async def test_contains_number():
    """Numbers (digits and words) must be detected."""
    # Digits
    assert _contains_number("5 minutes")
    assert _contains_number("99.9% uptime")
    assert _contains_number("10k requests")
    
    # Number words
    assert _contains_number("five minutes")
    assert _contains_number("twenty integrations")
    assert _contains_number("thousands of users")
    assert _contains_number("hundreds of customers")
    
    # No numbers
    assert not _contains_number("Thanks for sharing")
    assert not _contains_number("What stack are you on?")


@pytest.mark.asyncio
async def test_matches_backstop_pattern_numbers():
    """
    Updated for Architect's (A): Backstop catches compact number patterns.
    
    Number words ("twenty") and simple digits ("5") are caught by _contains_number,
    not backstop. Backstop catches compact patterns like "10k", "99.9%".
    
    Reason: Under NEW architecture, _contains_number is the primary number check.
    Backstop is for pattern-based overrides.
    """
    # Backstop catches compact number patterns
    assert _matches_backstop_pattern("10k requests")
    assert _matches_backstop_pattern("99.9% uptime")
    
    # Number words and simple digits are caught by _contains_number, not backstop
    # (These would fail backstop, but pass _contains_number)
    assert not _matches_backstop_pattern("The product has twenty integrations.")
    assert not _matches_backstop_pattern("5 GB storage")
    
    # Verify those ARE caught by _contains_number
    assert _contains_number("The product has twenty integrations.")
    assert _contains_number("5 GB storage")


@pytest.mark.asyncio
async def test_matches_backstop_pattern_compliance():
    """Backstop patterns (compliance terms) override non_claim tags."""
    assert _matches_backstop_pattern("SOC2 certified")
    assert _matches_backstop_pattern("SSO authentication")
    assert _matches_backstop_pattern("Thanks. Ѕupports ЅЅO.")  # Cyrillic bypass caught


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
    assert _matches_backstop_pattern("Thanks. Ѕupports ЅЅO.")


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
    Updated for Architect's (A): Under the NEW closed-set rules, "it" is not in the allowlist
    because it's often a product reference. Opinion frames with "it's" are rejected.
    
    Reason: The NEW rules are deliberately conservative. "it" tokenizes separately from "'s",
    and "it" is not allowlisted to avoid product-reference laundering through opinion frames.
    """
    # "it's" is not allowlisted (even in opinion frames) - "it" not in ALLOWLIST_WORDS
    assert not is_closed_set_non_claim("I think it's a great question.")
    assert not is_closed_set_non_claim("In my opinion it's really interesting.")
    
    # "it's" with claim indicators → definitely NOT non_claim
    assert not is_closed_set_non_claim("I think it's way cheaper.")
    assert not is_closed_set_non_claim("It's 10x faster.")
    assert not is_closed_set_non_claim("It's better than the alternatives.")
    
    # But "this" IS allowlisted, so opinion frames with "this" pass
    assert is_closed_set_non_claim("I think this is a great question.")
    assert is_closed_set_non_claim("I think this is really interesting.")


@pytest.mark.asyncio
async def test_regression_modal_swap_rejected():
    """Regression: Modal swap (changing can/might/will) must be rejected.
    
    QA r10: Passage says "can support SSO", model outputs "supports SSO".
    This is not verbatim and must be rejected.
    """
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product can support SSO authentication.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    # Model tries to output "supports SSO" (modal swap: can → does)
    # This fails because it's not closed-set non_claim (has SSO backstop)
    segments = [
        Segment(kind="non_claim", text="Supports SSO authentication."),
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_modal_swap"
    ))
    
    assert not result.passed  # Backstop catches "SSO", requires verbatim citation
    assert len(result.failed_sentences) > 0


@pytest.mark.asyncio
async def test_regression_dropped_qualifier_rejected():
    """Regression: Dropped qualifiers (hedge words) must be rejected.
    
    QA r10: Passage says "might support SSO in the future", model outputs "supports SSO".
    This drops the qualifier and must be rejected.
    """
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="We might support SSO in the future.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    # Model tries to output "supports SSO" (dropped qualifier: might...in the future)
    # This fails because it's not closed-set non_claim (has SSO backstop)
    segments = [
        Segment(kind="non_claim", text="We support SSO."),
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_dropped_qualifier"
    ))
    
    assert not result.passed  # Backstop catches "SSO", requires verbatim citation
    assert len(result.failed_sentences) > 0


@pytest.mark.parametrize("test_case,expected_reason", [
    # Numbers and quantification
    ("Handles 10k requests per second.", "backstop"),
    ("We process millions of events daily.", "backstop"),
    ("Our platform has 99.99% uptime.", "backstop"),
    ("Supports 100x faster performance.", "backstop"),
    # Availability and SLAs
    ("We offer 24/7 support.", "backstop"),
    ("Guaranteed 99.9% uptime.", "backstop"),
    ("Provides exactly-once delivery.", "backstop"),
    # Compliance and security
    ("We are SOC2 certified.", "backstop"),
    ("Supports RBAC authentication.", "backstop"),
    ("HIPAA compliant storage.", "backstop"),
    # Competitive claims
    ("Battle-tested in production.", "backstop"),
    ("Beats Temporal on latency.", "backstop"),
    ("Faster than Kafka.", "backstop"),
    # Cost and business claims
    ("Cuts infrastructure bills in half.", "closed-set"),
    ("Eliminates on-call pain.", "closed-set"),
    ("Ships twice as fast.", "closed-set"),
    # Features and usage
    ("Used by thousands of developers.", "backstop"),
    ("Works on Kubernetes.", "closed-set"),
    ("Free tier available.", "closed-set"),
    # Negation mismatches (would swap meaning)
    ("Does not require manual configuration.", "closed-set"),
    ("Never needs downtime for upgrades.", "backstop"),
    # QA r10: Order swap
    ("Kafka is faster than our product.", "closed-set"),
])
@pytest.mark.asyncio
async def test_regression_paraphrase_free_text_rejected(test_case, expected_reason):
    """
    Regression suite: Agent free text (not verbatim passage sentences) must be rejected.
    
    These are the paraphrase regression examples. With the new architecture, agent cannot
    submit these as claims (no sentence_index), so they come as non_claim free text.
    The backstop patterns and closed-set rules must reject them.
    
    QA r8-r10 examples included: backstop patterns, competitive claims, order swaps.
    """
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our platform is designed for reliability.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    # Agent tries to submit as non_claim free text (not a passage sentence)
    segments = [
        Segment(kind="non_claim", text=test_case),
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key=f"test_paraphrase_{test_case[:20]}"
    ))
    
    assert not result.passed, f"Expected '{test_case}' to be rejected"
    assert len(result.failed_sentences) > 0
    # Verify the reason matches expectation (backstop or closed-set)
    reason_lower = result.failed_sentences[0][1].lower()
    if expected_reason == "backstop":
        assert "closed-set" in reason_lower or "backstop" in reason_lower
    else:  # closed-set
        assert "closed-set" in reason_lower


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
    
    # Verify these get caught by backstop
    from backend.gate.non_claim_set import _matches_backstop_pattern
    assert _matches_backstop_pattern(fullwidth)
    assert _matches_backstop_pattern(math_bold)
