"""Skip rule `needs_written_drafts` (issue #5; George's decision, 2026-11-24).

Under assembled drafts (ADR-0006, 2026-11-21 amendment) a reply without a
product claim is made of fixed phrases only. Fixed phrases cannot answer a
question or share our experience, so a valid plan of type answer_question
or share_experience with mention_product=false cannot be carried out. Such a
post is skipped with a recorded reason instead of producing a hollow reply.

Written drafts (ADR-0006 amendment 2026-11-24, accepted in PR #3) can carry
these replies, so the rule is off for a Review in written mode (issue #12):
the plan_reply activity passes enabled=False when the Review's draft mode is
"written" (backend/planning/draft_mode.py). For assembled Reviews
NEEDS_WRITTEN_DRAFTS_ENABLED applies; it is read at call time.

The rule runs inside the plan_reply activity, so its verdict is recorded in
workflow history; switching it off later does not change how in-flight
Review workflows replay.
"""

from typing import Optional

from backend.models.reply_plan import ReplyPlan

NEEDS_WRITTEN_DRAFTS_RULE = "needs_written_drafts"

# On for Reviews whose draft mode is "assembled".
NEEDS_WRITTEN_DRAFTS_ENABLED = True

# Planned reply types that need free text when the product is not mentioned.
NEEDS_WRITTEN_DRAFTS_REPLY_TYPES = {
    "answer_question": "answer the question",
    "share_experience": "share our experience",
}


def needs_written_drafts_skip_reason(
    plan: ReplyPlan, enabled: Optional[bool] = None
) -> Optional[str]:
    """Return the skip reason when the rule applies to a valid plan, else None.

    `enabled` defaults to NEEDS_WRITTEN_DRAFTS_ENABLED, read at call time.
    """
    if enabled is None:
        enabled = NEEDS_WRITTEN_DRAFTS_ENABLED
    if not enabled or plan.mention_product is not False:
        return None
    action = NEEDS_WRITTEN_DRAFTS_REPLY_TYPES.get(plan.reply_type)
    if action is None:
        return None
    return (
        f"Reply needs written drafts: the plan is to {action} without mentioning the product, "
        "and assembled drafts can only use fixed phrases for that, so they cannot do it yet "
        "(ADR-0006 amendment 2026-11-24, written drafts)"
    )
