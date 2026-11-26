"""Issue #35 (decided 2026-11-25): a removal also deletes the author's
identity (id, name, username, follower count, verified flag), the post URL
(it holds the handle), and the founder's edited draft with the check results
and post_anyway sentences derived from it.

Two Reviews of one post (this account and another account) are edited, then
one approve finds the post gone (404). For both cards, the stored card, the
stored post and the card the API returns (the get_card query, with
text_free_card and apply_stored_removal as GET /api/reviews/{id} applies
them) hold none of it, and neither does the SQLite file or its -wal file.
The card keeps its ids, status and timestamps.

Synthetic data only.
"""

import asyncio
import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from backend.activities.store_activities import (
    POST_TEXT_REMOVED,
    REMOVED_POST_KEPT_FIELDS,
    apply_stored_removal,
    get_stored_card,
    redact_removed_card,
    redact_removed_post,
)
from backend.models import Author
from backend.review_docs import store_posts
from backend.tests.review_waits import query_card, wait_for_card_ready
from backend.tests.test_text_out_of_history import MarkerXClient, marker_post, no_mention_plan
from backend.tests.test_written_workflow import PRODUCT, WritingAgent, _worker
from backend.workflows.review import POST_REMOVED_ERROR, ReviewWorkflow

# Each identity field gets its own word, so a failure names the field.
# No digits and no capitals (they would trigger the written check).
AUTHOR_ID = "author_quillfeather"
HANDLE = "quillfeather_dev"
NAME = "Robin Brambleworth"
PROFILE_URL = f"https://x.com/{HANDLE}"
POST_URL = f"https://x.com/{HANDLE}/status/1001"
# The founder's edits quote the post; one word per account's card
EDIT_THIS = "As you said, glimmerwick clusters drift. Is that your stack?"
EDIT_OTHER = "Fair point on snorkelvane clusters. What are you using?"
EDIT_WORDS = ("glimmerwick", "snorkelvane")
APPROVE_URL = "https://x.com/test/status/999"

IDENTITY = (AUTHOR_ID, HANDLE, "Brambleworth", PROFILE_URL, POST_URL)


def _post():
    return replace(
        marker_post(),
        url=POST_URL,
        author=Author(id=AUTHOR_ID, username=HANDLE, name=NAME, followers_count=5000, verified=True),
    )


def _assert_removed(card: dict, where: str) -> None:
    """No author identity, post URL or edited text in the card; ids kept."""
    dumped = json.dumps(card)
    for value in IDENTITY + EDIT_WORDS:
        assert value not in dumped, f"{value!r} left in {where}"
    post = card["post"]
    assert "author" not in post and "url" not in post, where
    assert set(post) <= set(REMOVED_POST_KEPT_FIELDS) | {"text", "text_removed"}, where
    assert post["id"] == "1001" and post["text"] == POST_TEXT_REMOVED and post["text_removed"] is True, where
    assert card["evidence"]["draft"]["text"] == POST_TEXT_REMOVED, where
    for result in card.get("edited_text_gate_results") or []:
        assert result["sentence"] == POST_TEXT_REMOVED, where
    for sentence in card.get("post_anyway_failing_sentences") or []:
        assert sentence == POST_TEXT_REMOVED, where


async def _api_card(handle) -> dict:
    """The card as GET /api/reviews/{id} returns it (backend/api.py get_review)."""
    return apply_stored_removal(handle.id, await query_card(handle))


def _stored_post() -> dict:
    from backend.database import StoredPostDB, get_db

    db = get_db()
    try:
        return dict(db.get(StoredPostDB, "1001").post_data)
    finally:
        db.close()


def _sqlite_bytes(db_path: Path) -> bytes:
    wal = Path(f"{db_path}-wal")
    return db_path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "draft_mode,this_legacy,other_legacy",
    [
        ("assembled", False, False),
        ("written", False, False),
        # A Review that carries the post in its input (recorded before issue
        # #23) builds its get_card query from workflow state. The removing
        # one redacts it itself (checked on the raw query); for the other
        # account's, the API redacts it from the stored removal
        ("assembled", True, True),
        ("written", True, True),
    ],
)
async def test_a_removal_drops_the_author_the_url_and_the_edits_on_every_card(
    make_review_input, test_db_isolation, draft_mode, this_legacy, other_legacy
):
    db_path = Path(test_db_isolation)
    post = _post()
    store_posts([post])
    written = draft_mode == "written"
    agent = WritingAgent(
        texts=["Good question. What are you using today?"] * 2 if written else None,
        plans=[no_mention_plan(), no_mention_plan()] if written else None,
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_client=MarkerXClient(gone="1001", post=post), agent=agent, draft_mode=draft_mode):
            handles = []
            for account, legacy in (("test", this_legacy), ("other", other_legacy)):
                review_input = make_review_input(
                    post=post, legacy=legacy, account=account, product_name=PRODUCT
                )
                handles.append(await env.client.start_workflow(
                    ReviewWorkflow.run, review_input, id=f"review:{account}:1001_author", task_queue="test"
                ))
            this, other = handles
            for handle, edit in ((this, EDIT_THIS), (other, EDIT_OTHER)):
                await wait_for_card_ready(handle)
                await handle.execute_update("edit", args=[edit, str(uuid.uuid4())])
            stored_before = _sqlite_bytes(db_path)
            for value in (HANDLE, NAME, *EDIT_WORDS):
                assert value.encode() in stored_before, f"the test must start with {value!r} stored"
            before = {h.id: get_stored_card(h.id) for h in handles}
            for card in before.values():
                assert card["edited_text_gate_results"], "the edit must have check results to remove"

            # approve re-fetches the post: 404, the removal duty runs
            with pytest.raises(WorkflowUpdateFailedError) as refused:
                # post_anyway: the assembled re-gate fails a free-text edit
                await this.execute_update("approve", args=[APPROVE_URL, str(uuid.uuid4()), True])
            assert refused.value.cause.type == POST_REMOVED_ERROR
            assert await this.result() == "skipped"

            for handle in handles:
                stored = get_stored_card(handle.id)
                _assert_removed(stored, f"stored card {handle.id}")
                _assert_removed(await _api_card(handle), f"get_card result {handle.id}")
                # Ids and timestamps stay
                assert stored["workflow_id"] == handle.id
                assert stored["created_at"] == before[handle.id]["created_at"]
                assert stored["post"]["created_at"] == before[handle.id]["post"]["created_at"]
                assert stored["post"]["conversation_id"] == "1001"
            # The removing Review's own query, before the API's overlay
            raw = await query_card(this)
            if this_legacy:
                assert not raw.get("text_free")
            _assert_removed(raw, f"raw get_card query {this.id}")
            assert get_stored_card(this.id)["status"] == "skipped"
            # The removal leaves the other account's status alone (the
            # time-skipping server can let that Review expire meanwhile)
            assert get_stored_card(other.id)["status"] in ("pending", "expired")

            stored_post = _stored_post()
            assert stored_post == redact_removed_post(stored_post)
            assert "author" not in stored_post and "url" not in stored_post

            # A later search that finds the post again cannot bring them back
            store_posts([post])
            assert "author" not in _stored_post()

            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(await this.fetch_history())

    after = _sqlite_bytes(db_path)
    for value in (HANDLE, NAME, AUTHOR_ID, *EDIT_WORDS):
        assert value.encode() not in after, f"{value!r} left in the SQLite file or its -wal"


@pytest.mark.asyncio
async def test_a_card_of_a_post_removed_before_any_card_was_stored_has_no_author(make_review_input):
    """The removal is recorded on the stored post only (no card of the post
    existed). A Review started later stores its "post removed" card from
    that stored post: the card records the removal and has no author or URL."""
    from backend.database import get_db
    from backend.review_docs import remove_post_docs
    from backend.tests.test_written_workflow import POST_REMOVED_SKIP_REASON

    post = _post()
    review_input = make_review_input(post=post, product_name=PRODUCT)
    db = get_db()
    try:
        remove_post_docs(db, post.id, [])
        db.commit()
    finally:
        db.close()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_client=MarkerXClient(post=post), draft_mode="assembled"):
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id="review:test:1001_removed_first", task_queue="test"
            )
            assert await handle.result() == "skipped"
            for where, card in (("stored card", get_stored_card(handle.id)), ("get_card", await _api_card(handle))):
                assert card["skip_reason"] == POST_REMOVED_SKIP_REASON, where
                assert card["post"]["text_removed"] is True, where
                assert "author" not in card["post"] and "url" not in card["post"], where
                for value in IDENTITY:
                    assert value not in json.dumps(card), f"{value!r} left in {where}"


def _card(**overrides) -> dict:
    card = {
        "workflow_id": "review:test:1001_card",
        "post": {
            "id": "1001",
            "text": "Is glimmerwick any good?",
            "url": POST_URL,
            "author": {"id": AUTHOR_ID, "username": HANDLE, "name": NAME, "followers_count": 5000, "verified": True},
            "created_at": "2026-01-01T00:00:00+00:00",
            "like_count": 3,
            "reply_count": 1,
            "repost_count": 0,
            "quote_count": 0,
            "conversation_id": "1000",
            "in_reply_to_user_id": "parent_author",
            "reply_settings": "everyone",
        },
        "evidence": {
            "post_id": "1001",
            "draft_mode": "assembled",
            "draft": {"mode": "assembled", "text": EDIT_THIS, "segments": [], "steers_applied": ["manual_edit"]},
        },
        "status": "approved",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-02T00:00:00+00:00",
        "posted_url": APPROVE_URL,
        "reply_post_id": "999",
        "edited_text_gate_results": [
            {"sentence": EDIT_THIS, "passed": False, "kind": "claim", "reason": f"'{EDIT_THIS}' is unsupported"},
        ],
        "post_anyway": True,
        "post_anyway_failing_sentences": [EDIT_THIS],
    }
    card.update(overrides)
    return card


def test_redact_removed_card_keeps_ids_status_and_timestamps_only():
    card = _card()
    redacted = redact_removed_card(card)

    assert redacted["post"] == {
        "id": "1001",
        "created_at": "2026-01-01T00:00:00+00:00",
        "like_count": 3,
        "reply_count": 1,
        "repost_count": 0,
        "quote_count": 0,
        "conversation_id": "1000",
        "reply_settings": "everyone",
        "text": POST_TEXT_REMOVED,
        "text_removed": True,
    }
    _assert_removed(redacted, "redact_removed_card")
    for key in ("workflow_id", "status", "created_at", "updated_at", "posted_url", "reply_post_id", "post_anyway"):
        assert redacted[key] == card[key]
    # Pure: the input is unchanged, and a second pass changes nothing
    assert card["post"]["author"]["username"] == HANDLE
    assert card["evidence"]["draft"]["text"] == EDIT_THIS
    assert redact_removed_card(redacted) == redacted


def test_removals_recorded_before_the_change_are_redacted_at_startup(test_db_isolation):
    """A card and a stored post removed under the earlier rules still hold the
    author, the URL and an assembled edit; init_db applies the current rules
    once, and a second start changes nothing."""
    from backend.database import ReviewCardDB, StoredPostDB, get_db, init_db

    old_card = _card()
    old_card["post"] = {**old_card["post"], "text": POST_TEXT_REMOVED, "text_removed": True}
    live_card = _card(workflow_id="review:test:2002_live")
    live_card["post"] = {**live_card["post"], "id": "2002"}
    db = get_db()
    try:
        for card in (old_card, live_card):
            db.add(ReviewCardDB(
                workflow_id=card["workflow_id"], post_data=card["post"], evidence_data=card["evidence"],
                status=card["status"], created_at=card["created_at"], updated_at=card["updated_at"],
                posted_url=card["posted_url"], reply_post_id=card["reply_post_id"],
                edited_text_gate_results=card["edited_text_gate_results"], post_anyway=True,
                post_anyway_failing_sentences=card["post_anyway_failing_sentences"],
            ))
        db.add(StoredPostDB(post_id="1001", post_data=old_card["post"], stored_at=card["created_at"]))
        db.commit()
    finally:
        db.close()

    init_db()

    _assert_removed(get_stored_card(old_card["workflow_id"]), "a card removed before the change")
    assert _stored_post() == redact_removed_post(old_card["post"])
    # A card of a post that was not removed is not touched
    live = get_stored_card(live_card["workflow_id"])
    assert live["post"]["author"]["username"] == HANDLE
    assert live["evidence"]["draft"]["text"] == EDIT_THIS

    from backend.activities.store_activities import redact_recorded_removals

    assert redact_recorded_removals().changed == 0


def test_a_start_retries_a_checkpoint_an_earlier_start_could_not_complete(test_db_isolation, monkeypatch):
    """The rows are redacted at the first start, whose checkpoint is blocked;
    the next start finds nothing to change and still runs the checkpoint, so
    the old pages leave the -wal file."""
    import backend.database
    from backend.activities.store_activities import redact_recorded_removals
    from backend.database import ReviewCardDB, get_db

    card = _card()
    db = get_db()
    try:
        db.add(ReviewCardDB(
            workflow_id=card["workflow_id"],
            post_data={**card["post"], "text": POST_TEXT_REMOVED, "text_removed": True},
            evidence_data=card["evidence"], status="skipped",
            created_at=card["created_at"], updated_at=card["updated_at"],
        ))
        db.commit()
    finally:
        db.close()
    calls = []
    real_checkpoint = backend.database.checkpoint_wal
    monkeypatch.setattr(backend.database, "checkpoint_wal", lambda: calls.append("blocked") and False)
    first = redact_recorded_removals()
    assert (first.changed, first.checkpoint_pending) == (1, True)
    assert calls == ["blocked"]  # one attempt: it waits out the busy timeout
    monkeypatch.setattr(backend.database, "checkpoint_wal", lambda: calls.append("ok") or real_checkpoint())
    again = redact_recorded_removals()
    assert (again.changed, again.checkpoint_pending) == (0, False)
    assert calls == ["blocked", "ok"]
    assert HANDLE.encode() not in _sqlite_bytes(Path(test_db_isolation))


@pytest.mark.asyncio
async def test_the_api_retries_a_pending_backfill_checkpoint_until_it_completes(monkeypatch):
    """A start whose checkpoint is blocked does not wait for the next start:
    the API retries it in the background until it completes."""
    import backend.database
    from backend.api import retry_wal_checkpoint

    results = iter([False, False, True])
    calls = []
    monkeypatch.setattr(backend.database, "checkpoint_wal", lambda: calls.append(1) or next(results))
    await asyncio.wait_for(retry_wal_checkpoint(interval_sec=0), timeout=5)
    assert len(calls) == 3


def test_redact_removed_post_tolerates_a_post_without_fields():
    assert redact_removed_post(None) == {"text": POST_TEXT_REMOVED, "text_removed": True}
    assert redact_removed_post({"id": "7", "author": None}) == {
        "id": "7", "text": POST_TEXT_REMOVED, "text_removed": True,
    }
