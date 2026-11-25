"""
Review API errors (issue #15) and requests that arrive before the card is
stored (issue #20).

- A rejected update reaches the client as JSON {"code", "detail", ...}: 409
  "post_anyway_required" with the failing sentences, 409 "not_ready" before
  the card is stored, 400 "invalid_request", 404 "not_found"; 500 only for
  failures that are not the Review's own decision.
- Approve, edit and nudge are rejected as not ready until the pending card is
  stored; an edit then is no longer lost (its card write found no row).
- GET /api/reviews/{id} reports the in-progress stage instead of 404.
- A Review recorded on main f9d58ec with an edit accepted before the card was
  stored replays against the current code.

Fake adapters and synthetic posts only; the API runs in-process over
httpx's ASGI transport against the time-skipping test server.
"""

import asyncio
import json
import uuid
from pathlib import Path

import httpx
import pytest
from temporalio import activity
from temporalio.client import WorkflowHistory, WorkflowUpdateFailedError
from temporalio.exceptions import ActivityError, ApplicationError, RetryState
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

import backend.api as api
from backend.activities.claim_gate_activity import claim_gate, verify_edited_text
from backend.activities.reply_activities import ReplyActivities
from backend.activities.store_activities import (
    StoreCardInput,
    get_all_cards,
    store_card,
    update_card,
)
from backend.adapters import FakeReplyAgent
from backend.tests.review_waits import WAIT_TIMEOUT_SEC, wait_for_card_ready
from backend.workflows.review import ReviewWorkflow

FIXTURES = Path(__file__).parent / "fixtures"
APPROVE_URL = "https://x.com/test/status/999"
# Not a passage sentence or a fixed phrase: fails the edit re-gate
FAILING_EDIT = "Thanks for the question. Works on Kubernetes. Happy to help."
PASSING_EDIT = "Thanks! Our product helps developers build reliable distributed systems. Happy to help."


class HeldResearch(FakeReplyAgent):
    """Research that waits for the test, so the Review stays "researching"."""

    def __init__(self):
        self.release = asyncio.Event()

    async def research(self, post, topic, product_brief, idempotency_key):
        await self.release.wait()
        return await super().research(post, topic, product_brief, idempotency_key)


class HeldPendingStore:
    """store_card that holds the pending card's write until the test releases
    it: the draft exists in the workflow, the card row does not yet."""

    def __init__(self):
        self.called = asyncio.Event()
        self.release = asyncio.Event()

    def activity(self):
        @activity.defn(name="store_card")
        async def held_store_card(input: StoreCardInput) -> bool:
            if input.idempotency_key.endswith(":store:pending"):
                self.called.set()
                await self.release.wait()
            return await store_card(input)

        return held_store_card


def _worker(env, x_activities, agent=None, store=store_card):
    reply_activities = ReplyActivities(agent or FakeReplyAgent())
    return Worker(
        env.client,
        task_queue="test",
        workflows=[ReviewWorkflow],
        activities=[
            x_activities.lookup_post,
            x_activities.check_followup,
            reply_activities.research_post,
            reply_activities.plan_reply,
            reply_activities.draft_reply,
            claim_gate,
            verify_edited_text,
            store,
            update_card,
        ],
    )


@pytest.fixture
def api_client(monkeypatch):
    """The FastAPI app over ASGI (its lifespan, which connects to Temporal and
    the database, does not run); each test points it at its test server."""
    def _make(temporal_client):
        monkeypatch.setattr(api, "temporal_client", temporal_client)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test")

    return _make


async def _post(http, workflow_id, action, **body):
    return await http.post(f"/api/reviews/{workflow_id}/{action}", json={"client_request_id": str(uuid.uuid4()), **body})


def _db_card(workflow_id):
    return next(c for c in get_all_cards() if c["workflow_id"] == workflow_id)


# ---------- issue #15: post_anyway_required reaches the UI ----------

@pytest.mark.asyncio
async def test_approve_after_failed_edit_is_409_post_anyway_required(make_review_input, x_activities, api_client):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities), api_client(env.client) as http:
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post_id}_api_post_anyway"
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test",
            )
            await wait_for_card_ready(handle)

            edited = await _post(http, workflow_id, "edit", new_text=FAILING_EDIT)
            assert edited.status_code == 200, edited.text

            shown = {"draft_text": FAILING_EDIT, "draft_gate_state": "failed"}
            rejected = await _post(http, workflow_id, "approve", posted_url=APPROVE_URL, post_anyway=False, **shown)
            assert rejected.status_code == 409
            body = rejected.json()
            assert body["code"] == "post_anyway_required"
            assert body["gate_state"] == "failed"
            # "Happy to help." is a fixed phrase and passes; the other two fail
            assert body["failing_sentences"] == ["Thanks for the question.", "Works on Kubernetes."]
            assert "post_anyway=true" in body["detail"]

            approved = await _post(http, workflow_id, "approve", posted_url=APPROVE_URL, post_anyway=True, **shown)
            assert approved.status_code == 200, approved.text
            assert approved.json() == {"message": "Approved"}


@pytest.mark.asyncio
async def test_approve_naming_a_draft_that_is_no_longer_current_is_409_draft_changed(
    make_review_input, x_activities, api_client
):
    """The founder approves the draft they saw (ADR-0004); an edit from
    elsewhere since then must not be approved, or overridden, unseen."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities), api_client(env.client) as http:
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post_id}_api_draft_changed"
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test",
            )
            shown = (await wait_for_card_ready(handle))["evidence"]["draft"]["text"]
            # Another tab edits the draft to text that fails the re-gate
            assert (await _post(http, workflow_id, "edit", new_text=FAILING_EDIT)).status_code == 200

            # The old draft as shown, and the new text with a gate result the
            # founder did not see (same text, re-gated: "not_checked" -> "failed")
            for seen in ({"draft_text": shown, "draft_gate_state": None},
                         {"draft_text": FAILING_EDIT, "draft_gate_state": "not_checked"}):
                for post_anyway in (False, True):
                    stale = await _post(
                        http, workflow_id, "approve", posted_url=APPROVE_URL, post_anyway=post_anyway, **seen,
                    )
                    assert stale.status_code == 409, (seen, stale.text)
                    assert stale.json()["code"] == "draft_changed"

            current = await _post(
                http, workflow_id, "approve", posted_url=APPROVE_URL, post_anyway=True,
                draft_text=FAILING_EDIT, draft_gate_state="failed",
            )
            assert current.status_code == 200, current.text


# ---------- issue #20: requests before the card is stored ----------

@pytest.mark.asyncio
async def test_requests_during_research_are_not_ready_and_get_reports_the_stage(
    make_review_input, x_activities, api_client
):
    agent = HeldResearch()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, agent), api_client(env.client) as http:
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post_id}_api_researching"
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test",
            )

            got = await http.get(f"/api/reviews/{workflow_id}")
            assert got.status_code == 200, got.text
            assert got.json() == {
                "workflow_id": workflow_id,
                "status": "researching",
                "in_progress": True,
                "created_at": got.json()["created_at"],
                "evidence": None,
            }

            for action, body in [
                ("approve", {"posted_url": APPROVE_URL, "post_anyway": False, "draft_text": "", "draft_gate_state": None}),
                ("edit", {"new_text": PASSING_EDIT}),
                ("nudge", {"instruction": "Make it shorter"}),
            ]:
                response = await _post(http, workflow_id, action, **body)
                assert response.status_code == 409, (action, response.text)
                assert response.json()["code"] == "not_ready", action

            agent.release.set()
            card = await wait_for_card_ready(handle)
            assert card["status"] == "pending"
            assert card["evidence"]["draft"]["text"]


@pytest.mark.asyncio
async def test_founder_skip_during_research_shows_no_post_until_the_card_is_stored(
    make_review_input, x_activities, api_client
):
    """A founder skip during research does not end the workflow: it still
    drafts and stores the card. Until that row exists the query shows no post
    or evidence, since a removal recorded by another Review of the post can be
    applied only against the stored row (ADR-0006 removal duty)."""
    agent = HeldResearch()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, agent), api_client(env.client) as http:
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post_id}_api_skip_researching"
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test",
            )
            skipped = await _post(http, workflow_id, "skip", reason="Not relevant")
            assert skipped.status_code == 200, skipped.text

            got = (await http.get(f"/api/reviews/{workflow_id}")).json()
            assert got["status"] == "skipped" and got["in_progress"] is True
            # No post, evidence or skip reason until the row exists (a
            # gate-failure skip reason quotes model-written sentences)
            assert got["evidence"] is None and "post" not in got and "skip_reason" not in got

            agent.release.set()
            assert await handle.result() == "skipped"
            got = (await http.get(f"/api/reviews/{workflow_id}")).json()
            assert got["status"] == "skipped" and "in_progress" not in got
            assert got["post"]["id"] == review_input.post_id
            assert _db_card(workflow_id)["status"] == "skipped"


@pytest.mark.asyncio
async def test_malformed_body_and_missing_temporal_client_have_codes(api_client):
    async with api_client(None) as http:
        response = await http.post("/api/reviews/review:test:any/approve", json={"posted_url": APPROVE_URL})
        assert (response.status_code, response.json()["code"]) == (422, "invalid_request")
        assert response.json()["errors"]

        response = await _post(http, "review:test:any", "edit", new_text="Synthetic.")
        assert (response.status_code, response.json()["code"]) == (500, "server_error")
        assert response.json()["detail"] == "Temporal client not connected"


@pytest.mark.asyncio
async def test_edit_before_the_card_is_stored_is_rejected_then_saved(make_review_input, x_activities, api_client):
    """issue #20 item 2: the draft exists but store_card has not written the
    row. main accepted the edit and its update_card found no row, so the
    stored card kept the unedited draft (fixture recorded below)."""
    store = HeldPendingStore()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, store=store.activity()), api_client(env.client) as http:
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post_id}_api_edit_before_store"
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test",
            )
            await asyncio.wait_for(store.called.wait(), WAIT_TIMEOUT_SEC)

            got = (await http.get(f"/api/reviews/{workflow_id}")).json()
            assert got["status"] == "drafting" and got["in_progress"] is True

            for action, body in [
                ("edit", {"new_text": PASSING_EDIT}),
                ("nudge", {"instruction": "Make it shorter"}),
                ("approve", {"posted_url": APPROVE_URL, "post_anyway": False, "draft_text": "", "draft_gate_state": None}),
            ]:
                response = await _post(http, workflow_id, action, **body)
                assert response.status_code == 409, (action, response.text)
                assert response.json()["code"] == "not_ready", action
                assert "before the draft card is ready" in response.json()["detail"]

            store.release.set()
            await wait_for_card_ready(handle)
            edited = await _post(http, workflow_id, "edit", new_text=PASSING_EDIT)
            assert edited.status_code == 200, edited.text
            assert _db_card(workflow_id)["evidence"]["draft"]["text"] == PASSING_EDIT
            got = (await http.get(f"/api/reviews/{workflow_id}")).json()
            assert got["status"] == "pending" and "in_progress" not in got


@pytest.mark.asyncio
async def test_invalid_request_invalid_state_and_unknown_review(make_review_input, x_activities, api_client):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities), api_client(env.client) as http:
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post_id}_api_codes"
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test",
            )
            shown = (await wait_for_card_ready(handle))["evidence"]["draft"]["text"]

            empty = await _post(http, workflow_id, "nudge", instruction="   ")
            assert (empty.status_code, empty.json()["code"]) == (400, "invalid_request")

            early = await _post(http, workflow_id, "confirm_posted", reply_url="https://x.com/me/status/1005")
            assert (early.status_code, early.json()["code"]) == (409, "invalid_state")

            # The draft the founder saw is required: omitted or null is a malformed body
            # (draft_gate_state too; it may be null for a draft never edited)
            for body in ({}, {"draft_text": None, "draft_gate_state": None}, {"draft_text": shown}):
                bad = await _post(http, workflow_id, "approve", posted_url=APPROVE_URL, **body)
                assert (bad.status_code, bad.json()["code"]) == (422, "invalid_request"), body
            approved = await _post(
                http, workflow_id, "approve", posted_url=APPROVE_URL, draft_text=shown, draft_gate_state=None,
            )
            assert approved.status_code == 200, approved.text
            bad_url = await _post(http, workflow_id, "confirm_posted", reply_url="https://x.com/me")
            assert (bad_url.status_code, bad_url.json()["code"]) == (400, "invalid_request")
            # A reply that is not a reply to the target: refused by the handler
            not_reply = await _post(http, workflow_id, "confirm_posted", reply_url="https://x.com/me/status/1002")
            assert (not_reply.status_code, not_reply.json()["code"]) == (422, "rejected")
            assert "is not a reply to target post" in not_reply.json()["detail"]

            missing = "review:test:no_such_review"
            got = await http.get(f"/api/reviews/{missing}")
            assert (got.status_code, got.json()["code"]) == (404, "not_found")
            approve_missing = await _post(
                http, missing, "approve", posted_url=APPROVE_URL, draft_text="", draft_gate_state=None,
            )
            assert (approve_missing.status_code, approve_missing.json()["code"]) == (404, "not_found")


# ---------- error mapping (unit) ----------

def _body(response):
    return response.status_code, json.loads(response.body)


def test_not_checked_gate_is_post_anyway_required_with_its_state():
    cause = ApplicationError(
        "Edited text gate check failed (retries exhausted). Set post_anyway=true to approve anyway.",
        {"gate_state": "not_checked", "failing_sentences": []},
        type="PostAnywayRequired",
    )
    status, body = _body(api.review_error_response(WorkflowUpdateFailedError(cause), "Approve"))
    assert status == 409
    assert body == {
        "code": "post_anyway_required",
        "detail": "Edited text gate check failed (retries exhausted). Set post_anyway=true to approve anyway.",
        "gate_state": "not_checked",
        "failing_sentences": [],
    }


@pytest.mark.parametrize("error_type,expected", [
    ("NotReady", (409, "not_ready")),
    ("Busy", (409, "busy")),
    ("PostRemoved", (409, "post_removed")),
    ("DraftChanged", (409, "draft_changed")),
    ("InvalidState", (409, "invalid_state")),
    ("InvalidRequest", (400, "invalid_request")),
    (None, (422, "rejected")),  # handler-body refusals (nudge gate, reply check)
])
def test_rejection_types_map_to_status_and_code(error_type, expected):
    cause = ApplicationError("synthetic reason", type=error_type, non_retryable=True)
    status, body = _body(api.review_error_response(WorkflowUpdateFailedError(cause), "Nudge"))
    assert (status, body["code"]) == expected
    assert body["detail"] == "synthetic reason"


def _activity_failure(cause=None):
    error = ActivityError(
        "Activity task failed", scheduled_event_id=1, started_event_id=2, identity="",
        activity_type="draft_reply", activity_id="1", retry_state=RetryState.NON_RETRYABLE_FAILURE,
    )
    error.__cause__ = cause
    return error


def test_a_nudge_refused_because_the_post_was_removed_is_409_post_removed():
    """draft_reply refuses to redraft a card whose post another Review found
    removed (issue #9); the refusal arrives wrapped in ActivityError."""
    refusal = ApplicationError("Post removed: not redrafting", type="PostRemoved", non_retryable=True)
    status, body = _body(api.review_error_response(WorkflowUpdateFailedError(_activity_failure(refusal)), "Nudge"))
    assert (status, body["code"], body["detail"]) == (409, "post_removed", "Post removed: not redrafting")


def test_failures_that_are_not_a_decision_are_500():
    for exc in [
        WorkflowUpdateFailedError(_activity_failure()),
        # Any other activity failure, even one that is an ApplicationError
        WorkflowUpdateFailedError(_activity_failure(ApplicationError("model unavailable", type="Busy"))),
        RPCError("connection refused", RPCStatusCode.UNAVAILABLE, b""),
        RuntimeError("unexpected"),
    ]:
        status, body = _body(api.review_error_response(exc, "Approve"))
        assert (status, body["code"]) == (500, "server_error"), exc


# ---------- replay safety ----------

# A Review recorded on main f9d58ec in which an edit was accepted while the
# pending store_card was still running (issue #20 item 2): the edit's
# update_card found no row. Synthetic data; identities scrubbed.
EDIT_BEFORE_STORE_HISTORY = (
    "review_history_main_f9d58ec_edit_before_store.json",
    "review:test:5120_main_f9d58ec_edit_before_store",
)


def test_fixture_has_an_edit_accepted_before_the_card_was_stored():
    events = json.loads((FIXTURES / EDIT_BEFORE_STORE_HISTORY[0]).read_text())["events"]
    store_id = next(
        int(e["eventId"]) for e in events
        if e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"
        and e["activityTaskScheduledEventAttributes"]["activityType"]["name"] == "store_card"
    )
    store_done = next(
        int(e["eventId"]) for e in events
        if e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_COMPLETED"
        and int(e["activityTaskCompletedEventAttributes"]["scheduledEventId"]) == store_id
    )
    edit_accepted = next(
        int(e["eventId"]) for e in events
        if e["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED"
        and e["workflowExecutionUpdateAcceptedEventAttributes"]["acceptedRequest"]["input"]["name"] == "edit"
    )
    assert store_id < edit_accepted < store_done


@pytest.mark.asyncio
async def test_review_with_an_edit_accepted_before_the_card_was_stored_replays():
    """The current validator would reject that edit as not ready. Validators do
    not run when an accepted update replays, so the history replays and the
    workflow keeps the edit it accepted then."""
    fixture, workflow_id = EDIT_BEFORE_STORE_HISTORY
    history = WorkflowHistory.from_json(workflow_id, (FIXTURES / fixture).read_text())
    await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)
