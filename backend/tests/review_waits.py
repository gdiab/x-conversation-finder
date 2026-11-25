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
            card = await handle.query("get_card")
            if not card.get("in_progress"):
                return card
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(_poll(), WAIT_TIMEOUT_SEC)
