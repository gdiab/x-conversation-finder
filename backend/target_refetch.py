"""
Re-fetch a stored card's target post before the ADR-0006 eval replays it (issue #9).

ADR-0006 (after Outpost ADR-0086) keeps post text only while the post exists:
a 404 on a re-fetch deletes it. The Review workflow re-fetches at approve
time (backend/workflows/review.py). The baseline eval replays stored
evidence; before it uses a card it must call refetch_stored_card_target, so
a post deleted since the card was stored is removed rather than replayed.

Pending cards are not re-checked on a schedule (decided in issue #9): X cost
stays proportional to approvals and eval runs, and a pending Review
re-fetches again when the founder approves.
"""

from typing import Literal

from backend.activities.store_activities import (
    DeletePostTextInput,
    GetCardInput,
    _delete_post_text_sync,
    _get_card_sync,
)
from backend.budget import reserve_x_budget
from backend.config import get_settings
from backend.ports import XClientPort

RefetchOutcome = Literal["found", "removed", "no_card"]

# Budget reservation scope for eval re-fetches; with the card's workflow id and
# the caller's run key it makes the reservation idempotent per eval run.
EVAL_REFETCH_RUN_ID = "eval_refetch"


async def refetch_stored_card_target(
    x_client: XClientPort,
    workflow_id: str,
    run_key: str,
) -> RefetchOutcome:
    """Look up a stored card's target post and apply the removal duty on a 404.

    - "found": the post still exists; the card may be replayed.
    - "removed": the post is gone (now, or at an earlier re-fetch); the stored
      post text and every evidence copy of it are removed, and the card must
      not be replayed with post text.
    - "no_card": no stored card has this workflow id.

    Reserves one X read through backend.budget first (raises BudgetExceeded
    over the cap, never reports "removed"). Calling again with the same
    run_key on the same UTC day reuses the reservation, so a retried eval run
    is not charged twice by our ledger. A card already marked removed costs
    no read. The stored card's status is left as it is; a pending Review
    re-fetches again at approve and ends as skipped there.
    """
    card = _get_card_sync(GetCardInput(workflow_id=workflow_id))
    if card is None:
        return "no_card"
    if card["post"].get("text_removed"):
        return "removed"

    settings = get_settings()
    reserve_x_budget(
        reads_needed=1,
        cost_per_read=settings.x_post_read_cost,
        workflow_id=workflow_id,
        workflow_run_id=EVAL_REFETCH_RUN_ID,
        activity_id=run_key,
    )
    post = await x_client.lookup_post(card["post"]["id"])
    if post is not None:
        return "found"

    _delete_post_text_sync(
        DeletePostTextInput(
            workflow_id=workflow_id,
            idempotency_key=f"{workflow_id}:delete_text:eval:{run_key}",
        )
    )
    return "removed"
