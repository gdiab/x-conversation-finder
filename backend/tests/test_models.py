"""Tests for data models: the confidence rule (v2, issue #4)."""

import pytest

from backend.config.confidence import CONFIDENCE_THRESHOLD, CONFIDENCE_WEIGHTS
from backend.models import Evidence, FounderMaterial


def make_evidence(**inputs) -> Evidence:
    base = {
        "lookup_succeeded": 1.0,
        "replies_open": 1.0,
        "has_founder_material": 1.0,
        "freshness": 1.0,
        "kind_score": 1.0,
        "asks_question": 1.0,
        "brief_overlap": 0.6,
        "author_reach": 0.5,
        "direct_post": 1.0,
    }
    base.update(inputs)
    return Evidence(
        post_id="123",
        matched_topic="reliability",
        why_it_fits="Post discusses reliability",
        confidence_score=0.0,
        confidence_inputs=base,
        founder_materials=[
            FounderMaterial(source="brief.md", passage="We help with reliability", relevance="Direct match")
        ],
    )


def test_weights_sum_to_one():
    assert sum(CONFIDENCE_WEIGHTS.values()) == pytest.approx(1.0)


def test_direct_question_clears_threshold():
    score = make_evidence().compute_confidence()
    assert CONFIDENCE_THRESHOLD < score <= 1.0


def test_always_on_features_alone_do_not_clear_threshold():
    """The v1 bug: lookup, replies open, freshness and material summed to 0.65.

    In v2 they are gates and a multiplier, so with nothing else they score 0.
    """
    score = make_evidence(
        kind_score=0.0, asks_question=0.0, brief_overlap=0.0, author_reach=0.0, direct_post=0.0,
    ).compute_confidence()
    assert score == 0.0


@pytest.mark.parametrize("gate", ["lookup_succeeded", "replies_open", "has_founder_material"])
def test_gate_at_zero_zeroes_the_score(gate):
    assert make_evidence(**{gate: 0.0}).compute_confidence() == 0.0


def test_missing_gate_counts_as_zero():
    evidence = make_evidence()
    del evidence.confidence_inputs["has_founder_material"]
    assert evidence.compute_confidence() == 0.0


def test_staleness_lowers_but_does_not_zero():
    fresh = make_evidence().compute_confidence()
    stale = make_evidence(freshness=0.1).compute_confidence()
    assert 0.0 < stale < fresh


def test_model_typed_legacy_input_is_ignored():
    """A topic_match_strength typed by a model has no weight in v2."""
    low = make_evidence(kind_score=0.35, asks_question=0.0, brief_overlap=0.0)
    high = make_evidence(kind_score=0.35, asks_question=0.0, brief_overlap=0.0, topic_match_strength=1.0)
    assert low.compute_confidence() == high.compute_confidence()
    assert low.compute_confidence() < CONFIDENCE_THRESHOLD


def test_out_of_range_and_non_numeric_inputs_are_clamped():
    assert make_evidence(kind_score=5.0).compute_confidence() == make_evidence(kind_score=1.0).compute_confidence()
    assert make_evidence(kind_score="1.0").compute_confidence() == make_evidence(kind_score=0.0).compute_confidence()
    assert make_evidence(kind_score=True).compute_confidence() == make_evidence(kind_score=0.0).compute_confidence()
