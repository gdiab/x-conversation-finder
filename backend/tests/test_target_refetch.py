"""
Target post re-fetch and the removal duty (issue #9, ADR-0006 / Outpost ADR-0086).

- approve re-fetches the target post through the budgeted lookup_post activity;
- a 404 removes the stored post text and every model-written copy of it
  (why_it_fits, the problem span, the plan's target point), records the
  removal on the card and ends the Review as skipped ("post removed");
- a 200 leaves the card unchanged and approves;
- a reply-URL 404 still keeps the target post text (the #5 fix);
- Reviews approved before the re-fetch existed replay unchanged;
- the eval hook re-fetches a stored card before it is replayed.

Fake adapters and synthetic posts only; no X or OpenAI calls.
"""

import asyncio
import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory, WorkflowUpdateFailedError, WorkflowUpdateStage
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from backend.activities.claim_gate_activity import claim_gate, verify_edited_text
from backend.activities.reply_activities import ReplyActivities
from backend.activities.store_activities import (
    POST_REMOVED_SKIP_REASON,
    POST_TEXT_REMOVED,
    delete_post_text,
    get_all_cards,
    store_card,
    update_card,
)
from backend.activities.x_activities import XActivities
from backend.adapters import FakeReplyAgent, FakeXClient
from backend.budget import get_today_budget, set_kill_switch
from backend.workflows.review import TARGET_REFETCH_PATCH_ID, ReviewWorkflow

FIXTURES = Path(__file__).parent / "fixtures"
TARGET_ID = "1001"  # conftest sample_post
QUESTION = "How do you folks handle eventual consistency?"  # from the sample post
# Fragments of the sample post that must not survive a removal
POST_FRAGMENTS = ("Struggling with distributed", "eventual consistency")
APPROVE_URL = "https://x.com/test/status/999"


class QuotingAgent(FakeReplyAgent):
    """Fake research whose why_it_fits quotes the post, as a model's may.
    Records the evidence each draft() call received."""

    def __init__(self):
        super().__init__()
        self.drafted = []

    async def draft(self, evidence, steer=None, idempotency_key=None, plan=None):
        self.drafted.append(idempotency_key)
        return await super().draft(evidence, steer, idempotency_key, plan)

    async def research(self, post, topic, product_brief, idempotency_key):
        evidence = await super().research(post, topic, product_brief, idempotency_key)
        evidence.why_it_fits = f"The post asks '{QUESTION}'"
        return evidence


class TargetClient(FakeXClient):
    """Fake X client with a scripted outcome for lookups of the target post.

    outcomes: per target lookup, "found", "gone" (404), "error" (transient
    failure), "block" (wait for `release`, then found) or "block_gone" (wait,
    then 404). Past the end of the list, the last outcome repeats.
    """

    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = list(outcomes)
        self.target_lookups = 0
        self.release = asyncio.Event()
        self.blocked = asyncio.Event()

    async def lookup_post(self, post_id):
        if post_id != TARGET_ID:
            return await super().lookup_post(post_id)
        index = min(self.target_lookups, len(self.outcomes) - 1)
        self.target_lookups += 1
        outcome = self.outcomes[index]
        if outcome.startswith("block"):
            self.blocked.set()
            await self.release.wait()
            outcome = "gone" if outcome == "block_gone" else "found"
        if outcome == "gone":
            return None
        if outcome == "error":
            raise RuntimeError("synthetic transient X error")
        return await super().lookup_post(post_id)


def _worker(env, x_client, agent=None):
    x_acts = XActivities(x_client)
    reply_acts = ReplyActivities(agent or QuotingAgent())
    return Worker(
        env.client,
        task_queue="test",
        workflows=[ReviewWorkflow],
        activities=[
            x_acts.lookup_post,
            x_acts.check_followup,
            reply_acts.research_post,
            reply_acts.plan_reply,
            reply_acts.draft_reply,
            claim_gate,
            verify_edited_text,
            store_card,
            update_card,
            delete_post_text,
        ],
    )


def _x_reads():
    # The ledger can carry reads from earlier tests (cached settings), so
    # tests compare before and after.
    return get_today_budget()["x_reads"]


def _db_card(workflow_id):
    return next(c for c in get_all_cards() if c["workflow_id"] == workflow_id)


def _scheduled(history):
    return [
        e.activity_task_scheduled_event_attributes
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]


def _scheduled_names(history):
    return [a.activity_type.name for a in _scheduled(history)]


async def _start(env, make_review_input, suffix):
    review_input = make_review_input()
    handle = await env.client.start_workflow(
        ReviewWorkflow.run,
        review_input,
        id=f"review:test:{review_input.post.id}_{suffix}",
        task_queue="test",
    )
    await env.sleep(1)
    card = await handle.query("get_card")
    assert card["status"] == "pending", card
    return review_input, handle


# ---------- acceptance: 404 vs 200 on the re-fetch ----------


@pytest.mark.asyncio
async def test_404_on_refetch_redacts_text_and_spans_and_skips(make_review_input):
    client = TargetClient(["gone"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            review_input, handle = await _start(env, make_review_input, "refetch_404")
            before = await handle.query("get_card")
            # The card quotes the post before the removal (so the test means something)
            assert QUESTION in before["evidence"]["why_it_fits"]
            assert before["evidence"]["reply_plan"]["target_point"] == QUESTION
            assert before["evidence"]["reply_fit"]["brief_problem_span"]

            with pytest.raises(WorkflowUpdateFailedError) as excinfo:
                await handle.execute_update("approve", args=[APPROVE_URL, str(uuid.uuid4()), False])
            assert excinfo.value.cause.type == "PostRemoved"
            assert await handle.result() == "skipped"

            for card in (await handle.query("get_card"), _db_card(handle.id)):
                assert card["status"] == "skipped"
                assert card["skip_reason"] == POST_REMOVED_SKIP_REASON
                assert card["post"]["text"] == POST_TEXT_REMOVED
                assert card["post"]["text_removed"] is True
                assert card["evidence"]["why_it_fits"] == POST_TEXT_REMOVED
                assert card["evidence"]["reply_fit"]["brief_problem_span"] == POST_TEXT_REMOVED
                assert card["evidence"]["reply_plan"]["target_point"] == POST_TEXT_REMOVED
                dumped = json.dumps(card)
                for fragment in POST_FRAGMENTS:
                    assert fragment not in dumped
                # The valid assembled draft and its segment trace stay
                assert card["evidence"]["draft"]["text"] == before["evidence"]["draft"]["text"]
                assert [seg["text"] for seg in card["evidence"]["draft"]["segments"]] == [
                    seg["text"] for seg in before["evidence"]["draft"]["segments"]
                ]
                assert all(seg["text"] for seg in card["evidence"]["draft"]["segments"])
                assert [seg.get("sentence_index") for seg in card["evidence"]["draft"]["segments"]] == [
                    seg.get("sentence_index") for seg in before["evidence"]["draft"]["segments"]
                ]
            assert client.target_lookups == 1

            history = await handle.fetch_history()
            names = _scheduled_names(history)
            assert names.count("lookup_post") == 1
            assert "delete_post_text" in names
            lookup = next(a for a in _scheduled(history) if a.activity_type.name == "lookup_post")
            assert TARGET_ID in str(lookup.input)
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_200_on_refetch_leaves_card_unchanged_and_approves(make_review_input):
    client = TargetClient(["found"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            review_input, handle = await _start(env, make_review_input, "refetch_200")
            before = await handle.query("get_card")
            reads_before = _x_reads()

            assert await handle.execute_update(
                "approve", args=[APPROVE_URL, str(uuid.uuid4()), False]
            ) == "Approved"
            await env.sleep(1)

            card = await handle.query("get_card")
            assert card["status"] == "approved"
            assert card["post"] == before["post"]
            assert "text_removed" not in card["post"]
            assert card["evidence"]["why_it_fits"] == before["evidence"]["why_it_fits"]
            assert card["evidence"]["reply_plan"] == before["evidence"]["reply_plan"]
            stored = _db_card(handle.id)
            assert stored["status"] == "approved"
            assert stored["post"]["text"] == review_input.post.text
            assert stored["evidence"]["reply_plan"]["target_point"] == QUESTION

            # One budgeted X read for the re-fetch (research and plan use no X reads)
            assert client.target_lookups == 1
            assert _x_reads() - reads_before == 1
            history = await handle.fetch_history()
            assert "delete_post_text" not in _scheduled_names(history)
            markers = [e for e in history.events if e.HasField("marker_recorded_event_attributes")]
            assert any(TARGET_REFETCH_PATCH_ID.encode() in m.SerializeToString() for m in markers)
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)


# ---------- budget and retries ----------


@pytest.mark.asyncio
async def test_refetch_retry_reuses_the_budget_reservation(make_review_input):
    """A lookup attempt that fails is retried by Temporal with the same
    activity id, so reserve_x_budget reuses the reservation: one read on the
    ledger. (Issue #8 may change how a failed attempt that reached X is
    counted; update this expectation with it.)"""
    client = TargetClient(["error", "found"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            _, handle = await _start(env, make_review_input, "refetch_retry")
            reads_before = _x_reads()
            assert await handle.execute_update(
                "approve", args=[APPROVE_URL, str(uuid.uuid4()), False]
            ) == "Approved"
            assert client.target_lookups == 2
            assert _x_reads() - reads_before == 1
            history = await handle.fetch_history()
            assert _scheduled_names(history).count("lookup_post") == 1


@pytest.mark.asyncio
async def test_failed_refetch_fails_approve_and_same_request_id_can_retry(make_review_input):
    """Retries exhausted: approve fails, the card stays pending with its text,
    and a retry with the same request id re-fetches instead of reporting
    "Approved" through the dedupe path."""
    client = TargetClient(["error", "error", "error", "found"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            review_input, handle = await _start(env, make_review_input, "refetch_fail")
            request_id = str(uuid.uuid4())
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("approve", args=[APPROVE_URL, request_id, False])
            card = await handle.query("get_card")
            assert card["status"] == "pending"
            assert card["post"]["text"] == review_input.post.text
            assert _db_card(handle.id)["status"] == "pending"

            assert await handle.execute_update("approve", args=[APPROVE_URL, request_id, False]) == "Approved"
            assert client.target_lookups == 4
            await env.sleep(1)
            assert (await handle.query("get_card"))["status"] == "approved"


@pytest.mark.asyncio
async def test_over_budget_refetch_fails_approve_without_removing_text(make_review_input):
    """Over budget, lookup_post raises BudgetExceeded and never reports a 404,
    so nothing is removed and no X call is made."""
    client = TargetClient(["gone"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            review_input, handle = await _start(env, make_review_input, "refetch_budget")
            set_kill_switch(True)
            with pytest.raises(WorkflowUpdateFailedError) as excinfo:
                await handle.execute_update("approve", args=[APPROVE_URL, str(uuid.uuid4()), False])
            assert "kill switch" in str(excinfo.value.cause.cause)
            assert client.target_lookups == 0
            for card in (await handle.query("get_card"), _db_card(handle.id)):
                assert card["status"] == "pending"
                assert card["post"]["text"] == review_input.post.text
                assert not card["post"].get("text_removed")
            set_kill_switch(False)


# ---------- concurrency during the re-fetch ----------


@pytest.mark.asyncio
async def test_edit_nudge_and_second_approve_rejected_while_refetch_in_flight(make_review_input):
    client = TargetClient(["block"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            _, handle = await _start(env, make_review_input, "refetch_overlap")
            approve = await handle.start_update(
                "approve",
                args=[APPROVE_URL, str(uuid.uuid4()), False],
                wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            )
            for name, args in (
                ("edit", ["Thanks for sharing! What stack are you on?", str(uuid.uuid4())]),
                ("nudge", ["Make it shorter", str(uuid.uuid4())]),
                ("approve", [APPROVE_URL, str(uuid.uuid4()), False]),
            ):
                with pytest.raises(WorkflowUpdateFailedError) as excinfo:
                    await handle.execute_update(name, args=args)
                assert "approval is in progress" in str(excinfo.value.cause), name

            client.release.set()
            assert await approve.result() == "Approved"
            await env.sleep(1)
            assert (await handle.query("get_card"))["status"] == "approved"


@pytest.mark.asyncio
async def test_skip_during_refetch_waits_for_the_removal(make_review_input):
    """A founder skip while the re-fetch is running must not let the
    workflow close before a 404's removal is done."""
    client = TargetClient(["block_gone"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            _, handle = await _start(env, make_review_input, "refetch_skip_race")
            approve = await handle.start_update(
                "approve",
                args=[APPROVE_URL, str(uuid.uuid4()), False],
                wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            )
            await client.blocked.wait()
            await handle.signal("skip", args=["Not relevant", str(uuid.uuid4())])
            await env.sleep(1)
            client.release.set()

            with pytest.raises(WorkflowUpdateFailedError) as excinfo:
                await approve.result()
            assert excinfo.value.cause.type == "PostRemoved"
            assert await handle.result() == "skipped"
            for card in (await handle.query("get_card"), _db_card(handle.id)):
                assert card["status"] == "skipped"
                assert card["skip_reason"] == POST_REMOVED_SKIP_REASON
                assert card["post"]["text_removed"] is True
                for fragment in POST_FRAGMENTS:
                    assert fragment not in json.dumps(card)
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(await handle.fetch_history())


# The expiry race (the 48h timer firing during approve's re-fetch) takes the
# same wait as the skip race above. It is not tested directly: the
# time-skipping test server cannot advance time while an activity runs.


@pytest.mark.asyncio
async def test_removal_reaches_every_stored_card_of_the_post(make_review_input):
    """Two accounts review the same post. A 404 at one account's approve
    removes the text from both stored cards; the other Review's query (its
    workflow state still holds the text) is redacted on read, a nudge there
    sends nothing to the model, and a Review that stores its card only after
    the removal stores it redacted."""
    from backend.activities.store_activities import apply_stored_removal

    client = TargetClient(["gone"])
    agent = QuotingAgent()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client, agent):
            review_input, first = await _start(env, make_review_input, "refetch_multi_a")
            other_input = make_review_input(account="other")
            other = await env.client.start_workflow(
                ReviewWorkflow.run, other_input,
                id=f"review:other:{other_input.post.id}_refetch_multi_b", task_queue="test",
            )
            await env.sleep(1)
            assert _db_card(other.id)["post"]["text"] == review_input.post.text

            with pytest.raises(WorkflowUpdateFailedError):
                await first.execute_update("approve", args=[APPROVE_URL, str(uuid.uuid4()), False])

            stored_other = _db_card(other.id)
            assert stored_other["post"]["text_removed"] is True
            assert stored_other["status"] == "pending"  # it ends at its own approve
            for fragment in POST_FRAGMENTS:
                assert fragment not in json.dumps(stored_other)

            queried = await other.query("get_card")
            assert queried["post"]["text"] == review_input.post.text  # workflow state
            redacted = apply_stored_removal(other.id, queried)
            assert redacted["post"]["text_removed"] is True
            assert redacted["status"] == "pending"
            for fragment in POST_FRAGMENTS:
                assert fragment not in json.dumps(redacted)

            # A nudge on the other Review must not send its stale evidence on
            drafts_before = len(agent.drafted)
            with pytest.raises(WorkflowUpdateFailedError):
                await other.execute_update("nudge", args=["Make it shorter", str(uuid.uuid4())])
            assert len(agent.drafted) == drafts_before

            # A Review whose card is stored after the removal stores it redacted
            late_input = make_review_input(account="late")
            late = await env.client.start_workflow(
                ReviewWorkflow.run, late_input,
                id=f"review:late:{late_input.post.id}_refetch_multi_c", task_queue="test",
            )
            await env.sleep(1)
            stored_late = _db_card(late.id)
            assert stored_late["post"]["text_removed"] is True
            for fragment in POST_FRAGMENTS:
                assert fragment not in json.dumps(stored_late)


@pytest.mark.asyncio
async def test_approve_is_rejected_until_the_card_is_stored(make_review_input):
    """A Review is "pending" from the start. An approve during research would
    re-fetch with no card to remove the text from, so it is rejected."""

    class SlowResearch(QuotingAgent):
        release = asyncio.Event()

        async def research(self, post, topic, product_brief, idempotency_key):
            await self.release.wait()
            return await super().research(post, topic, product_brief, idempotency_key)

    agent = SlowResearch()
    client = TargetClient(["gone"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client, agent):
            review_input = make_review_input()
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input,
                id=f"review:test:{review_input.post.id}_approve_too_early", task_queue="test",
            )
            with pytest.raises(WorkflowUpdateFailedError) as excinfo:
                await handle.execute_update("approve", args=[APPROVE_URL, str(uuid.uuid4()), False])
            assert "before the draft card is ready" in str(excinfo.value.cause)
            assert client.target_lookups == 0
            agent.release.set()
            await env.sleep(1)
            assert (await handle.query("get_card"))["status"] == "pending"
            assert _db_card(handle.id)["post"]["text"] == review_input.post.text


@pytest.mark.asyncio
async def test_late_store_after_removal_keeps_removed_status(make_review_input):
    """A store_card attempt that finishes after the removal (a timed-out
    attempt's thread) keeps the text redacted and the skipped status."""
    from backend.activities.store_activities import StoreCardInput, _store_card_sync
    from backend.models import ReviewCard

    client = TargetClient(["gone"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            review_input, handle = await _start(env, make_review_input, "late_store")
            pending = await handle.query("get_card")
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("approve", args=[APPROVE_URL, str(uuid.uuid4()), False])
            assert _db_card(handle.id)["status"] == "skipped"

    # Replay the stale first store: the pending card as it was before approve
    stale = ReviewCard(
        workflow_id=handle.id, post=review_input.post, evidence=_evidence_from(pending["evidence"]),
        status="pending", created_at=pending["created_at"], updated_at=pending["updated_at"],
    )
    assert _store_card_sync(StoreCardInput(card=stale, idempotency_key="stale"))
    stored = _db_card(handle.id)
    assert stored["status"] == "skipped"
    assert stored["skip_reason"] == POST_REMOVED_SKIP_REASON
    for fragment in POST_FRAGMENTS:
        assert fragment not in json.dumps(stored)


def _evidence_from(data):
    """Rebuild an Evidence object from a get_card evidence dict (test helper)."""
    from backend.models import ClaimGateResult, Draft, Evidence, FounderMaterial, Segment

    draft = data["draft"]
    segments = [Segment(kind=s["kind"], text=s.get("text"), source_passage_id=s.get("source_passage_id"),
                        sentence_index=s.get("sentence_index"), phrase_id=s.get("phrase_id"))
                for s in draft["segments"]]
    return Evidence(
        post_id=data["post_id"], matched_topic=data["matched_topic"], why_it_fits=data["why_it_fits"],
        confidence_score=data["confidence_score"], confidence_inputs=data["confidence_inputs"],
        founder_materials=[FounderMaterial(source=m["source"], passage=m["passage"], relevance=m["relevance"],
                                           passage_id=m.get("passage_id"))
                           for m in data["founder_materials"]],
        draft=Draft(text=draft["text"], segments=segments,
                    claim_gate=ClaimGateResult(passed=True, segments=segments, failed_sentences=[], reasoning="ok")),
    )


# ---------- the #5 fix still holds ----------


@pytest.mark.asyncio
async def test_reply_url_404_keeps_target_text_after_refetch(make_review_input):
    """Approve's re-fetch finds the target (200); a later 404 on the founder's
    reply URL fails confirm_posted and removes nothing."""

    class GoneReply(TargetClient):
        async def lookup_post(self, post_id):
            if post_id == "404404":
                return None
            return await super().lookup_post(post_id)

    client = GoneReply(["found"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client):
            review_input, handle = await _start(env, make_review_input, "refetch_reply_404")
            await handle.execute_update("approve", args=[APPROVE_URL, str(uuid.uuid4()), False])
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update(
                    "confirm_posted", args=["https://x.com/test/status/404404", str(uuid.uuid4())]
                )
            for card in (await handle.query("get_card"), _db_card(handle.id)):
                assert card["status"] == "approved"
                assert card["post"]["text"] == review_input.post.text
                assert not card["post"].get("text_removed")
                assert card["evidence"]["reply_plan"]["target_point"] == QUESTION
            assert "delete_post_text" not in _scheduled_names(await handle.fetch_history())


# ---------- replay safety ----------

# A Review approved on main 9f4df4a (before the re-fetch), waiting for the
# reply URL. Synthetic data; worker and client identities scrubbed.
APPROVED_HISTORY = ("review_history_main_9f4df4a_approved.json", "review:test:5090_main_9f4df4a_approved")


def _approved_history() -> WorkflowHistory:
    fixture, workflow_id = APPROVED_HISTORY
    return WorkflowHistory.from_json(workflow_id, (FIXTURES / fixture).read_text())


def test_approved_fixture_has_an_approve_without_a_refetch():
    events = json.loads((FIXTURES / APPROVED_HISTORY[0]).read_text())["events"]
    updates = [
        e["workflowExecutionUpdateAcceptedEventAttributes"]["acceptedRequest"]["input"]["name"]
        for e in events
        if e["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED"
    ]
    scheduled = [
        e["activityTaskScheduledEventAttributes"]["activityType"]["name"]
        for e in events
        if e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"
    ]
    assert updates == ["approve"]
    assert "lookup_post" not in scheduled
    assert scheduled[-1] == "update_card"


@pytest.mark.asyncio
async def test_review_approved_before_refetch_replays():
    await Replayer(workflows=[ReviewWorkflow]).replay_workflow(_approved_history())


@pytest.mark.asyncio
async def test_approved_history_fails_replay_without_patch_guard(monkeypatch):
    """Control: with the re-fetch unguarded, the old approval would not replay."""
    from temporalio import workflow as temporal_workflow

    real_patched = temporal_workflow.patched
    monkeypatch.setattr(
        temporal_workflow,
        "patched",
        lambda patch_id: True if patch_id == TARGET_REFETCH_PATCH_ID else real_patched(patch_id),
    )
    result = await Replayer(workflows=[ReviewWorkflow]).replay_workflow(
        _approved_history(), raise_on_replay_failure=False
    )
    assert result.replay_failure is not None
    assert "Nondeterminism" in str(result.replay_failure)


# ---------- the eval hook ----------


async def _finished(workflow_id):
    """review_is_running for Reviews whose workflow has ended."""
    return False


async def _stored_card(make_review_input, suffix):
    """Run a Review until it expires (a past card) and return its workflow id."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, TargetClient(["found"])):
            _, handle = await _start(env, make_review_input, suffix)
            await env.sleep(timedelta(hours=2))  # review_timeout_hours=1 in tests
            assert await handle.result() == "expired"
            return handle.id


@pytest.mark.asyncio
async def test_eval_refetch_leaves_a_live_review_alone(make_review_input):
    """A pending Review holds its own copy of the evidence, so the eval must
    not remove text behind its back: no X read, no change."""
    from backend.target_refetch import refetch_stored_card_target, temporal_review_is_running

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, TargetClient(["found"])):
            review_input, handle = await _start(env, make_review_input, "eval_live")
            client = TargetClient(["gone"])
            reads_before = _x_reads()
            is_running = temporal_review_is_running(env.client)
            assert await refetch_stored_card_target(
                client, handle.id, "run1", review_is_running=is_running
            ) == "live"
            assert client.target_lookups == 0
            assert _x_reads() == reads_before
            assert _db_card(handle.id)["post"]["text"] == review_input.post.text
            assert (await handle.query("get_card"))["post"]["text"] == review_input.post.text


@pytest.mark.asyncio
async def test_eval_refetch_found_keeps_card(make_review_input):
    from backend.target_refetch import refetch_stored_card_target

    workflow_id = await _stored_card(make_review_input, "eval_found")
    client = TargetClient(["found"])
    reads_before = _x_reads()
    assert await refetch_stored_card_target(client, workflow_id, "run1", review_is_running=_finished) == "found"
    stored = _db_card(workflow_id)
    assert QUESTION in stored["post"]["text"]
    assert stored["evidence"]["reply_plan"]["target_point"] == QUESTION
    assert _x_reads() - reads_before == 1


@pytest.mark.asyncio
async def test_eval_refetch_404_removes_text_once_and_is_idempotent(make_review_input):
    from backend.target_refetch import refetch_stored_card_target

    workflow_id = await _stored_card(make_review_input, "eval_gone")
    client = TargetClient(["error", "gone"])
    reads_before = _x_reads()
    with pytest.raises(RuntimeError):
        await refetch_stored_card_target(client, workflow_id, "run1", review_is_running=_finished)
    # A retry of the same eval run reuses its budget reservation
    assert await refetch_stored_card_target(client, workflow_id, "run1", review_is_running=_finished) == "removed"
    assert _x_reads() - reads_before == 1
    stored = _db_card(workflow_id)
    assert stored["post"]["text_removed"] is True
    assert stored["status"] == "expired"  # status is left as it was
    dumped = json.dumps(stored)
    for fragment in POST_FRAGMENTS:
        assert fragment not in dumped
    # A query of the closed Review (workflow state still holds the text) is
    # redacted on read through the stored removal
    from backend.activities.store_activities import apply_stored_removal

    queried = {
        "post": {"id": TARGET_ID, "text": "Struggling with distributed systems reliability."},
        "evidence": {"why_it_fits": f"The post asks '{QUESTION}'", "reply_plan": {"target_point": QUESTION}},
        "status": "expired",
    }
    redacted = apply_stored_removal(workflow_id, queried)
    assert redacted["post"]["text_removed"] is True
    for fragment in POST_FRAGMENTS:
        assert fragment not in json.dumps(redacted)
    # Already removed: no further X read
    assert await refetch_stored_card_target(client, workflow_id, "run2", review_is_running=_finished) == "removed"
    assert client.target_lookups == 2
    assert _x_reads() - reads_before == 1


@pytest.mark.asyncio
async def test_eval_refetch_over_budget_raises_and_keeps_text(make_review_input):
    from backend.budget import BudgetExceeded
    from backend.target_refetch import refetch_stored_card_target

    workflow_id = await _stored_card(make_review_input, "eval_budget")
    client = TargetClient(["gone"])
    set_kill_switch(True)
    with pytest.raises(BudgetExceeded):
        await refetch_stored_card_target(client, workflow_id, "run1", review_is_running=_finished)
    set_kill_switch(False)
    assert client.target_lookups == 0
    assert QUESTION in _db_card(workflow_id)["post"]["text"]



@pytest.mark.asyncio
async def test_founder_skip_is_stored_so_the_eval_can_recheck_it(make_review_input):
    """A founder skip used to leave the stored card "pending" after the
    workflow ended, so the eval hook called it live forever."""
    from backend.target_refetch import refetch_stored_card_target

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, TargetClient(["found"])):
            _, handle = await _start(env, make_review_input, "eval_founder_skip")
            await handle.signal("skip", args=["Not relevant", str(uuid.uuid4())])
            assert await handle.result() == "skipped"
            stored = _db_card(handle.id)
            assert stored["status"] == "skipped"
            assert stored["skip_reason"] == "Not relevant"
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(await handle.fetch_history())

    client = TargetClient(["gone"])
    assert await refetch_stored_card_target(client, handle.id, "run1", review_is_running=_finished) == "removed"
    assert _db_card(handle.id)["post"]["text_removed"] is True


@pytest.mark.asyncio
async def test_removal_redacts_model_written_topic_and_source():
    from backend.activities.store_activities import redact_post_text_copies

    out = redact_post_text_copies({
        "reply_fit": {"post_kind": "question"},  # passages were checked (issue #4)
        "matched_topic": f"posts like '{QUESTION}'",
        "founder_materials": [
            {"source": f"brief, for '{QUESTION}'", "passage": "Our product records work.", "relevance": ""},
            {"source": "product_brief", "passage": "It runs locally.", "relevance": "fits"},
        ],
    })
    assert QUESTION not in json.dumps(out)
    assert out["founder_materials"][1]["source"] == "product_brief"
    assert out["founder_materials"][0]["passage"] == "Our product records work."


@pytest.mark.asyncio
async def test_eval_rechecks_a_stale_pending_card_of_a_finished_review(make_review_input):
    """Reviews skipped before issue #9 ended with their stored card still
    "pending". Liveness comes from Temporal, so such a card is re-fetched."""
    from backend.target_refetch import refetch_stored_card_target, temporal_review_is_running

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, TargetClient(["found"])):
            _, handle = await _start(env, make_review_input, "eval_stale_pending")
            await handle.terminate("test: workflow ended, card left pending")
            assert _db_card(handle.id)["status"] == "pending"
            client = TargetClient(["gone"])
            assert await refetch_stored_card_target(
                client, handle.id, "run1", review_is_running=temporal_review_is_running(env.client)
            ) == "removed"
            assert client.target_lookups == 1
            # A workflow Temporal no longer knows is not running
            assert await temporal_review_is_running(env.client)("review:test:never-started") is False


class QuoteAsIdentifierAgent(QuotingAgent):
    """A drafting model that puts a post quote where identifiers belong."""

    def __init__(self, quote):
        super().__init__()
        self.quote = quote

    async def draft(self, evidence, steer=None, idempotency_key=None, plan=None):
        from backend.models import Segment

        evidence = await super().draft(evidence, steer, idempotency_key, plan)
        evidence.draft.segments = [
            Segment(kind="non_claim", phrase_id=self.quote),
            Segment(kind="claim", source_passage_id=self.quote, sentence_index=0),
            # A number copied from the post as an out-of-range index
            Segment(kind="claim", source_passage_id=evidence.founder_materials[0].passage_id, sentence_index=424242),
        ]
        return evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", [QUESTION, "Struggling"])  # a phrase, and an identifier-shaped word
async def test_model_identifiers_quoting_the_post_are_not_stored(make_review_input, quote):
    """A gate failure must not carry a model-supplied quote of the post into
    stored errors or the skip reason, where an eval removal could not find it."""
    from backend.target_refetch import refetch_stored_card_target

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, TargetClient(["found"]), QuoteAsIdentifierAgent(quote)):
            review_input = make_review_input()
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input,
                id=f"review:test:{review_input.post.id}_quote_as_id", task_queue="test",
            )
            assert await handle.result() == "skipped"
            queried = await handle.query("get_card")
    stored = _db_card(handle.id)
    for card in (queried, stored):
        assert card["skip_reason"].startswith("Not enough evidence")
        for value in (quote, "424242"):
            assert value not in card["skip_reason"]
            assert value not in json.dumps(card["evidence"]["draft"])

    assert await refetch_stored_card_target(
        TargetClient(["gone"]), handle.id, "run1", review_is_running=_finished
    ) == "removed"
    dumped = json.dumps(_db_card(handle.id))
    for fragment in POST_FRAGMENTS:
        assert fragment not in dumped


def _insert_legacy_card(workflow_id, with_reply_fit):
    """A card in the shape stored before issue #9 (and, without reply_fit,
    before issue #4), with a post quote in every model-derived field."""
    from backend.database import ReviewCardDB, get_db

    evidence = {
        "post_id": TARGET_ID,
        "matched_topic": "reliability",
        "why_it_fits": "fits",
        "founder_materials": [{"source": "product_brief",
                               "passage": "Our product records work." if with_reply_fit else f"They said: {QUESTION}",
                               "relevance": "r", "passage_id": "passage_0"}],
        "draft": {
            "text": "Thanks for sharing." if with_reply_fit else f"They said: {QUESTION}",
            "segments": [{"kind": "non_claim", "text": f"As you said, {QUESTION}",
                          "source_passage_id": None, "sentence_index": None},
                         {"kind": f"quote: {QUESTION}", "text": None,
                          "source_passage_id": None, "sentence_index": None},
                         # a number copied from the post as an unchecked index
                         {"kind": "claim", "text": None,
                          "source_passage_id": "passage_0", "sentence_index": 424242},
                         # ...also next to a valid phrase's text
                         {"kind": "non_claim", "text": "Thanks for sharing.", "phrase_id": "thanks.for_sharing",
                          "source_passage_id": None, "sentence_index": 434343}],
            "claim_gate": {
                "passed": False,
                "segments": [{"kind": "non_claim", "text": f"As you said, {QUESTION}",
                              "source_passage_id": None, "sentence_index": None}],
                "failed_sentences": [
                    {"sentence": "[non_claim segment]", "reason": f"Unknown phrase_id: {QUESTION}"},
                ],
                "reasoning": f"Composition violation: Unknown phrase_id: {QUESTION}",
                "claims": [], "failed_claims": [],
            },
            "steers_applied": [
                "Make it shorter",
                f"Previous draft failed claim gate. Fix these issues: '[non_claim segment]...': Unknown phrase_id: {QUESTION}",
            ],
        },
        "skip_reason": f"Not enough evidence: [non_claim segment]... (Unknown phrase_id: {QUESTION})",
    }
    if with_reply_fit:
        evidence["reply_fit"] = {"post_kind": "question", "brief_problem_span": None, "span_check": "missing"}
    db = get_db()
    try:
        db.add(ReviewCardDB(
            workflow_id=workflow_id,
            post_data={"id": TARGET_ID, "text": "Struggling with distributed systems reliability.",
                       "author": {"id": "a", "username": "synthetic_dev", "name": "Synthetic Dev",
                                  "followers_count": 1, "verified": False}},
            evidence_data=evidence,
            status="skipped",
            created_at="2026-11-20T00:00:00+00:00",
            updated_at="2026-11-20T00:00:00+00:00",
            skip_reason=evidence["skip_reason"],
        ))
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_reply_fit", [True, False], ids=["after_issue_4", "before_issue_4"])
async def test_removal_scrubs_a_card_stored_before_issue_9(with_reply_fit):
    """Older cards can hold a post quote in echoed gate values (and, before
    issue #4, in unverified founder passages). A removal scrubs those by
    provenance and keeps founder text."""
    from backend.target_refetch import refetch_stored_card_target

    workflow_id = f"review:test:legacy_card_{with_reply_fit}"
    _insert_legacy_card(workflow_id, with_reply_fit)
    assert await refetch_stored_card_target(
        TargetClient(["gone"]), workflow_id, "run1", review_is_running=_finished
    ) == "removed"
    stored = _db_card(workflow_id)
    dumped = json.dumps(stored)
    for fragment in POST_FRAGMENTS:
        assert fragment not in dumped
    assert "424242" not in dumped
    assert "434343" not in dumped
    phrase_seg = stored["evidence"]["draft"]["segments"][-1]
    assert phrase_seg["text"] == "Thanks for sharing." and phrase_seg["phrase_id"] == "thanks.for_sharing"
    draft = stored["evidence"]["draft"]
    assert "Make it shorter" in draft["steers_applied"]  # founder nudge kept
    assert stored["skip_reason"].startswith("Not enough evidence:")
    if with_reply_fit:
        # Verified passages and the assembled draft stay
        assert stored["evidence"]["founder_materials"][0]["passage"] == "Our product records work."
        assert draft["text"] == "Thanks for sharing."


@pytest.mark.asyncio
async def test_update_on_a_removed_card_is_redacted_again():
    """A late update_card with stale evidence (an in-flight redraft, another
    Review's edit) cannot restore copies of a removed post."""
    from backend.activities.store_activities import UpdateCardInput, _update_card_sync
    from backend.models import ClaimGateResult, Draft, Evidence, Segment
    from backend.target_refetch import refetch_stored_card_target

    workflow_id = "review:test:late_update"
    _insert_legacy_card(workflow_id, with_reply_fit=True)
    assert await refetch_stored_card_target(
        TargetClient(["gone"]), workflow_id, "run1", review_is_running=_finished
    ) == "removed"

    stale_segments = [Segment(kind="non_claim", text=f"As you said, {QUESTION}")]
    stale = Evidence(
        post_id=TARGET_ID, matched_topic="t", why_it_fits="w", confidence_score=0.9,
        confidence_inputs={}, founder_materials=[],
        draft=Draft(
            text="Thanks for sharing.",
            segments=stale_segments,
            claim_gate=ClaimGateResult(
                passed=False, segments=stale_segments,
                failed_sentences=[("[non_claim segment]", f"Unknown phrase_id: {QUESTION}")],
                reasoning=f"Composition violation: {QUESTION}",
            ),
            steers_applied=[f"Previous draft failed claim gate. Fix: {QUESTION}"],
        ),
    )
    assert _update_card_sync(UpdateCardInput(workflow_id=workflow_id, idempotency_key="k", evidence=stale))
    dumped = json.dumps(_db_card(workflow_id))
    for fragment in POST_FRAGMENTS:
        assert fragment not in dumped

@pytest.mark.asyncio
async def test_eval_refetch_unknown_card():
    from backend.target_refetch import refetch_stored_card_target

    client = TargetClient(["found"])
    assert await refetch_stored_card_target(client, "review:test:nope", "run1", review_is_running=_finished) == "no_card"
    assert client.target_lookups == 0
