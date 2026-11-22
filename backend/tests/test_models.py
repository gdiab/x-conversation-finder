"""Tests for data models."""

import pytest
from datetime import datetime

from backend.models import Evidence, FounderMaterial


def test_evidence_compute_confidence():
    """Test confidence computation from evidence shape."""
    evidence = Evidence(
        post_id="123",
        matched_topic="reliability",
        why_it_fits="Post discusses reliability",
        confidence_score=0.0,
        confidence_inputs={
            "lookup_succeeded": 1.0,
            "replies_open": 1.0,
            "topic_match_strength": 0.8,
            "freshness": 1.0,
            "author_reach": 0.5,
            "has_founder_material": 1.0,
        },
        founder_materials=[
            FounderMaterial(
                source="brief.md",
                passage="We help with reliability",
                relevance="Direct match",
            )
        ],
    )
    
    score = evidence.compute_confidence()
    
    assert score > 0.6
    assert score <= 1.0


def test_evidence_compute_confidence_low():
    """Test low confidence computation."""
    evidence = Evidence(
        post_id="123",
        matched_topic="reliability",
        why_it_fits="Weak match",
        confidence_score=0.0,
        confidence_inputs={
            "lookup_succeeded": 1.0,
            "replies_open": 0.0,
            "topic_match_strength": 0.2,
            "freshness": 0.1,
            "author_reach": 0.0,
            "has_founder_material": 0.0,
        },
        founder_materials=[],
    )
    
    score = evidence.compute_confidence()
    
    assert score < 0.4
