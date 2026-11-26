"""
A post removal applies to every account's Review of the same post (issue #22).

Two Reviews of the same post id under different accounts. When one finds the
post removed (a 404 at its approve's re-fetch), the other:
- stops at its next activity, with no model call, and ends as skipped
  "post removed" with its card stored redacted, whether it is researching,
  planning or drafting; this holds for a Review whose input carries the post
  (recorded before issue #23) as well as for a text-free one;
- makes no model call at all when it starts after the removal;
- at its own approve, ends as "post removed" without an X read.

Fake adapters and synthetic posts only; no X or OpenAI calls. The database
is the per-test temporary one (conftest), never the app's.
"""

import asyncio
import json
import uuid

import pytest
from temporalio import activity
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Replayer

from backend.activities.store_activities import (
    DeletePostTextInput,
    POST_REMOVED_SKIP_REASON,
    _delete_post_text_sync,
    get_all_cards,
)
from backend.activities.x_activities import LookupPostInput, XActivities
from backend.adapters import FakeReplyAgent
from backend.budget import get_today_budget
from backend.workflows.review import REMOVAL_BEFORE_DRAFT_PATCH_ID, ReviewWorkflow
from backend.tests.review_waits import WAIT_TIMEOUT_SEC, wait_for_card_ready
from backend.tests.test_target_refetch import (
    APPROVE_URL,
    POST_FRAGMENTS,
    TARGET_ID,
    QuotingAgent,
    TargetClient,
    _wait_for_status,
    _worker,
)


class CountingAgent(QuotingAgent):
    """Records each model call by the workflow that made it; can hold one
    workflow's call at a stage ("research" or "plan") until `release`."""

    def __init__(self, hold_workflow: str = "", hold_stage: str = ""):
        super().__init__()
        self.calls = []  # (workflow_id, stage)
        self.hold_workflow = hold_workflow
        self.hold_stage = hold_stage
        self.held = asyncio.Event()
        self.release = asyncio.Event()

    async def _record(self, stage):
        workflow_id = activity.info().workflow_id
        self.calls.append((workflow_id, stage))
        if workflow_id == self.hold_workflow and stage == self.hold_stage:
            self.held.set()
            await self.release.wait()

    def calls_of(self, workflow_id):
        return [stage for w, stage in self.calls if w == workflow_id]

    async def research(self, post, topic, product_brief, idempotency_key):
        await self._record("research")
        return await super().research(post, topic, product_brief, idempotency_key)

    async def plan(self, post, evidence, idempotency_key, feedback=None):
        await self._record("plan")
        return await super().plan(post, evidence, idempotency_key, feedback)

    async def draft(self, evidence, steer=None, idempotency_key=None, plan=None, max_chars=280):
        await self._record("draft")
        return await super().draft(evidence, steer, idempotency_key, plan)

    async def draft_written(self, request, idempotency_key=None):
        await self._record("draft")
        return await super().draft_written(request, idempotency_key)


def _stored(workflow_id):
    return next(c for c in get_all_cards() if c["workflow_id"] == workflow_id)


def _assert_removed_skip(card):
    assert card["status"] == "skipped", card
    assert card["skip_reason"] == POST_REMOVED_SKIP_REASON
    assert card["post"]["text_removed"] is True
    for fragment in POST_FRAGMENTS:
        assert fragment not in json.dumps(card)


async def _start(env, make_review_input, account, legacy):
    review_input = make_review_input(account=account, legacy=legacy)
    return await env.client.start_workflow(
        ReviewWorkflow.run,
        review_input,
        id=f"review:{account}:{review_input.post_id}",
        task_queue="test",
    )


async def _remove_through_approve(handle):
    """The first account approves; the re-fetch finds the post gone."""
    with pytest.raises(WorkflowUpdateFailedError) as failed:
        await handle.execute_update("approve", args=[APPROVE_URL, str(uuid.uuid4()), False])
    assert failed.value.cause.type == "PostRemoved"
    # Queried, not awaited: awaiting a result lets the time-skipping server
    # jump past the other Review's review timeout
    await _wait_for_status(handle, "skipped")


# ---------- acceptance: the other Review stops at its next activity ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True], ids=["text_free", "post_in_input"])
@pytest.mark.parametrize("stage", ["research", "plan"])
async def test_a_removal_found_by_one_account_stops_the_other_at_its_next_activity(
    make_review_input, legacy, stage
):
    """Account b's Review is inside its `stage` model call when account a's
    approve finds the post gone. That call was already sent; b's next
    activity makes no model call, and b ends as skipped "post removed"."""
    other_id = f"review:b:{TARGET_ID}"
    agent = CountingAgent(hold_workflow=other_id, hold_stage=stage)
    client = TargetClient(["gone"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client, agent):
            first = await _start(env, make_review_input, "a", legacy)
            assert (await wait_for_card_ready(first))["status"] == "pending"
            other = await _start(env, make_review_input, "b", legacy)
            await asyncio.wait_for(agent.held.wait(), WAIT_TIMEOUT_SEC)

            await _remove_through_approve(first)
            agent.release.set()

            assert await other.result() == "skipped"
            # The held call is the last one: nothing after the removal
            assert agent.calls_of(other_id) == ["research", "plan"][: ["research", "plan"].index(stage) + 1]
            _assert_removed_skip(_stored(other_id))
            assert client.target_lookups == 1  # b never approved


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True], ids=["text_free", "post_in_input"])
async def test_a_review_started_after_the_removal_makes_no_model_call(make_review_input, legacy):
    agent = CountingAgent()
    client = TargetClient(["gone"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client, agent):
            first = await _start(env, make_review_input, "a", legacy)
            await wait_for_card_ready(first)
            await _remove_through_approve(first)

            other = await _start(env, make_review_input, "b", legacy)
            assert await other.result() == "skipped"
            assert agent.calls_of(other.id) == []
            _assert_removed_skip(_stored(other.id))

            if legacy:
                # issue #22's patch: the new branch is marked, and replays
                history = await other.fetch_history()
                markers = [
                    e.marker_recorded_event_attributes
                    for e in history.events
                    if e.HasField("marker_recorded_event_attributes")
                ]
                assert any(
                    REMOVAL_BEFORE_DRAFT_PATCH_ID in str(m.details) for m in markers
                ), "patch marker missing"
                await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)


# ---------- acceptance: approve after a removal recorded elsewhere ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True], ids=["text_free", "post_in_input"])
async def test_approve_of_a_post_removed_elsewhere_reads_nothing_from_x(make_review_input, legacy):
    """Both accounts' Reviews are pending. After a's approve found the post
    gone, b's approve ends b as "post removed" without asking X again."""
    agent = CountingAgent()
    client = TargetClient(["gone", "found"])  # a second X lookup would find it
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, client, agent):
            first = await _start(env, make_review_input, "a", legacy)
            other = await _start(env, make_review_input, "b", legacy)
            await wait_for_card_ready(first)
            await wait_for_card_ready(other)
            await _remove_through_approve(first)
            assert client.target_lookups == 1
            reads_before = get_today_budget()["x_reads"]

            await _remove_through_approve(other)

            assert client.target_lookups == 1
            assert get_today_budget()["x_reads"] == reads_before
            _assert_removed_skip(_stored(other.id))
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(await other.fetch_history())


# ---------- the activity: a recorded removal answers the lookup ----------


@pytest.mark.asyncio
async def test_lookup_post_answers_a_recorded_removal_without_x_or_budget(sample_post):
    """A removal recorded only on the stored post (no card yet) still
    counts: lookup_post returns None and reserves nothing."""
    from backend.review_docs import store_posts

    store_posts([sample_post])
    _delete_post_text_sync(DeletePostTextInput(
        workflow_id="review:none:x", idempotency_key="k", post_id=sample_post.id,
    ))
    client = TargetClient(["found"])
    reads_before = get_today_budget()["x_reads"]

    result = await ActivityEnvironment().run(
        XActivities(client).lookup_post, LookupPostInput(post_id=sample_post.id, idempotency_key="k")
    )

    assert result is None
    assert client.target_lookups == 0
    assert get_today_budget()["x_reads"] == reads_before
