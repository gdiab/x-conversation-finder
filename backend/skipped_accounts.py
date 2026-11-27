"""Skipped accounts (issue #48): X handles whose posts no watch reviews.

One list for the app, edited in the review UI (GET, POST to add, DELETE
/<handle> to remove under /api/settings/skipped-accounts), stored in the
global_settings table. The UI sends one change at a time, never the whole
list, so a delayed or retried request cannot undo another edit. Every
watch reads it in its search_recent activity at each search, so an edit
applies at each watch's next search without a restart; the activity's result
is recorded in history, so replay does not read the list again.

Two layers, both in the activity:
1. `query_with_skips` adds `-from:<handle>` to the X query for each handle
   while the query stays within X's 512-character recent-search limit, so X
   does not return (or bill) those posts.
2. `drop_skipped_authors` drops any returned post written by a listed handle
   (case-insensitive), which covers the handles left out of the query.

Only posts written by a listed account are skipped. A post that mentions one
("@grok is this true?") is still reviewed (George's decision, 2026-11-27).
"""

import json
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Iterable, List, Sequence, Tuple

from backend.database import GlobalSettingsDB, get_db
from backend.models import Post

logger = logging.getLogger(__name__)

SETTING_KEY = "skipped_accounts"

# X's recent search accepts a query of at most 512 characters
X_QUERY_MAX_CHARS = 512

# More handles than could ever fit in the query; the rest are only filtered
MAX_SKIPPED_ACCOUNTS = 100

# An X handle: 1-15 letters, digits or underscores
HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def normalize_handles(raw: Iterable[str]) -> List[str]:
    """Trim each handle, drop a leading "@", lowercase it, and drop blanks
    and repeats, keeping the founder's order. Raises ValueError for a value
    that is not an X handle, or for more than MAX_SKIPPED_ACCOUNTS handles."""
    handles: List[str] = []
    for value in raw:
        handle = value.strip()
        if handle.startswith("@"):
            handle = handle[1:].strip()
        if not handle:
            continue
        if not HANDLE_PATTERN.match(handle):
            raise ValueError(
                f'"{value.strip()}" is not an X handle (1-15 letters, digits or underscores)'
            )
        handle = handle.lower()
        if handle not in handles:
            handles.append(handle)
    if len(handles) > MAX_SKIPPED_ACCOUNTS:
        raise ValueError(f"At most {MAX_SKIPPED_ACCOUNTS} skipped accounts")
    return handles


def get_skipped_accounts() -> List[str]:
    """The stored list, normalized; empty when none was saved. A stored value
    that cannot be read is logged and treated as empty: the watch searches
    as it did before the list existed, instead of failing."""
    db = get_db()
    try:
        row = db.query(GlobalSettingsDB).filter(GlobalSettingsDB.key == SETTING_KEY).first()
        if row is None:
            return []
        try:
            stored = json.loads(row.value)
            if not isinstance(stored, list) or not all(isinstance(h, str) for h in stored):
                raise ValueError("not a list of strings")
            return normalize_handles(stored)
        except ValueError as e:
            logger.error(f"Stored skipped accounts are unreadable ({e}); skipping no accounts")
            return []
    finally:
        db.close()


# The API's request threads change the list read-modify-write; the worker
# only reads it
_edit_lock = threading.Lock()


def add_skipped_accounts(raw: Iterable[str]) -> List[str]:
    """Add handles to the end of the stored list (one already on it stays
    where it is); returns the list. Raises ValueError as normalize_handles."""
    with _edit_lock:
        return set_skipped_accounts([*get_skipped_accounts(), *normalize_handles(raw)])


def remove_skipped_accounts(raw: Iterable[str]) -> List[str]:
    """Remove handles from the stored list (one not on it is ignored);
    returns the list. Raises ValueError as normalize_handles."""
    removed = set(normalize_handles(raw))
    with _edit_lock:
        return set_skipped_accounts([h for h in get_skipped_accounts() if h not in removed])


def set_skipped_accounts(raw: Iterable[str]) -> List[str]:
    """Normalize and store the list (replacing the stored one); returns it."""
    handles = normalize_handles(raw)
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    try:
        row = db.query(GlobalSettingsDB).filter(GlobalSettingsDB.key == SETTING_KEY).first()
        if row is None:
            db.add(GlobalSettingsDB(key=SETTING_KEY, value=json.dumps(handles), last_updated=now))
        else:
            row.value = json.dumps(handles)
            row.last_updated = now
        db.commit()
    finally:
        db.close()
    return handles


def query_with_skips(
    query: str, handles: Sequence[str], max_chars: int = X_QUERY_MAX_CHARS
) -> Tuple[str, List[str]]:
    """The query with `-from:<handle>` for each handle that fits in
    max_chars, and the handles left out.

    The founder's query is put in parentheses first, so an OR in it cannot
    bind to the first operator ("a OR b -from:grok" is "a OR (b -from:grok)"
    on X). A handle that does not fit is left out and the next, shorter one
    is still tried. Without handles, or if not even one fits, the query is
    returned unchanged.
    """
    if not handles:
        return query, []
    built = f"({query.strip()})"
    added: List[str] = []
    left_out: List[str] = []
    for handle in handles:
        operator = f" -from:{handle}"
        if len(built) + len(operator) <= max_chars:
            built += operator
            added.append(handle)
        else:
            left_out.append(handle)
    if not added:
        return query, left_out
    return built, left_out


def drop_skipped_authors(posts: Sequence[Post], handles: Sequence[str]) -> List[Post]:
    """The posts not written by a listed handle (case-insensitive). Only the
    author counts: a post that mentions a listed handle is kept."""
    skipped = {h.casefold() for h in handles}
    if not skipped:
        return list(posts)
    return [p for p in posts if p.author.username.casefold() not in skipped]
