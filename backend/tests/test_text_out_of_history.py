"""Issue #23 (George's decision, 2026-11-25, option A): post text stays out of
Temporal history, and a removal leaves no copy of it in SQLite.

- New TopicWatch and Review histories carry ids, digests, counts and
  code-written reasons only. The tests put one unique synthetic word in the
  post, its author's handle and name, the drafts, the founder's edits and
  nudges, the founder's reply and the follow-up replies, then scan every
  event of the fetched history, payloads decoded.
- After a removal, the word is in neither the database file nor its -wal
  file (secure_delete, and a WAL checkpoint after the removal).
- Histories recorded on main 06db357, before this change, still replay.

Synthetic data only.
"""

import asyncio
import base64
import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest
from temporalio.client import WorkflowHistory, WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from backend.adapters import FakeXClient
from backend.models import Author, Post, ReplyPlan
from backend.ports.x_client import SearchResult
from backend.review_docs import text_digest
from backend.tests.review_waits import query_card, wait_for_card_ready
from backend.tests.test_edit_regate import fetch_history_past_approve
from backend.tests.test_written_workflow import PRODUCT, WritingAgent, _worker
from backend.workflows.review import POST_REMOVED_ERROR, ReviewWorkflow
from backend.workflows.topic_watch import TopicWatchWorkflow

# No digits (a number triggers the written check) and no capital (a name trigger)
MARKER = "zorblaxian"
HANDLE = "zorblaxian_dev"
APPROVE_URL = "https://x.com/test/status/999"
FIXTURES = Path(__file__).parent / "fixtures"


def _author(handle: str = HANDLE) -> Author:
    return Author(id="author_23", username=handle, name=f"Pat {MARKER}", followers_count=5000)


def marker_post(post_id: str = "1001") -> Post:
    return Post(
        id=post_id,
        # The question holds the word, so the plan's target point quotes it
        text=f"Struggling with distributed systems reliability. How do you folks handle eventual consistency in {MARKER} clusters?",
        author=_author(),
        created_at=datetime.now(timezone.utc).isoformat(),
        like_count=15,
        reply_count=3,
        repost_count=2,
        quote_count=0,
        conversation_id=post_id,
    )


class MarkerXClient(FakeXClient):
    """Search finds the marker post; the founder's reply and its follow-up
    replies quote the word too. lookup of `gone` returns 404."""

    def __init__(self, gone: Optional[str] = None, post: Optional[Post] = None):
        super().__init__()
        self.gone = gone
        self.post = post or marker_post()

    async def search_recent(self, query, since_id=None, max_results=10):
        if query.startswith("conversation_id:"):
            reply = replace(marker_post("20001"), text=f"Re: {MARKER} too", conversation_id="9999",
                            author=_author("zorblaxian_fan"))
            return SearchResult(posts=[reply], newest_id="20001", billable_reads=1)
        posts = [] if since_id else [self.post]
        return SearchResult(posts=posts, newest_id="1001" if posts else since_id, billable_reads=len(posts))

    async def lookup_post(self, post_id):
        if post_id == self.gone:
            return None
        if post_id == "9999":
            return replace(marker_post("9999"), text=f"My reply on {MARKER}", conversation_id="1001",
                           author=_author("founder_zorblaxian"))
        return replace(self.post, id=post_id)


def no_mention_plan() -> ReplyPlan:
    """A plan for a reply without a product claim (the written drafts here have none)."""
    question = f"How do you folks handle eventual consistency in {MARKER} clusters?"
    return ReplyPlan(
        reply_type="answer_question",
        target_point=question,
        mention_product=False,
        mention_product_reason="A question about their setup; no product needed.",
    )


def _decoded_strings(value):
    """Every string in a history's JSON, with each payload's data decoded."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "data" and isinstance(item, str):
                try:
                    yield base64.b64decode(item).decode("utf-8", "replace")
                except ValueError:
                    pass
            yield from _decoded_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _decoded_strings(item)
    elif isinstance(value, str):
        yield value


def assert_no_post_text(history: WorkflowHistory) -> None:
    """The history, every payload decoded, has no trace of the marker."""
    data = json.loads(history.to_json())
    assert data["events"], "empty history"
    for text in _decoded_strings(data):
        assert MARKER not in text.lower(), f"post text in history: {text[:200]}"


def _payload_count(history: WorkflowHistory) -> int:
    return sum(1 for _ in _decoded_strings(json.loads(history.to_json())))


def _db_has_marker() -> bool:
    from backend.database import get_db
    from sqlalchemy import text

    db = get_db()
    try:
        rows = db.execute(text(
            "SELECT post_data FROM stored_posts UNION ALL SELECT data FROM review_docs "
            "UNION ALL SELECT evidence_data FROM review_cards"
        )).all()
        return any(MARKER in str(r[0]) for r in rows)
    finally:
        db.close()


def _start_review(env, make_review_input, suffix, **overrides):
    overrides.setdefault("post", marker_post())
    review_input = make_review_input(**overrides)
    return env.client.start_workflow(
        ReviewWorkflow.run, review_input, id=f"review:test:{review_input.post_id}_{suffix}", task_queue="test"
    )


# ---------- new histories carry no post text ----------

@pytest.mark.asyncio
async def test_a_watch_and_the_review_it_starts_keep_the_post_out_of_history(make_topic_watch_input):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_client=MarkerXClient(), draft_mode="written",
                           workflows=[TopicWatchWorkflow, ReviewWorkflow]):
            watch = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                make_topic_watch_input(product_name=PRODUCT),
                id="watch:test:reliability_23",
                task_queue="test",
            )
            review = env.client.get_workflow_handle("review:test:1001")
            for _ in range(200):  # the watch starts the Review after its search
                try:
                    await review.describe()
                    break
                except Exception:
                    await asyncio.sleep(0.05)
            card = await wait_for_card_ready(review)
            assert card["status"] == "pending"
            # The text was used: it is on the card the founder sees
            assert MARKER in card["post"]["text"]
            assert MARKER in card["evidence"]["reply_plan"]["target_point"]

            await watch.signal("stop")
            await watch.result()
            for handle in (watch, review):
                history = await handle.fetch_history()
                assert _payload_count(history) > 10
                assert_no_post_text(history)
            await Replayer(workflows=[TopicWatchWorkflow, ReviewWorkflow]).replay_workflow(
                await watch.fetch_history()
            )


@pytest.mark.asyncio
async def test_an_assembled_review_through_edit_nudge_approve_confirm_and_followup(make_review_input):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_client=MarkerXClient(), draft_mode="assembled"):
            handle = await _start_review(env, make_review_input, "assembled_23")
            await wait_for_card_ready(handle)
            edited = await handle.execute_update("edit", args=[f"Thanks! Is {MARKER} your stack?", str(uuid.uuid4())])
            assert MARKER in edited
            nudged = await handle.execute_update("nudge", args=[f"Drop the {MARKER} part", str(uuid.uuid4())])
            card = await query_card(handle)
            assert card["evidence"]["draft"]["text"] == nudged
            # approve names the draft by digest (what the API sends)
            await handle.execute_update("approve", args=[
                APPROVE_URL, str(uuid.uuid4()), False, {"text_sha256": text_digest(nudged), "gate_state": None},
            ])
            await fetch_history_past_approve(handle)
            await handle.execute_update("confirm_posted", args=["https://x.com/founder/status/9999", str(uuid.uuid4())])
            await env.sleep(timedelta(days=3, hours=1))
            assert await handle.result() == "followup_due"

            card = await query_card(handle)
            assert card["status"] == "followup_due"
            [reply] = card["followup_replies"]
            assert MARKER in reply["text"] and reply["author"] == "zorblaxian_fan"
            history = await handle.fetch_history()
            assert_no_post_text(history)
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_a_written_review_through_edit_nudge_and_approve(make_review_input):
    agent = WritingAgent(texts=[
        f"Good question about {MARKER} clusters. What are you using today?",
        f"Short version: {MARKER} clusters are hard. What are you using today?",
    ], plans=[no_mention_plan()])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_client=MarkerXClient(), agent=agent):
            handle = await _start_review(env, make_review_input, "written_23", product_name=PRODUCT)
            card = await wait_for_card_ready(handle)
            assert MARKER in card["evidence"]["draft"]["text"]
            # The drafter was sent the post (read from the database)
            assert MARKER in agent.requests[0].post.text

            edit = f"That {MARKER} setup is hard. What are you using today?"
            assert await handle.execute_update("edit", args=[edit, str(uuid.uuid4())]) == edit
            assert (await query_card(handle))["edited_text_gate_state"] == "passed"
            nudged = await handle.execute_update("nudge", args=[f"Shorter, keep {MARKER}", str(uuid.uuid4())])
            assert MARKER in nudged

            # A stale draft is refused (by digest), the current one approved
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("approve", args=[
                    APPROVE_URL, str(uuid.uuid4()), False, {"text_sha256": text_digest(edit), "gate_state": None},
                ])
            await handle.execute_update("approve", args=[
                APPROVE_URL, str(uuid.uuid4()), False, {"text_sha256": text_digest(nudged), "gate_state": None},
            ])
            history = await fetch_history_past_approve(handle)
            assert_no_post_text(history)
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_a_written_review_skipped_by_its_check_keeps_the_reasons_out_of_history(make_review_input):
    """The skip reason quotes the failing sentences: it is on the card only."""
    unsupported = f"Our {MARKER} tool beats every product."
    agent = WritingAgent(texts=[unsupported, unsupported])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_client=MarkerXClient(), agent=agent):
            handle = await _start_review(env, make_review_input, "written_skip_23", product_name=PRODUCT)
            assert await handle.result() == "skipped"
            card = await query_card(handle)
            assert card["skip_reason"].startswith("Not enough evidence:")
            assert MARKER in card["skip_reason"]
            assert_no_post_text(await handle.fetch_history())


# ---------- removal leaves no copy in SQLite ----------

def _sqlite_bytes(db_path: Path) -> bytes:
    data = db_path.read_bytes()
    wal = Path(f"{db_path}-wal")
    return data + (wal.read_bytes() if wal.exists() else b"")


@pytest.mark.asyncio
@pytest.mark.parametrize("draft_mode", ["assembled", "written"])
async def test_after_a_removal_the_text_is_in_neither_the_database_nor_its_wal(
    make_review_input, test_db_isolation, draft_mode
):
    db_path = Path(test_db_isolation)
    # The removal duty (issue #9) removes the post's text and keeps its
    # author on the card, so the word is only in the text here
    post = replace(
        marker_post(), url="", author=Author(id="author_9", username="pat_dev", name="Pat", followers_count=5000)
    )
    # Written drafts here make no product claim; an assembled draft follows the fake plan
    agent = WritingAgent(
        texts=[f"Good question about {MARKER} clusters. What are you using today?"],
        plans=[no_mention_plan()] if draft_mode == "written" else None,
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_client=MarkerXClient(gone="1001", post=post), agent=agent, draft_mode=draft_mode):
            handle = await _start_review(
                env, make_review_input, f"removal_{draft_mode}", product_name=PRODUCT, post=post
            )
            await wait_for_card_ready(handle)
            # The removal keeps a founder's edit of an assembled draft (the
            # founder's own words, issue #9) and removes one of a written
            # draft (it starts from model text that can quote the post)
            edit = f"Is {MARKER} your stack?" if draft_mode == "written" else "What is your stack?"
            await handle.execute_update("edit", args=[edit, str(uuid.uuid4())])
            assert MARKER.encode() in _sqlite_bytes(db_path), "the test must start with the text stored"

            # approve re-fetches the post: 404, the removal duty runs
            with pytest.raises(WorkflowUpdateFailedError) as refused:
                # post_anyway: the assembled re-gate fails the free-text edit
                await handle.execute_update("approve", args=[APPROVE_URL, str(uuid.uuid4()), True])
            assert refused.value.cause.type == POST_REMOVED_ERROR
            assert await handle.result() == "skipped"
            assert not _db_has_marker()
            assert_no_post_text(await handle.fetch_history())

    # Both files: the database, and the -wal file (later writes, such as the
    # skipped card, add frames after the removal's checkpoint; none holds it)
    assert MARKER.encode() not in _sqlite_bytes(db_path)


# ---------- histories recorded before this change replay ----------

# Recorded on main 06db357 (before issue #23): a written Review edited,
# nudged and approved; a written Review skipped by its check; an assembled
# Review confirmed and through its follow-up; a TopicWatch after its first
# search. Synthetic data; worker and client identities scrubbed.
MAIN_06DB357_HISTORIES = [
    ("review_history_main_06db357_written_edit_nudge_approve.json",
     "review:test:1001_main_06db357_written_edit_nudge_approve"),
    ("review_history_main_06db357_written_gate_skip.json", "review:test:1001_main_06db357_written_gate_skip"),
    ("review_history_main_06db357_assembled_confirmed_followup.json",
     "review:test:1001_main_06db357_assembled_followup"),
    ("topic_watch_history_main_06db357_in_flight.json", "watch:test:reliability_main_06db357_in_flight"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture,workflow_id", MAIN_06DB357_HISTORIES)
async def test_histories_recorded_before_the_change_replay(fixture, workflow_id):
    history = WorkflowHistory.from_json(workflow_id, (FIXTURES / fixture).read_text())
    await Replayer(workflows=[TopicWatchWorkflow, ReviewWorkflow]).replay_workflow(history)


def test_the_recorded_histories_carry_the_post_and_the_new_ones_do_not():
    """Control for the scan above: it finds post text where there is some."""
    fixture, workflow_id = MAIN_06DB357_HISTORIES[0]
    history = WorkflowHistory.from_json(workflow_id, (FIXTURES / fixture).read_text())
    texts = list(_decoded_strings(json.loads(history.to_json())))
    assert any("eventual consistency" in t for t in texts)


# ---------- review round 1 (retries, failures, busy readers) ----------

def test_a_founder_text_is_stored_once_per_request_and_text(make_review_input):
    """A retry of an edit with other text gets its own slot: the text an
    update is checking cannot be replaced under it."""
    from backend.review_docs import load_doc, store_founder_text

    review_input = make_review_input(post=marker_post())
    first = store_founder_text("review:test:1", review_input.post_id, "edit", "r1", "First text.")
    again = store_founder_text("review:test:1", review_input.post_id, "edit", "r1", "First text.")
    other = store_founder_text("review:test:1", review_input.post_id, "edit", "r1", "Other text.")
    assert first == again != other
    assert load_doc("review:test:1", review_input.post_id, first, str) == "First text."
    assert load_doc("review:test:1", review_input.post_id, other, str) == "Other text."


def test_a_database_error_reaches_history_without_its_data():
    """SQLAlchemy puts a failed statement's parameters in its message; an
    activity failure's message is recorded in history."""
    from sqlalchemy.exc import IntegrityError
    from temporalio.exceptions import ApplicationError
    from backend.review_docs import DATABASE_ERROR_TYPE, text_safe_db_errors

    with pytest.raises(ApplicationError) as raised:
        with text_safe_db_errors():
            raise IntegrityError("INSERT INTO review_docs ...", {"data": f"a draft about {MARKER}"}, Exception("x"))
    assert raised.value.type == DATABASE_ERROR_TYPE
    assert not raised.value.non_retryable
    assert MARKER not in str(raised.value) and raised.value.__cause__ is None
    assert raised.value.__suppress_context__


@pytest.mark.asyncio
async def test_a_removal_is_retried_until_its_checkpoint_completes(make_review_input, test_db_isolation):
    """A reader holding an old snapshot keeps the WAL from being truncated:
    the removal fails retryably and completes once the reader is gone."""
    import sqlite3
    from temporalio.exceptions import ApplicationError
    from backend.activities.store_activities import (
        WAL_CHECKPOINT_PENDING_ERROR,
        DeletePostTextInput,
        _delete_post_text_sync,
    )
    from backend.review_docs import save_doc

    db_path = Path(test_db_isolation)
    # The author is kept on removal (issue #9), so the word is only in the text
    plain_author = Author(id="author_9", username="pat_dev", name="Pat", followers_count=5000)
    review_input = make_review_input(post=replace(marker_post(), url="", author=plain_author))
    save_doc("review:test:1001_busy", review_input.post_id, "evidence", f"a draft about {MARKER}")
    reader = sqlite3.connect(db_path, timeout=0.1)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM review_docs").fetchone()  # holds a snapshot
    removal = DeletePostTextInput(workflow_id="review:test:1001_busy", idempotency_key="k", post_id="1001")
    try:
        with pytest.raises(ApplicationError) as pending:
            _delete_post_text_sync(removal)
        assert pending.value.type == WAL_CHECKPOINT_PENDING_ERROR and not pending.value.non_retryable
    finally:
        reader.rollback()
        reader.close()
    _delete_post_text_sync(removal)  # the retry
    assert MARKER.encode() not in _sqlite_bytes(db_path)


@pytest.mark.asyncio
async def test_a_retried_claim_gate_gates_the_same_draft(make_review_input):
    """The gate rewrites the draft it gates; a retry (worker restart after
    the write) reads the copy draft_reply stored and returns the same result."""
    from temporalio.testing import ActivityEnvironment
    from backend.activities.claim_gate_activity import ClaimGateInput, claim_gate
    from backend.activities.reply_activities import DraftReplyInput, ReplyActivities
    from backend.adapters import FakeReplyAgent
    from backend.activities.reply_activities import PlanReplyInput, ResearchPostInput

    review_input = make_review_input()
    env = ActivityEnvironment()  # workflow id "test"
    reply = ReplyActivities(FakeReplyAgent(), draft_mode="assembled")
    await env.run(reply.research_post, ResearchPostInput(
        post=None, topic="reliability", product_brief=review_input.product_brief, idempotency_key="r",
        post_id=review_input.post_id,
    ))
    await env.run(reply.plan_reply, PlanReplyInput(
        post=None, evidence=None, idempotency_key="p", post_id=review_input.post_id, attempt=1,
    ))
    drafted = await env.run(reply.draft_reply, DraftReplyInput(
        evidence=None, idempotency_key="d", product_brief=review_input.product_brief,
        post_id=review_input.post_id, source_slot="evidence", out_slot="draft:attempt1",
    ))
    assert drafted.has_segments
    gate = ClaimGateInput(segments=[], founder_materials=[], idempotency_key="g",
                          post_id=review_input.post_id, slot="draft:attempt1")
    first = await env.run(claim_gate, gate)
    second = await env.run(claim_gate, gate)
    assert first.passed and second == first


@pytest.mark.asyncio
async def test_an_edit_whose_card_write_failed_can_be_retried(make_review_input):
    """The Review's state changes only once the edit's card write committed;
    a failed write frees the request id, so a retry saves the edit."""
    from temporalio import activity
    from temporalio.exceptions import ApplicationError
    from backend.activities.store_activities import update_card as real_update_card

    fail = {"edit": True}

    @activity.defn(name="update_card")
    async def flaky_update_card(input):
        from backend.activities.store_activities import UpdateCardInput
        from temporalio.converter import value_to_type
        input = value_to_type(UpdateCardInput, input)
        if input.edit_text_slot and fail["edit"]:
            raise ApplicationError("simulated card write failure", non_retryable=True)
        return await real_update_card(input)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = _worker(env, x_client=MarkerXClient(), draft_mode="assembled")
        worker._config["activities"] = [
            a for a in worker._config["activities"] if getattr(a, "__temporal_activity_definition").name != "update_card"
        ] + [flaky_update_card]
        async with type(worker)(env.client, **{k: v for k, v in worker._config.items() if k != "client"}):
            handle = await _start_review(env, make_review_input, "edit_retry_23")
            card = await wait_for_card_ready(handle)
            before = card["evidence"]["draft"]["text"]
            request_id = str(uuid.uuid4())
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("edit", args=["What is your stack?", request_id])
            state = await handle.query("get_card")
            assert state["draft_sha256"] == text_digest(before) and state["edited_text_gate_state"] is None
            assert (await query_card(handle))["evidence"]["draft"]["text"] == before

            fail["edit"] = False
            assert await handle.execute_update("edit", args=["What is your stack?", request_id]) == "What is your stack?"
            state = await handle.query("get_card")
            assert state["draft_sha256"] == text_digest("What is your stack?")
            card = await query_card(handle)
            assert card["evidence"]["draft"]["text"] == "What is your stack?"
            assert card["evidence"]["draft"]["steers_applied"].count("manual_edit") == 1


def test_a_late_attempt_cannot_overwrite_what_a_later_step_wrote(make_review_input):
    """A timed-out attempt whose thread finishes after its retry (and after
    the workflow moved on) writes nothing (review round 2)."""
    from unittest import mock
    from backend import review_docs
    from backend.database import ReviewDocDB, get_db

    review_input = make_review_input()
    post_id = review_input.post_id

    def write_as(seq, attempt, value):
        with mock.patch.object(review_docs, "current_writer", return_value=(seq, attempt)):
            review_docs.save_doc("review:test:late", post_id, "evidence", value)

    write_as(1, 1, "research, first attempt")
    write_as(2, 1, "plan")  # a later step
    write_as(1, 1, "research, late thread")  # dropped
    assert review_docs.load_doc("review:test:late", post_id, "evidence", str) == "plan"
    write_as(2, 2, "plan, retry")  # a later attempt of the same step wins
    write_as(2, 1, "plan, late thread")  # dropped
    assert review_docs.load_doc("review:test:late", post_id, "evidence", str) == "plan, retry"
    db = get_db()
    try:
        row = db.get(ReviewDocDB, ("review:test:late", "evidence"))
        assert (row.writer_seq, row.writer_attempt) == (2, 2)
    finally:
        db.close()


def test_the_post_anyway_sentences_are_the_ones_acknowledged(make_review_input):
    """update_card records the failing sentences of the check the approve
    names, not of whatever results the card holds then (review round 2)."""
    from backend.activities.claim_gate_activity import SentenceGateResult
    from backend.activities.store_activities import UpdateCardInput, _update_card_sync
    from backend.database import ReviewCardDB, get_db
    from backend.review_docs import save_doc

    review_input = make_review_input()
    wid = "review:test:1001_pa"
    db = get_db()
    db.add(ReviewCardDB(workflow_id=wid, post_data={"id": review_input.post_id, "text": "t"}, evidence_data={},
                        status="approved", created_at="t", updated_at="t",
                        edited_text_gate_results=[{"sentence": "A later edit.", "passed": False}]))
    db.commit()
    db.close()
    save_doc(wid, review_input.post_id, "edit_results:r1",
             [SentenceGateResult(sentence="The acknowledged one.", passed=False, kind="claim", reason="x", kept_segment=False)])
    _update_card_sync(UpdateCardInput(workflow_id=wid, idempotency_key="k", post_anyway=True,
                                      post_id=review_input.post_id, post_anyway_results_slot="edit_results:r1"))
    db = get_db()
    try:
        assert db.get(ReviewCardDB, wid).post_anyway_failing_sentences == ["The acknowledged one."]
    finally:
        db.close()
