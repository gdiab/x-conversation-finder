"""Post text kept out of Temporal history (issue #23, George's option A).

Workflows carry only ids: the post id, a Review's document slots, digests
and counts. The text itself (the post, the model's evidence, drafts, check
results, a founder's edit or nudge) lives in SQLite, where the removal duty
(ADR-0006, following ADR-0086) can delete it:

- stored_posts: the posts a TopicWatch search found, by post id;
- review_docs: a Review's working documents, by (workflow_id, slot).

Documents are stored in the JSON shape Temporal's data converter gives the
same dataclass, so an activity reads back exactly what it would have received
as a payload.

A removal (delete_post_text) records the post as removed in stored_posts and
deletes every document of the post. Reads and writes after that raise
PostRemoved, the same error type the Review workflow already handles.
"""

import functools
import hashlib
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional, Type, TypeVar

from sqlalchemy import text as sql_text
from sqlalchemy.exc import SQLAlchemyError
from temporalio.converter import DataConverter, value_to_type
from temporalio.exceptions import ApplicationError

from backend.database import ReviewDocDB, StoredPostDB, get_db
from backend.models import Post

T = TypeVar("T")
logger = logging.getLogger(__name__)
DATABASE_ERROR_TYPE = "DatabaseError"


@contextmanager
def text_safe_db_errors():
    """Replace a database error with one that carries no data.

    SQLAlchemy puts a failed statement's parameters in its message (the
    post, a draft, a card). An activity failure's message is recorded in
    Temporal history, so the original goes to the worker log only. The
    replacement is retryable, as the database error was.
    """
    try:
        yield
    except SQLAlchemyError as e:
        logger.exception("Database error (issue #23: details kept out of workflow history)")
        raise ApplicationError(
            f"Database error ({type(e).__name__}); the details are in the worker log",
            type=DATABASE_ERROR_TYPE,
        ) from None


@contextmanager
def text_safe_errors():
    """Any error but our own ApplicationErrors, without its message.

    A text-free activity sends the post, a draft or a founder's text to a
    model or an API; a library's exception message can echo that request,
    and an activity failure's message is recorded in Temporal history. The
    original goes to the worker log; the replacement keeps the exception's
    type name, so retries behave as before. ApplicationErrors are raised by
    this code base with code-written messages and pass through.
    """
    try:
        yield
    except ApplicationError:
        raise
    except SQLAlchemyError as e:
        logger.exception("Database error (issue #23: details kept out of workflow history)")
        raise ApplicationError(
            f"Database error ({type(e).__name__}); the details are in the worker log",
            type=DATABASE_ERROR_TYPE,
        ) from None
    except Exception as e:
        logger.exception("Activity error (issue #23: details kept out of workflow history)")
        raise ApplicationError(
            f"{type(e).__name__}; the details are in the worker log", type=type(e).__name__
        ) from None


def text_free_activity(is_text_free):
    """Decorate an activity: when `is_text_free(input)`, its errors reach
    history through text_safe_errors. Put it under @activity.defn."""
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(*args):
            if not is_text_free(args[-1]):
                return await fn(*args)
            with text_safe_errors():
                return await fn(*args)
        return wrapper
    return decorate


def _text_safe(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with text_safe_db_errors():
            return fn(*args, **kwargs)
    return wrapper

# The error type the Review workflow maps to "post removed" (review.POST_REMOVED_ERROR)
POST_REMOVED_ERROR_TYPE = "PostRemoved"
# A document a workflow names is missing, and no removal explains it
DOC_MISSING_ERROR_TYPE = "ReviewDocMissing"

# Slot names. Each is fixed by the workflow (from its own keys), so replaying
# a history names the same slots.
EVIDENCE_SLOT = "evidence"  # the Review's current evidence (research, plan, accepted draft)


def founder_text_slot(kind: str, client_request_id: str, digest: str) -> str:
    """Where the API stores a founder's edit or nudge text ("edit" or
    "nudge"). Named by the text's digest too, so a retry of the request with
    other text cannot replace the text an update is already checking."""
    return f"founder:{kind}:{client_request_id}:{digest}"


def drafted_slot(slot: str) -> str:
    """The draft as draft_reply stored it, before the claim gate rewrote it
    (the gate reads this copy, so a retried gate sees the same input)."""
    return f"{slot}#drafted"


def failures_slot(slot: str) -> str:
    """The (sentence, reason) failures of the draft in `slot`."""
    return f"{slot}#failures"


def check_slot(kind: str, digest: str) -> str:
    """The written check of a text, by kind ("draft" or "edit") and digest."""
    return f"check:{kind}:{digest}"


def edit_results_slot(client_request_id: str) -> str:
    """The assembled re-gate results of a founder edit."""
    return f"edit_results:{client_request_id}"


def text_digest(text: Optional[str]) -> str:
    """The SHA-256 of a text; what the workflow holds instead of the text.

    Also what approve's expected_draft names (issue #15): the API hashes the
    draft text the founder saw. Deterministic and pure, so the workflow can
    compute it in a validator on a text it still holds (old histories).
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def post_removed_error(what: str) -> ApplicationError:
    return ApplicationError(
        f"Post removed: the target post was deleted or is no longer visible on X; {what}",
        type=POST_REMOVED_ERROR_TYPE,
        non_retryable=True,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_json(value: Any) -> Any:
    """The JSON value Temporal's default converter makes of `value`."""
    payload = DataConverter.default.payload_converter.to_payloads([value])[0]
    return json.loads(payload.data)


def from_json(data: Any, type_hint: Type[T]) -> T:
    """Decode a stored JSON value as Temporal decodes an activity payload."""
    return value_to_type(type_hint, data)


# ---------- removal state ----------

def _post_removed(db, post_id: str) -> bool:
    """Whether the post's removal is recorded: on its stored post, or on any
    stored card of it (the eval re-fetch and other accounts' Reviews record
    it on cards)."""
    from backend.activities.store_activities import _post_removal_recorded

    stored = db.get(StoredPostDB, post_id)
    if stored is not None and (stored.post_data or {}).get("text_removed"):
        return True
    return _post_removal_recorded(db, post_id)


@_text_safe
def post_removed(post_id: str) -> bool:
    db = get_db()
    try:
        return _post_removed(db, post_id)
    finally:
        db.close()


# ---------- posts ----------

@_text_safe
def store_posts(posts: Iterable[Post]) -> List[str]:
    """Store the posts a search found; return their ids in order.

    A post already stored is refreshed. A post whose removal is recorded is
    stored without its text (as a removed card keeps its post), so a later
    search cannot bring the text back and its Review can still store a
    "post removed" card.
    """
    from backend.activities.store_activities import POST_TEXT_REMOVED

    ids = []
    db = get_db()
    try:
        db.execute(sql_text("BEGIN IMMEDIATE"))
        for post in posts:
            ids.append(post.id)
            data = to_json(post)
            if _post_removed(db, post.id):
                data.update({"text": POST_TEXT_REMOVED, "text_removed": True})
            db.merge(StoredPostDB(post_id=post.id, post_data=data, stored_at=_now()))
        db.commit()
        return ids
    finally:
        db.close()


@_text_safe
def load_post(post_id: str, what: str = "not reading it") -> Post:
    """The stored post; raises PostRemoved once its removal is recorded."""
    db = get_db()
    try:
        if _post_removed(db, post_id):
            raise post_removed_error(what)
        row = db.get(StoredPostDB, post_id)
        if row is None:
            raise ApplicationError(
                f"Post {post_id} is not stored; a Review started without a TopicWatch search must pass the post",
                type=DOC_MISSING_ERROR_TYPE,
                non_retryable=True,
            )
        return from_json(row.post_data, Post)
    finally:
        db.close()


def stored_post_data(db, post_id: str) -> Optional[dict]:
    row = db.get(StoredPostDB, post_id)
    return dict(row.post_data) if row is not None else None


# ---------- Review documents ----------

def _load_row(db, workflow_id: str, post_id: str, slot: str, what: str):
    row = db.get(ReviewDocDB, (workflow_id, slot))
    if row is None:
        if _post_removed(db, post_id):
            raise post_removed_error(what)
        raise ApplicationError(
            f"Review document {slot!r} of {workflow_id} is missing",
            type=DOC_MISSING_ERROR_TYPE,
            non_retryable=True,
        )
    return row


@_text_safe
def load_doc(workflow_id: str, post_id: str, slot: str, type_hint: Type[T], what: str = "not reading it") -> T:
    """A Review document; PostRemoved once the post's removal is recorded."""
    db = get_db()
    try:
        if _post_removed(db, post_id):
            raise post_removed_error(what)
        return from_json(_load_row(db, workflow_id, post_id, slot, what).data, type_hint)
    finally:
        db.close()


@_text_safe
def find_doc(workflow_id: str, post_id: str, slot: str, type_hint: Type[T]) -> Optional[T]:
    """A Review document, or None if there is none (or the post was removed)."""
    db = get_db()
    try:
        if _post_removed(db, post_id):
            return None
        row = db.get(ReviewDocDB, (workflow_id, slot))
        return from_json(row.data, type_hint) if row is not None else None
    finally:
        db.close()


def current_writer() -> Optional[tuple]:
    """(workflow run id, activity id, attempt) of the running activity, or
    None outside one (the API) or when the id is not a number."""
    from temporalio import activity

    try:
        info = activity.info()
    except RuntimeError:
        return None
    try:
        return info.workflow_run_id, int(info.activity_id), int(info.attempt)
    except (TypeError, ValueError):
        return None


def written_later(row, writer: Optional[tuple]) -> bool:
    """Whether `row` was written, in the same workflow run, by a later step
    or a later attempt of the same step than `writer`: then `writer` is a
    late attempt (its retry has finished and the workflow moved on), and its
    write is dropped. A row from another run never blocks a write."""
    if row is None or writer is None or row.writer_seq is None:
        return False
    if row.writer_run != writer[0]:
        return False
    return (row.writer_seq, row.writer_attempt or 0) > (writer[1], writer[2])


def _writer_fields(writer: Optional[tuple]) -> dict:
    if writer is None:
        return {"writer_run": None, "writer_seq": None, "writer_attempt": None}
    return {"writer_run": writer[0], "writer_seq": writer[1], "writer_attempt": writer[2]}


# Which run of a workflow id owns its documents: {"owner": run id,
# "retired": [earlier run ids]}. Written by each run's first activity.
RUN_SLOT = "#run"


def run_superseded(db, workflow_id: str) -> bool:
    """Whether the running activity's run was replaced by a later run of
    the same workflow id: then it writes nothing (cards included)."""
    return _superseded(db, workflow_id, current_writer())


def _superseded(db, workflow_id: str, writer: Optional[tuple]) -> bool:
    """Whether `writer` belongs to a run that a later run of the same
    workflow id has replaced (a timed-out activity of a closed Review whose
    thread finished after a restarted watch started the Review again)."""
    if writer is None:
        return False
    owner = db.get(ReviewDocDB, (workflow_id, RUN_SLOT))
    return owner is not None and writer[0] in ((owner.data or {}).get("retired") or [])


def _write_blocked(db, workflow_id: str, row, writer: Optional[tuple]) -> bool:
    return _superseded(db, workflow_id, writer) or written_later(row, writer)


@_text_safe
def claim_run(workflow_id: str, post_id: str) -> bool:
    """Make the running activity's workflow run the owner of this workflow
    id's documents; called at the start of a run's first activity
    (research_post), before its model call. A new run (a Review started again under the same id) retires
    the previous owner and deletes its documents. Returns False for a run
    already retired: its late activity then writes nothing."""
    writer = current_writer()
    if writer is None:
        return True
    run = writer[0]
    db = get_db()
    try:
        db.execute(sql_text("BEGIN IMMEDIATE"))
        row = db.get(ReviewDocDB, (workflow_id, RUN_SLOT))
        data = dict(row.data or {}) if row is not None else {}
        retired = list(data.get("retired") or [])
        if run in retired:
            db.commit()
            return False
        owner = data.get("owner")
        if owner != run:
            if owner:
                retired.append(owner)
            db.query(ReviewDocDB).filter(
                ReviewDocDB.workflow_id == workflow_id,
                ReviewDocDB.slot != RUN_SLOT,
            ).delete(synchronize_session=False)
            db.merge(ReviewDocDB(
                workflow_id=workflow_id, slot=RUN_SLOT, post_id=post_id,
                data={"owner": run, "retired": retired}, updated_at=_now(), **_writer_fields(None),
            ))
        db.commit()
        return True
    finally:
        db.close()


def doc_written_later(db, workflow_id: str, slot: str, writer: Optional[tuple] = None) -> bool:
    """Whether a write of `slot` by `writer` (default: the running activity)
    would be a late attempt's, or a retired run's, and so is dropped."""
    return _write_blocked(db, workflow_id, db.get(ReviewDocDB, (workflow_id, slot)), writer or current_writer())


def claim_write(db, workflow_id: str, post_id: str, slot: str) -> bool:
    """For a card write that has no document of its own (the follow-up
    replies): record this activity as the writer of a marker slot, unless a
    later attempt already did. Returns False for a late attempt, whose write
    the caller then drops. The marker holds no text, so it is kept after a
    removal like the card it guards."""
    writer = current_writer()
    if _write_blocked(db, workflow_id, db.get(ReviewDocDB, (workflow_id, slot)), writer):
        return False
    db.merge(ReviewDocDB(
        workflow_id=workflow_id, slot=slot, post_id=post_id, data={}, updated_at=_now(),
        **_writer_fields(writer),
    ))
    return True


@_text_safe
def save_doc(workflow_id: str, post_id: str, slot: str, value: Any, first_wins: bool = False) -> Any:
    """Write a Review document; refused (PostRemoved) once the post's removal
    is recorded, so a late write cannot bring the text back.

    first_wins: keep a document already in the slot and return it decoded as
    `value`'s type (the written check: the first finished check of a text
    stands, see ReviewWorkflow._written_checks).

    A write from a late attempt (written_later) is dropped: what a later
    step wrote stays.
    """
    db = get_db()
    try:
        db.execute(sql_text("BEGIN IMMEDIATE"))
        if _post_removed(db, post_id):
            raise post_removed_error("not storing it")
        existing = db.get(ReviewDocDB, (workflow_id, slot))
        if first_wins and existing is not None:
            db.commit()
            return from_json(existing.data, type(value))
        save_doc_in(db, workflow_id, post_id, slot, value)
        db.commit()
        return value
    finally:
        db.close()


def save_doc_in(db, workflow_id: str, post_id: str, slot: str, value: Any) -> bool:
    """save_doc inside a caller's transaction. Returns False when the write
    was dropped as a late attempt's."""
    if _post_removed(db, post_id):
        raise post_removed_error("not storing it")
    writer = current_writer()
    if _write_blocked(db, workflow_id, db.get(ReviewDocDB, (workflow_id, slot)), writer):
        logger.info(f"Dropped a late attempt's write of {slot!r} ({workflow_id})")
        return False
    db.merge(ReviewDocDB(
        workflow_id=workflow_id, slot=slot, post_id=post_id, data=to_json(value), updated_at=_now(),
        **_writer_fields(writer),
    ))
    return True


def load_doc_in(db, workflow_id: str, post_id: str, slot: str, type_hint: Type[T]) -> T:
    if _post_removed(db, post_id):
        raise post_removed_error("not reading it")
    return from_json(_load_row(db, workflow_id, post_id, slot, "not reading it").data, type_hint)


@_text_safe
def store_founder_text(workflow_id: str, post_id: str, kind: str, client_request_id: str, text: str) -> str:
    """Store a founder's edit or nudge text for the Review to read (the API
    calls this before the update). Returns the slot the update names."""
    slot = founder_text_slot(kind, client_request_id, text_digest(text))
    save_doc(workflow_id, post_id, slot, text, first_wins=True)
    return slot


def remove_post_docs(db, post_id: str, workflow_ids: Iterable[str]) -> None:
    """Removal duty (issue #23), inside delete_post_text's transaction: record
    the removal on the stored post, replace its text, and delete every
    working document of the post (by post id, and by the workflows of its
    cards)."""
    from backend.activities.store_activities import POST_TEXT_REMOVED

    stored = db.get(StoredPostDB, post_id)
    if stored is not None:
        data = dict(stored.post_data or {})
        data["text"] = POST_TEXT_REMOVED
        data["text_removed"] = True
        stored.post_data = data
    ids = [w for w in workflow_ids if w]
    query = db.query(ReviewDocDB).filter(ReviewDocDB.post_id == post_id)
    query.delete(synchronize_session=False)
    if ids:
        db.query(ReviewDocDB).filter(ReviewDocDB.workflow_id.in_(ids)).delete(synchronize_session=False)
