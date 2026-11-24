"""Decide whether to reply, before any draft is made (issue #4, ADR-0006).

The research model proposes features (ReplyFit: post_kind, brief_problem_span,
in_others_thread, is_vendor_post). This module, which is plain code:

1. checks the features against the post and the brief: the span must be an
   exact substring of the post with enough content words, at least one of them
   shared with the brief; founder passages must appear in the brief; thread
   position comes from X metadata when X gives it;
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
    MIN_SPAN_CONTENT_WORDS,
    POST_KIND_SCORES,
    POST_KINDS,
)
from backend.models import Evidence, FounderMaterial, Post, ReplyFit

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
    """NFKC, unescape HTML entities, straighten quotes, collapse spaces, casefold.

    Used only to compare words for the brief-overlap feature, never to decide
    whether a quote is in the post or the brief.
    """
    text = html.unescape(unicodedata.normalize("NFKC", text)).translate(_QUOTE_MAP)
    return " ".join(text.split()).casefold()


_WRAPPING_QUOTES = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019"}


def match_span(span: Optional[str], post_text: str) -> tuple[str, Optional[str]]:
    """Check a proposed problem span; return (check, the matched post text).

    check is ok, missing, not_in_post or too_short. The span must be an exact
    substring of the post: only surrounding whitespace and one pair of wrapping
    quotes are dropped. On ok, the second value is the text as it appears in
    the post, which is what code uses from then on.
    """
    if not isinstance(span, str) or not span.strip():
        return "missing", None
    candidate = span.strip()
    candidates = [candidate]
    if len(candidate) > 2 and _WRAPPING_QUOTES.get(candidate[0]) == candidate[-1]:
        candidates.append(candidate[1:-1].strip())
    match = next((c for c in candidates if c and c in post_text), None)
    if match is None:
        return "not_in_post", None
    if len(_stems(match)) < MIN_SPAN_CONTENT_WORDS:
        return "too_short", None
    return "ok", match


def check_span(span: Optional[str], post_text: str) -> str:
    """The check result of match_span alone."""
    return match_span(span, post_text)[0]


def _collapse_spaces(text: str) -> str:
    return " ".join(text.split())


def verify_materials(
    materials: list[FounderMaterial], product_brief: str
) -> tuple[list[FounderMaterial], int]:
    """Keep founder passages that appear in the brief; return (kept, dropped count).

    The comparison ignores only differences in whitespace (a passage copied
    across a line break). A passage the model wrote or reworded is dropped, so
    the draft and the claim gate never treat it as the founder's own words.
    """
    brief = _collapse_spaces(product_brief)
    kept = [
        m for m in materials
        if isinstance(m.passage, str) and m.passage.strip()
        and _collapse_spaces(m.passage) in brief
    ]
    return kept, len(materials) - len(kept)


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
    fit.span_check, matched = match_span(fit.brief_problem_span, post.text)
    overlap = brief_overlap(matched, product_brief) if matched else 0.0
    if fit.span_check == "ok" and overlap == 0.0:
        fit.span_check = "off_brief"
    span_ok = fit.span_check == "ok"
    span = matched if span_ok else ""
    # A failed span counts as null; keep the proposal apart so it is never
    # shown as a quotation from the post.
    if not span_ok and isinstance(fit.brief_problem_span, str) and fit.brief_problem_span.strip():
        fit.rejected_span = fit.brief_problem_span
    fit.brief_problem_span = span or None
    fit.in_others_thread_used, fit.thread_source = thread_position(post, fit.in_others_thread)

    evidence.founder_materials, fit.materials_dropped = verify_materials(
        evidence.founder_materials, product_brief
    )
    has_material = bool(evidence.founder_materials)

    if span_ok and _has_question(span):
        asks_question = 1.0
    elif _has_question(post.text):
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
        "brief_overlap": overlap if span_ok else 0.0,
        "author_reach": author_reach(post.author.followers_count),
        "direct_post": 0.0 if fit.in_others_thread_used is not False else 1.0,
    }
    evidence.confidence_score = evidence.compute_confidence()

    rule, reason = _skip_rule(fit, kind, has_material, post)
    fit.skip_rule = rule
    evidence.skip_reason = reason
    evidence.reply_fit = fit
    return evidence


def _has_question(text: str) -> bool:
    """Whether the text has a question mark (ASCII or full-width)."""
    return "?" in text or "\uff1f" in text


def _skip_rule(
    fit: ReplyFit,
    kind: Optional[str],
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

    if kind in BROADCAST_KINDS and not _has_question(post.text):
        return "broadcast_post", f"{_label(kind)} post with no question to answer"

    if fit.span_check != "ok":
        detail = {
            "missing": "",
            "not_in_post": " (the quoted span is not in the post)",
            "too_short": f" (the quoted span has fewer than {MIN_SPAN_CONTENT_WORDS} content words)",
            "off_brief": " (the quoted span shares no words with the brief)",
        }.get(fit.span_check, "")
        return "no_problem_span", f"No part of the post shows a problem the brief solves{detail}"

    if fit.in_others_thread_used and kind not in IN_THREAD_ALLOWED_KINDS:
        return "others_thread", f"{_label(kind)} post inside someone else's conversation"

    if not has_material:
        if fit.materials_dropped:
            return "no_founder_material", (
                "No founder material: the research quoted passages that are not in the brief"
            )
        return "no_founder_material", "The brief has no material that supports a reply"

    return None, None
