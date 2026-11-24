"""Confidence rule and "should we reply?" rules (ADR-0006, issue #4).

ADR-0006: the model proposes features; code sets the number and the threshold.
The weights and threshold are guesses. Each change gets a new
CONFIDENCE_RULE_VERSION date, and the version is stored with every card's
confidence inputs, so a card always says which rule scored it.
Revisit after the first 50 real cards.

History:
- 2026-11-21 (v1): additive weighted sum, threshold 0.6. lookup_succeeded,
  replies_open, freshness and has_founder_material were 1.0 for almost every
  searched post and summed to 0.65, so the threshold filtered nothing;
  topic_match_strength was a number the model typed.
- 2026-11-24 (v2, this file): those always-on features became gates or a
  multiplier, and the additive part uses only features code measures or
  scores from a model-proposed category. Threshold 0.55.
"""

CONFIDENCE_RULE_VERSION = "2026-11-24"

# Set 2026-11-24: threshold for the v2 rule. On the synthetic fixture
# (backend/tests/fixtures/reply_fit_posts.json) the questions and problem
# descriptions that pass the skip rules score 0.67-0.98 (the lowest is a
# six-day-old question), while an on-topic opinion and a day-old experience
# post phrased in words the brief does not use score about 0.45. 0.55 sits in
# that gap with about 0.1 of margin on each side.
CONFIDENCE_THRESHOLD = 0.55

# Gates: if any is 0 the confidence is 0. Each one also has a skip rule with
# its own reason (backend/gate/reply_fit.py), so a gated post never reaches
# the threshold check.
CONFIDENCE_GATES = ("lookup_succeeded", "replies_open", "has_founder_material")

# Freshness discounts the score but cannot raise it:
# multiplier = FRESHNESS_FLOOR + (1 - FRESHNESS_FLOOR) * freshness.
# freshness is 1.0 (<24h), 0.5 (<48h), 0.25 (<7d), 0.1 (older).
FRESHNESS_FLOOR = 0.6

# Additive part. Weights sum to 1.0; every feature is in [0, 1].
CONFIDENCE_WEIGHTS = {
    # Score of the model-proposed post_kind (POST_KIND_SCORES below).
    "kind_score": 0.40,
    # 1.0 if the verified problem span is itself a question, 0.5 if the post
    # has a question elsewhere, else 0. Code reads this from the post text.
    "asks_question": 0.15,
    # Share of the span's content words that also appear in the brief,
    # scaled so half or more counts as 1.0. Code computes it.
    "brief_overlap": 0.25,
    # log10(followers) / 6, capped at 1.0. Small: reach says little about
    # whether a reply is welcome.
    "author_reach": 0.05,
    # 1.0 for a top-level post or the poster's own thread, 0.0 for a reply in
    # someone else's conversation.
    "direct_post": 0.15,
}

# Model-proposed category -> score. Broadcast kinds score 0; the skip rules
# usually stop them first.
POST_KIND_SCORES = {
    "question": 1.0,
    "problem": 0.9,
    "experience": 0.7,
    "opinion": 0.2,
    "announcement": 0.0,
    "promo": 0.0,
    "roundup": 0.0,
    "news": 0.0,
}

POST_KINDS = tuple(POST_KIND_SCORES)

# Kinds that broadcast rather than converse. Skipped unless the verified
# problem span is a question.
BROADCAST_KINDS = ("announcement", "promo", "roundup", "news")

# Kinds that may be answered inside someone else's conversation.
IN_THREAD_ALLOWED_KINDS = ("question", "problem")

# The problem span must hold at least this many content words (4+ letters,
# not a stopword), so a fragment such as "How do you" or "help" cannot pass
# the substring check. It must also share at least one content word with the
# brief ("off_brief" otherwise).
MIN_SPAN_CONTENT_WORDS = 2

# brief_overlap: ratio of matched content words that counts as full overlap.
BRIEF_OVERLAP_FULL = 0.5
