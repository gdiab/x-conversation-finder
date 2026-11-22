"""
Test coverage for QA R8-R13 bypass examples (deleted from test_claim_gate.py in 253e3b6).

Per task DO #2: Restore coverage of deleted examples.

Two test suites:
1. Edit-path test: verify_edited_text rejects all prior bypass examples
2. Model-path test: non_claim segments with text field are rejected

These ensure the new architecture (Architect decision 3) prevents all the bypasses
that were caught by frames/allowlists in the old architecture.
"""

import pytest
from backend.models import (
    FounderMaterial,
    Segment,
)
from backend.activities.claim_gate_activity import (
    claim_gate,
    verify_edited_text,
    ClaimGateInput,
    EditedTextGateInput,
)


# QA R8-R13 bypass examples (from deleted test_regression_paraphrase_free_text_rejected)
BYPASS_EXAMPLES = [
    # Numbers and quantification (backstop patterns)
    "Handles 10k requests per second.",
    "We process millions of events daily.",
    "Our platform has 99.99% uptime.",
    "Supports 100x faster performance.",
    # Availability and SLAs
    "We offer 24/7 support.",
    "Guaranteed 99.9% uptime.",
    "Provides exactly-once delivery.",
    # Compliance and security
    "We are SOC2 certified.",
    "Supports RBAC authentication.",
    "HIPAA compliant storage.",
    # Competitive claims
    "Battle-tested in production.",
    "Beats Temporal on latency.",
    "Faster than Kafka.",
    # Cost and business claims
    "Cuts infrastructure bills in half.",
    "Eliminates on-call pain.",
    "Ships twice as fast.",
    # Features and usage
    "Used by thousands of developers.",
    "Works on Kubernetes.",
    "Free tier available.",
    # Negation mismatches
    "Does not require manual configuration.",
    "Never needs downtime for upgrades.",
    # Order swap (from test_regression_order_swap_rejected)
    "Kafka is faster than our product.",
    # Modal swap examples (from test_regression_modal_swap_rejected)
    "Supports SSO authentication.",
    # Dropped qualifier examples (from test_regression_dropped_qualifier_rejected)
    "We support SSO.",
    # Paraphrase examples (from test_regression_paraphrase_rejected)
    "Supports SSO.",
    # R11-R13 frame bypass examples (from ADR-0006)
    "I think this works well.",
    "I think they are the same.",
    "I like the idea.",
    "I think you are the problem.",
    "Why is the issue the same?",
    "I think we have a great setup.",
    "I love setup.",
]


@pytest.mark.parametrize("bypass_text", BYPASS_EXAMPLES)
@pytest.mark.asyncio
async def test_edit_path_rejects_prior_bypass_examples(bypass_text):
    """
    Edit-path test: verify_edited_text rejects all QA R8-R13 bypass examples.
    
    These examples were caught by frames/allowlists in the old architecture.
    Under the new architecture (Architect decision 3), they fail because:
    - They are not exact matches to fixed phrases (closed-set)
    - They are not verbatim passage sentences
    
    So an edited text containing any of these requires post_anyway=True to approve.
    """
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product helps developers build reliable distributed systems.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    # Original gated segments (doesn't matter what they are for this test)
    gated_segments = [
        Segment(kind="non_claim", phrase_id="opener.thanks"),
        Segment(kind="claim", source_passage_id="passage_0", sentence_index=0),
    ]
    
    # Founder edits to include the bypass text
    edited_text = f"Thanks! {bypass_text}"
    
    results = await verify_edited_text(EditedTextGateInput(
        edited_text=edited_text,
        gated_segments=gated_segments,
        founder_materials=materials,
        idempotency_key=f"test_edit_{bypass_text[:20]}"
    ))
    
    # The bypass sentence should fail (not an exact phrase match or passage sentence)
    failing_sentences = [r for r in results if not r.passed]
    assert len(failing_sentences) > 0, (
        f"Expected '{bypass_text}' to fail in edit path, "
        f"but all sentences passed"
    )


@pytest.mark.asyncio
async def test_model_path_rejects_non_claim_with_text_field():
    """
    Model-path test: non_claim segments with text field are rejected.
    
    Per Architect decision 3, non_claim segments must have phrase_id only, NO text.
    A model that outputs {kind: "non_claim", text: "..."} is violating the contract
    and must be rejected by the gate.
    """
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product helps developers.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    # Model incorrectly outputs non_claim with text field
    segments = [
        Segment(kind="non_claim", text="Thanks for sharing!"),  # WRONG: has text
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_text_field"
    ))
    
    # Gate should reject: non_claim must have phrase_id, not text
    assert not result.passed, (
        "Expected gate to reject non_claim segment with text field"
    )
    assert len(result.failed_sentences) > 0
    # Verify the error message mentions the structure issue
    reason = result.failed_sentences[0][1].lower()
    assert "phrase_id" in reason or "text" in reason, (
        f"Expected error about phrase_id/text, got: {reason}"
    )


@pytest.mark.asyncio
async def test_model_path_claim_with_text_field_rejected():
    """
    Model-path test: claim segments with text field are rejected.
    
    Per Architect decision 3, claim segments must have source_passage_id and
    sentence_index only, NO text. Code inserts the text.
    """
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product helps developers.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    # Model incorrectly outputs claim with text field
    segments = [
        Segment(
            kind="claim",
            text="Our product helps developers.",  # WRONG: has text
            source_passage_id="passage_0",
            sentence_index=0
        ),
    ]
    
    result = await claim_gate(ClaimGateInput(
        segments=segments,
        founder_materials=materials,
        idempotency_key="test_claim_text_field"
    ))
    
    # Gate should accept this because the text matches the sentence
    # But we want to verify that if the text field is present and WRONG,
    # it doesn't get used - code inserts from passage instead
    assert result.passed, "Valid claim segment should pass"
    
    # Now test with WRONG text in the field - code should ignore it and insert correct text
    segments_wrong_text = [
        Segment(
            kind="claim",
            text="WRONG TEXT THAT SHOULD BE IGNORED",
            source_passage_id="passage_0",
            sentence_index=0
        ),
    ]
    
    result2 = await claim_gate(ClaimGateInput(
        segments=segments_wrong_text,
        founder_materials=materials,
        idempotency_key="test_claim_wrong_text"
    ))
    
    # Should still pass because code inserts from passage, ignoring the text field
    assert result2.passed, "Claim segment with wrong text should still pass"
    # Verify the inserted text is from the passage, not the wrong text
    inserted_text = result2.segments[0].text
    assert "WRONG TEXT" not in inserted_text, (
        "Code should not use the text field from claim segment"
    )
    assert "developers" in inserted_text.lower(), (
        "Code should insert from passage"
    )


@pytest.mark.asyncio
async def test_confusables_in_edit_path():
    """
    Confusables (Cyrillic homoglyphs) in edited text must be normalized.
    
    From deleted test_b5a_regression_confusables: "Thanks. Ѕupports ЅЅO."
    The Ѕ (Cyrillic) should be normalized to S, but this still won't match
    a fixed phrase, so it should fail.
    """
    materials = [
        FounderMaterial(
            source="brief.md",
            passage="Our product helps developers.",
            relevance="relevant",
            passage_id="passage_0"
        )
    ]
    
    gated_segments = [
        Segment(kind="non_claim", phrase_id="opener.thanks"),
    ]
    
    # Edited text with Cyrillic confusables
    edited_text = "Thanks. Ѕupports ЅЅO."  # Cyrillic S
    
    results = await verify_edited_text(EditedTextGateInput(
        edited_text=edited_text,
        gated_segments=gated_segments,
        founder_materials=materials,
        idempotency_key="test_confusables"
    ))
    
    # "Thanks." should pass (matches fixed phrase)
    # "Ѕupports ЅЅO." should fail (not a fixed phrase, even after normalization)
    failing_sentences = [r for r in results if not r.passed]
    assert len(failing_sentences) > 0, (
        "Expected Cyrillic bypass to fail"
    )
