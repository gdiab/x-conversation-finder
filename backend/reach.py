"""Reach score of a review card (issue #49; rule in backend/config/reach.py).

Pure functions over the stored post dict. Computed by the store_card activity
and by the startup backfill (database.init_db), never in workflow code, so
no workflow history changes.
"""

import math
from typing import Any, Dict, Optional

from backend.config.reach import (
    ENGAGEMENT_ACTION_WEIGHT,
    ENGAGEMENT_FULL,
    FOLLOWERS_FLOOR,
    FOLLOWERS_FULL,
    REACH_HIGHLIGHT_LEVEL,
    REACH_RULE_VERSION,
    REACH_SORT_WEIGHT,
    REACH_WEIGHTS,
)

METRIC_KEYS = ("like_count", "reply_count", "repost_count", "quote_count")


def _count(value: Any) -> Optional[int]:
    """A non-negative integer count, or None for anything else (missing,
    bool, negative, float, string)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def engagement_score(engagement: int) -> float:
    """log10(1 + engagement) / log10(1 + ENGAGEMENT_FULL), capped at 1.0."""
    if engagement <= 0:
        return 0.0
    return round(min(1.0, math.log10(1 + engagement) / math.log10(1 + ENGAGEMENT_FULL)), 4)


def audience_score(followers: int) -> float:
    """Followers on a log scale between FOLLOWERS_FLOOR (0) and FOLLOWERS_FULL (1)."""
    if followers <= FOLLOWERS_FLOOR:
        return 0.0
    low, high = math.log10(FOLLOWERS_FLOOR), math.log10(FOLLOWERS_FULL)
    return round(min(1.0, (math.log10(followers) - low) / (high - low)), 4)


def compute_reach(post_data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The reach record stored with a card, or None.

    None when the post records a removal (issue #35: no author data is kept,
    and followers belong to the author) or any metric is missing or not a
    non-negative integer; such a card has no reach score and sorts by
    confidence alone.
    """
    if not isinstance(post_data, dict) or post_data.get("text_removed"):
        return None
    counts = {key: _count(post_data.get(key)) for key in METRIC_KEYS}
    author = post_data.get("author")
    followers = _count(author.get("followers_count")) if isinstance(author, dict) else None
    if followers is None or any(v is None for v in counts.values()):
        return None
    engagement = counts["like_count"] + ENGAGEMENT_ACTION_WEIGHT * (
        counts["reply_count"] + counts["repost_count"] + counts["quote_count"]
    )
    components = {
        "engagement_score": engagement_score(engagement),
        "audience_score": audience_score(followers),
    }
    score = sum(components[k] * w for k, w in REACH_WEIGHTS.items())
    return {
        "score": round(min(1.0, max(0.0, score)), 4),
        "inputs": {**counts, "followers_count": followers},
        "engagement": engagement,
        **components,
        "rule_version": REACH_RULE_VERSION,
    }


def stored_reach_score(reach: Any) -> Optional[float]:
    """The score of a stored reach record, or None when it has none."""
    if not isinstance(reach, dict):
        return None
    score = reach.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or math.isnan(score):
        return None
    return min(1.0, max(0.0, float(score)))


def reach_highlighted(reach: Any) -> bool:
    """Whether the card's reach is at or above REACH_HIGHLIGHT_LEVEL."""
    score = stored_reach_score(reach)
    return score is not None and score >= REACH_HIGHLIGHT_LEVEL


def queue_priority(confidence: Any, reach: Any) -> float:
    """confidence + REACH_SORT_WEIGHT * reach; confidence alone without reach."""
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or math.isnan(confidence):
        confidence = 0.0
    score = stored_reach_score(reach)
    # Not rounded: two cards whose stored scores differ by 0.0001 must not tie
    return float(confidence) + (REACH_SORT_WEIGHT * score if score is not None else 0.0)
