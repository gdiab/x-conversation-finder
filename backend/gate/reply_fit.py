"""Decide whether to reply, before any draft is made (issue #4, ADR-0006).

The research model proposes features (ReplyFit: post_kind, brief_problem_span,
in_others_thread, is_vendor_post). This module, which is plain code:

1. checks the features against the post (the span must be in the post text;
   thread position comes from X metadata when X gives it);
2. sets every confidence input and computes the confidence score;
3. applies the hard skip rules and records the rule and its reason.

The research_post activity calls assess_reply_fit() on every adapter's
output, so no adapter can bypass it, and the Review workflow skips the post
before drafting when skip_reason is set.
"""

import html
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

from backend.config.confidence import (
    BRIEF_OVERLAP_FULL,
    BROADCAST_KINDS,
    CONFIDENCE_RULE_VERSION,
    IN_THREAD_ALLOWED_KINDS,
    MIN_SPAN_WORDS,
    POST_KIND_SCORES,
    POST_KINDS,
)
from backend.models import Evidence, Post, ReplyFit

# Common words that say nothing about topic. Only words of 4+ letters matter,
# because shorter words are dropped before this list is used.
_STOPWORDS = frozenset("""
    about above after again against also been before being below between both
    cant could didnt does doesnt doing done dont down during each even every
    from have having here hers into itll just know like make many more most
    much need only other ours over same should some such than that thats their
    them then there these they this those through very want were what when
    where which while will with would your yours youre anyone anybody someone
    something thing things really still using used folks people guys today
    anything everyone everything
""".split())

_QUOTE_MAP = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-",
})


def normalize_text(text: str) -> str:
    """NFKC, unescape HTML entities, straighten quotes, collapse spaces, casefold."""
    text = html.unescape(unicodedata.normalize("NFKC", text)).translate(_QUOTE_MAP)
    return " ".join(text.split()).casefold()


def check_span(span: Optional[str], post_text: str) -> str:
    """Return ok, missing, not_in_post or too_short for a proposed problem span."""
    if not isinstance(span, str) or not span.strip():
        return "missing"
    candidate = normalize_text(span).strip("\"' ")
    if not candidate:
        return "missing"
    if candidate not in normalize_text(post_text):
        return "not_in_post"
    if len(candidate.split()) < MIN_SPAN_WORDS:
        return "too_short"
    return "ok"


def thread_position(post: Post, model_value: Optional[bool]) -> tuple[Optional[bool], str]:
    """Whether the post is a reply in someone else's conversation, and the source.

    X metadata decides when it can: a post that starts its own conversation is
    not in another thread, and a reply to a different user is. A reply to the
    poster's own post (a self-thread) cannot be placed from the post alone, so
    the model's proposal is used, as it is when metadata is missing.
    """
    conversation_id = post.conversation_id or ""
    if conversation_id and conversation_id == post.id:
        return False, "metadata"
    if conversation_id and post.in_reply_to_user_id and post.in_reply_to_user_id != post.author.id:
        return True, "metadata"
    if model_value is None:
        return None, "missing"
    return model_value, "model"


def freshness(created_at: str, now: datetime) -> float:
    """1.0 under 24h, 0.5 under 48h, 0.25 under 7 days, else 0.1."""
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_hours = (now - created).total_seconds() / 3600
    if age_hours < 24:
        return 1.0
    if age_hours < 48:
        return 0.5
    if age_hours < 168:
        return 0.25
    return 0.1


def author_reach(followers: int) -> float:
    """log10(followers) / 6, capped at 1.0."""
    if followers <= 1:
        return 0.0
    return round(min(1.0, math.log10(followers) / 6.0), 4)


def _stems(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", normalize_text(text).replace("'", ""))
    return {w[:5] for w in words if len(w) >= 4 and w not in _STOPWORDS}


def brief_overlap(span: str, product_brief: str) -> float:
    """Share of the span's content words (5-letter stems) found in the brief.

    Half or more counts as full overlap (BRIEF_OVERLAP_FULL). This is a lexical check, so a post
    that names the problem in different words scores low here; the weight is
    set with that in mind.
    """
    span_stems = _stems(span)
    if not span_stems:
        return 0.0
    ratio = len(span_stems & _stems(product_brief)) / len(span_stems)
    return round(min(1.0, ratio / BRIEF_OVERLAP_FULL), 4)


def _label(kind: str) -> str:
    return kind[:1].upper() + kind[1:]


def assess_reply_fit(
    evidence: Evidence,
    post: Post,
    product_brief: str,
    now: Optional[datetime] = None,
) -> Evidence:
    """Check the proposed features, set confidence, and apply skip rules.

    Mutates and returns evidence: confidence_inputs and confidence_score are
    replaced (no adapter-typed number survives), reply_fit gets the code's
    checks, and skip_reason is set when a skip rule fires.
    """
    now = now or datetime.now(timezone.utc)
    fit = evidence.reply_fit or ReplyFit()
    fit.rule_version = CONFIDENCE_RULE_VERSION

    kind = fit.post_kind if fit.post_kind in POST_KINDS else None
    fit.span_check = check_span(fit.brief_problem_span, post.text)
    span_ok = fit.span_check == "ok"
    span = fit.brief_problem_span if span_ok else ""
    fit.in_others_thread_used, fit.thread_source = thread_position(post, fit.in_others_thread)

    has_material = any(m.passage and m.passage.strip() for m in evidence.founder_materials)

    if span_ok and "?" in span:
        asks_question = 1.0
    elif "?" in post.text:
        asks_question = 0.5
    else:
        asks_question = 0.0

    evidence.confidence_inputs = {
        "lookup_succeeded": 1.0,
        "replies_open": 1.0 if post.reply_settings == "everyone" else 0.0,
        "has_founder_material": 1.0 if has_material else 0.0,
        "freshness": freshness(post.created_at, now),
        "kind_score": POST_KIND_SCORES.get(kind, 0.0) if kind else 0.0,
        "asks_question": asks_question,
        "brief_overlap": brief_overlap(span, product_brief) if span_ok else 0.0,
        "author_reach": author_reach(post.author.followers_count),
        "direct_post": 0.0 if fit.in_others_thread_used is not False else 1.0,
    }
    evidence.confidence_score = evidence.compute_confidence()

    rule, reason = _skip_rule(fit, kind, span, has_material, post)
    fit.skip_rule = rule
    evidence.skip_reason = reason
    evidence.reply_fit = fit
    return evidence


def _skip_rule(
    fit: ReplyFit,
    kind: Optional[str],
    span: str,
    has_material: bool,
    post: Post,
) -> tuple[Optional[str], Optional[str]]:
    """Return (rule id, reason) for the first skip rule that fires, else (None, None)."""
    if post.reply_settings != "everyone":
        return "replies_limited", f"Replies are limited (reply setting: {post.reply_settings})"

    missing = []
    if kind is None:
        missing.append("a known post kind")
    if not isinstance(fit.is_vendor_post, bool):
        missing.append("whether this is a vendor post")
    if fit.in_others_thread_used is None:
        missing.append("whether the post is in someone else's thread")
    if missing:
        return "research_incomplete", f"Research did not say {', '.join(missing)}; not guessing"

    if fit.is_vendor_post:
        return "vendor_post", "Vendor post: the poster is promoting their own product"

    if kind in BROADCAST_KINDS and "?" not in span:
        return "broadcast_post", f"{_label(kind)} post with no question to answer"

    if fit.span_check != "ok":
        detail = {
            "missing": "",
            "not_in_post": " (the quoted span is not in the post)",
            "too_short": f" (the quoted span is under {MIN_SPAN_WORDS} words)",
        }.get(fit.span_check, "")
        return "no_problem_span", f"No part of the post shows a problem the brief solves{detail}"

    if fit.in_others_thread_used and kind not in IN_THREAD_ALLOWED_KINDS:
        return "others_thread", f"{_label(kind)} post inside someone else's conversation"

    if not has_material:
        return "no_founder_material", "The brief has no material that supports a reply"

    return None, None
