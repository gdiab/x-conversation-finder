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

The eval replays past cards only. A card whose Review workflow is still
running is left alone: the workflow holds its own copy of the evidence and
re-fetches at approve, so a removal here would not reach it. Whether it runs
is asked of Temporal (temporal_review_is_running), not read from the stored
status: Reviews skipped before issue #9 ended with their card still "pending".
"""

import uuid
from typing import Awaitable, Callable, Literal

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from backend.activities.store_activities import (
    DeletePostTextInput,
    GetCardInput,
    _delete_post_text_sync,
    _get_card_sync,
)
from backend.activities.x_activities import x_billed_nothing
from backend.budget import release_x_reservation, reserve_x_budget
from backend.config import get_settings
from backend.ports import XClientPort

RefetchOutcome = Literal["found", "removed", "live", "no_card"]

# Answers whether the Review workflow with this id is still running
ReviewIsRunning = Callable[[str], Awaitable[bool]]


def temporal_review_is_running(client: Client) -> ReviewIsRunning:
    """A ReviewIsRunning check that asks Temporal for the workflow's status.

    A workflow Temporal no longer knows (its history passed the namespace's
    retention) is not running. Other errors propagate: the eval must not
    guess.
    """

    async def is_running(workflow_id: str) -> bool:
        try:
            description = await client.get_workflow_handle(workflow_id).describe()
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                return False
            raise
        return description.status == WorkflowExecutionStatus.RUNNING

    return is_running

# Budget reservation scope for eval re-fetches (with the card's workflow id,
# the caller's run key and a per-call id)
EVAL_REFETCH_RUN_ID = "eval_refetch"


async def refetch_stored_card_target(
    x_client: XClientPort,
    workflow_id: str,
    run_key: str,
    *,
    review_is_running: ReviewIsRunning,
) -> RefetchOutcome:
    """Look up a stored card's target post and apply the removal duty on a 404.

    - "found": the post still exists; the card may be replayed.
    - "removed": the post is gone (now, or at an earlier re-fetch); the stored
      post text and every evidence copy of it are removed, and the card must
      not be replayed with post text.
    - "live": the card's Review workflow is still running (per
      review_is_running, usually temporal_review_is_running); nothing is read
      or changed, and the card must not be replayed. Its workflow re-fetches
      the post at approve.
    - "no_card": no stored card has this workflow id.

    Reserves one X read through backend.budget before each lookup (raises
    BudgetExceeded over the cap, never reports "removed"). As for the
    lookup_post activity (issue #8), every call that may have been billed is
    counted, including a retry; a request X refused, and a lookup that found
    the post gone (X returned no post object, issue #17), are released. A card
    already marked removed costs no read. The stored card's status is left
    as it is.
    """
    card = _get_card_sync(GetCardInput(workflow_id=workflow_id))
    if card is None:
        return "no_card"
    if card["post"].get("text_removed"):
        return "removed"
    if await review_is_running(workflow_id):
        return "live"

    settings = get_settings()
    call_id = f"{run_key}:{uuid.uuid4().hex}"
    _, reservation_date = reserve_x_budget(
        reads_needed=1,
        cost_per_read=settings.x_post_read_cost,
        workflow_id=workflow_id,
        workflow_run_id=EVAL_REFETCH_RUN_ID,
        activity_id=call_id,
    )
    try:
        post = await x_client.lookup_post(card["post"]["id"])
    except Exception as e:
        if x_billed_nothing(e):
            release_x_reservation(
                reserved_reads=1,
                reservation_date=reservation_date,
                workflow_id=workflow_id,
                workflow_run_id=EVAL_REFETCH_RUN_ID,
                activity_id=call_id,
                cost_per_read=settings.x_post_read_cost,
            )
        raise
    if post is not None:
        return "found"

    _delete_post_text_sync(
        DeletePostTextInput(
            workflow_id=workflow_id,
            idempotency_key=f"{workflow_id}:delete_text:eval:{run_key}",
            post_id=card["post"]["id"],
        )
    )
    # Issue #17: X returned no post object for the gone post and billed
    # nothing. Released after the removal, so a failed release cannot keep
    # the text; it then stays counted (the safe direction).
    release_x_reservation(
        reserved_reads=1,
        reservation_date=reservation_date,
        workflow_id=workflow_id,
        workflow_run_id=EVAL_REFETCH_RUN_ID,
        activity_id=call_id,
        cost_per_read=settings.x_post_read_cost,
    )
    return "removed"
