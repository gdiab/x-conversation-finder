"""Deterministic waits for Review workflow tests (issues #14, #20).

On the time-skipping test server, env.sleep(1) returns early only while
nothing is running; while activities run, virtual time follows real time, so
env.sleep(1) right after starting a Review is a 1 s real-time race against
research -> plan -> draft -> claim_gate -> store_card. Under load the chain
can take longer, and an update sent then is rejected as not ready (issue #20:
the nudge test that failed about 1 in 12 runs).
"""

import asyncio

# Hang guard only: the wait ends on workflow state, and this bound is hit only
# if the Review never stores its card (a real failure, not load).
WAIT_TIMEOUT_SEC = 60


async def wait_for_card_ready(handle) -> dict:
    """Return get_card once the Review has stored its card.

    get_card reports "in_progress": true until the workflow task that handled
    a store_card completion (issue #20): the pending card, or a skip card.
    For a pending card that task also sets the flag the approve, edit and
    nudge validators check, so an update sent after this returns is not
    rejected as not ready, and the stored row can be read. The query is
    re-sent every 50 ms; that sets only how soon the wait notices.
    """
    async def _poll() -> dict:
        while True:
            card = await query_card(handle)
            if not card.get("in_progress"):
                return card
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(_poll(), WAIT_TIMEOUT_SEC)


def rejection_message(handle, error) -> str:
    """An update rejection's message as the API reports it.

    issue #23: a text-free nudge's rejection reasons quote the redraft, so
    they are stored, not in the error; the API adds them
    (backend.api.stored_nudge_rejection).
    """
    from backend.api import stored_nudge_rejection
    from backend.workflows.review import NUDGE_REJECTED_ERROR

    cause = getattr(error, "cause", error)
    if getattr(cause, "type", None) == NUDGE_REJECTED_ERROR:
        return stored_nudge_rejection(handle.id, cause) or str(cause)
    return str(cause)


async def query_card(handle) -> dict:
    """The card as GET /api/reviews/{id} returns it.

    issue #23: a Review started with only a post id answers get_card with
    its state ("text_free": true); its content is on the stored card, and
    the API merges the two (store_activities.text_free_card). A Review whose
    input carries the post answers with the whole card, as before.
    """
    card = await handle.query("get_card")
    if card.get("text_free"):
        from backend.activities.store_activities import text_free_card

        card = text_free_card(handle.id, card)
    return card
