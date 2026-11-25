"""Which draft mode a Review uses (issue #12; ADR-0006 amendment 2026-11-24, item 7).

`DRAFT_MODE=assembled|written` is the worker's setting. Written mode also
needs the watch's product name: the deterministic name trigger reads only the
"Product name" and "Also called" fields of the Start Watch form (George's
decision, 2026-11-24), so a draft cannot be checked without one.

A watch started before those fields existed has no product name. Its Reviews
stay assembled, with a note on the card, until the watch is stopped and
started again with a name.

The mode is resolved once per Review, inside the plan_reply activity, so the
result is recorded in workflow history: the Review keeps its mode through
redrafts, nudges and edits, and replays the same way after the setting
changes.
"""

from typing import Optional, Tuple

from backend.models.written import DRAFT_MODE_ASSEMBLED, DRAFT_MODE_WRITTEN

NO_PRODUCT_NAME_NOTE = (
    "Written drafts are switched on (DRAFT_MODE=written), but this watch was started without a "
    "product name, so this reply was assembled from fixed phrases. Stop the watch and start it "
    "again with a product name to get written drafts."
)


def resolve_draft_mode(setting: str, product_name: Optional[str]) -> Tuple[str, Optional[str]]:
    """Return (mode, note). The note says why written mode was asked for but not used."""
    if setting != DRAFT_MODE_WRITTEN:
        return DRAFT_MODE_ASSEMBLED, None
    if not (product_name or "").strip():
        return DRAFT_MODE_ASSEMBLED, NO_PRODUCT_NAME_NOTE
    return DRAFT_MODE_WRITTEN, None
