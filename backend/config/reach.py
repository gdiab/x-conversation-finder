"""Reach rule: how many people a reply on this post could reach (issue #49).

Reach is separate from confidence (backend/config/confidence.py):
- confidence answers "is a reply welcome and on-topic?" and decides whether
  a post gets a card;
- reach answers "how many people will see it?" and only orders and
  highlights pending cards. It never changes confidence, the threshold or
  which posts get a card.

As with confidence (ADR-0006), code sets the number from the post metrics X
returned with the searched post (no extra X read). Each change gets a new
REACH_RULE_VERSION date, and the version and the inputs are stored with
every card (review_cards.reach), so a card always says which rule scored it.

Known limit: the counts are from the moment of search. A post found minutes
after it was written has low counts and a low reach, however far it later
spreads. Re-fetching the counts would cost X reads and is out of scope.

History:
- 2026-11-27 (v1, this file): weighted sum of an engagement score and an
  audience score, both on log scales. Scales and the highlight level were
  set from the 203 cards in the local database on 2026-11-27 (see below).
"""

REACH_RULE_VERSION = "2026-11-27"

# Engagement = likes + ENGAGEMENT_ACTION_WEIGHT * (replies + reposts + quotes).
# A reply, repost or quote takes more effort than a like, and each one puts
# the post in front of more people (a repost or quote in the reposter's
# timeline, a reply in the conversation where our reply would also appear).
ENGAGEMENT_ACTION_WEIGHT = 2

# engagement_score = log10(1 + engagement) / log10(1 + ENGAGEMENT_FULL),
# capped at 1.0. Counts are heavy-tailed: on the 2026-11-27 cards the median
# engagement was 2, the 90th percentile 9, the 99th 58 and the largest 172,
# and 42% of posts had none. A log scale separates 0, 2, 10 and 50, and
# 100 or more counts as full.
ENGAGEMENT_FULL = 100

# audience_score = (log10(followers) - log10(FLOOR)) / (log10(FULL) - log10(FLOOR)),
# clamped to [0, 1]. Median followers on those cards were 400, the 75th
# percentile about 3,100 and the 90th about 49,000; a few posts came from
# accounts with 9.4M. At or below 100 followers the audience counts as 0;
# 1M or more counts as full, so a huge account cannot score more than
# REACH_WEIGHTS["audience_score"] on followers alone.
FOLLOWERS_FLOOR = 100
FOLLOWERS_FULL = 1_000_000

# Weights sum to 1.0. Engagement weighs more: it is evidence that this post
# is being seen now, while followers are only the author's potential
# audience (a 9.4M-follower account's reply with 3 likes reaches few people).
REACH_WEIGHTS = {
    "engagement_score": 0.65,
    "audience_score": 0.35,
}

# Cards with reach at or above this level get a highlight in the review
# queue. On the 2026-11-27 cards 8 of 203 (4%) reached 0.6: about 18+
# engagement from a 15,000+ follower account, or about 70+ engagement from
# any account. A 9.4M-follower account with 3 engagements scores about 0.55
# and is not highlighted.
REACH_HIGHLIGHT_LEVEL = 0.6

# Pending queue order: queue_priority = confidence + REACH_SORT_WEIGHT * reach,
# highest first. A card with full reach is placed above a card with no reach
# that is up to 0.2 more confident; cards that pass the confidence threshold
# (0.55) spread from about 0.55 to 0.98, so confidence still leads. A card
# with no reach score (no stored metrics) sorts by confidence alone.
REACH_SORT_WEIGHT = 0.2
