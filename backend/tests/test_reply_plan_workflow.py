"""
Workflow tests for the reply plan step (issue #5).

- the plan is produced, stored on the card (query and DB) and passed to draft()
- an invalid plan is re-requested once with the errors, then the post is skipped
- mention_product=False yields a claim-less assembled draft that passes the
  unchanged claim gate
- Review histories recorded before the plan step replay without error

Uses fake adapters and synthetic posts only.
"""

import json
import uuid
from pathlib import Path
from typing import List, Optional

import pytest
from temporalio.client import WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from backend.activities.claim_gate_activity import claim_gate, verify_edited_text
from backend.activities.reply_activities import ReplyActivities
from backend.activities.store_activities import get_all_cards, store_card, update_card
from backend.adapters import FakeReplyAgent
from backend.models import ReplyPlan
from backend.workflows.review import REPLY_PLAN_PATCH_ID, ReviewWorkflow

FIXTURES = Path(__file__).parent / "fixtures"
QUESTION = "How do you folks handle eventual consistency?"  # from conftest sample_post


class RecordingAgent(FakeReplyAgent):
    """Fake agent that can script plans and records what draft() received."""

    def __init__(self, plans: Optional[List[ReplyPlan]] = None):
        self._plans = list(plans or [])
        self.plan_calls: List[dict] = []
        self.draft_plans: List[Optional[ReplyPlan]] = []

    async def plan(self, post, evidence, idempotency_key, feedback=None):
        self.plan_calls.append({"key": idempotency_key, "feedback": feedback})
        if self._plans:
            return self._plans.pop(0)
        return await super().plan(post, evidence, idempotency_key, feedback)

    async def draft(self, evidence, steer=None, idempotency_key=None, plan=None):
        self.draft_plans.append(plan)
        return await super().draft(evidence, steer, idempotency_key, plan)


def _worker(env, x_activities, agent):
    reply_activities = ReplyActivities(agent)
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
            store_card,
            update_card,
        ],
    )


def _db_card(workflow_id):
    return next(c for c in get_all_cards() if c["workflow_id"] == workflow_id)


@pytest.mark.asyncio
async def test_plan_is_produced_stored_and_passed_to_draft(make_review_input, x_activities):
    agent = RecordingAgent()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, agent):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_plan"
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test"
            )
            await env.sleep(1)

            card = await handle.query("get_card")
            assert card["status"] == "pending"
            plan = card["evidence"]["reply_plan"]
            assert plan["reply_type"] == "answer_question"
            assert plan["target_point"] == QUESTION
            assert plan["mention_product"] is True
            assert plan["mention_product_reason"]
            assert card["evidence"]["reply_plan_errors"] == []
            assert card["evidence"]["reply_plan_attempts"] == 1

            # Stored card (what the list API serves) carries the same plan
            assert _db_card(workflow_id)["evidence"]["reply_plan"] == plan

            # draft() received the validated plan
            assert len(agent.plan_calls) == 1
            assert agent.plan_calls[0]["feedback"] is None
            assert len(agent.draft_plans) == 1
            assert agent.draft_plans[0].target_point == QUESTION
            assert agent.draft_plans[0].reply_type == "answer_question"

            # A nudge redraft is steered by the same plan
            await handle.execute_update("nudge", args=["Make it shorter", str(uuid.uuid4())])
            assert agent.draft_plans[-1].target_point == QUESTION

            history = await handle.fetch_history()
            markers = [e for e in history.events if e.HasField("marker_recorded_event_attributes")]
            assert markers, "plan step must record its patch marker"

            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_invalid_plan_is_rerequested_once_with_errors(make_review_input, x_activities):
    bad = ReplyPlan(
        reply_type="answer_question",
        target_point="How do you handle consistency?",  # paraphrase, not in the post
        mention_product=True,
        mention_product_reason="Material covers consistency.",
    )
    agent = RecordingAgent(plans=[bad])  # second call falls back to the valid fake plan
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, agent):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_plan_retry"
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test"
            )
            await env.sleep(1)

            card = await handle.query("get_card")
            assert card["status"] == "pending"
            assert card["evidence"]["reply_plan_attempts"] == 2
            assert card["evidence"]["reply_plan_errors"] == []
            assert card["evidence"]["reply_plan"]["target_point"] == QUESTION

            assert len(agent.plan_calls) == 2
            assert agent.plan_calls[1]["key"].endswith(":plan:attempt2")
            assert any("not an exact substring" in f for f in agent.plan_calls[1]["feedback"])
            assert agent.draft_plans[0].target_point == QUESTION


@pytest.mark.asyncio
async def test_invalid_plan_twice_skips_with_reason(make_review_input, x_activities):
    not_in_post = ReplyPlan(
        reply_type="add_observation",
        target_point="consistency is hard",
        mention_product=True,
        mention_product_reason="Material covers consistency.",
    )
    answers_a_statement = ReplyPlan(
        reply_type="answer_question",
        target_point="Struggling with distributed systems reliability.",
        mention_product=True,
        mention_product_reason="Material covers reliability.",
    )
    agent = RecordingAgent(plans=[not_in_post, answers_a_statement])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, agent):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_plan_skip"
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test"
            )
            assert await handle.result() == "skipped"

            assert len(agent.plan_calls) == 2
            assert agent.draft_plans == [], "draft() must not run after an invalid plan"

            card = await handle.query("get_card")
            assert card["status"] == "skipped"
            assert card["skip_reason"].startswith("Reply plan invalid after 2 attempts")
            assert "must be the question being answered" in card["skip_reason"]
            assert card["evidence"]["draft"] is None
            assert card["evidence"]["reply_plan"]["target_point"] == answers_a_statement.target_point
            assert card["evidence"]["reply_plan_errors"]

            stored = _db_card(workflow_id)
            assert stored["status"] == "skipped"
            assert stored["skip_reason"] == card["skip_reason"]
            assert stored["evidence"]["reply_plan_errors"] == card["evidence"]["reply_plan_errors"]


@pytest.mark.asyncio
async def test_no_product_mention_gives_claimless_draft_that_passes_gate(make_review_input, x_activities):
    no_mention = ReplyPlan(
        reply_type="add_observation",
        target_point="Struggling with distributed systems reliability.",
        mention_product=False,
        mention_product_reason="They are venting, not asking for a tool.",
    )
    agent = RecordingAgent(plans=[no_mention])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, agent):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_plan_nomention"
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test"
            )
            await env.sleep(1)

            card = await handle.query("get_card")
            assert card["status"] == "pending"
            draft = card["evidence"]["draft"]
            assert draft["claim_gate"]["passed"] is True
            assert all(s["kind"] == "non_claim" for s in draft["segments"])
            # add_observation closes with a question in the fake drafter
            assert draft["text"] == "Thanks! What are you using today?"
            assert card["evidence"]["reply_plan"]["mention_product"] is False


def _pre_plan_history() -> WorkflowHistory:
    path = FIXTURES / "review_history_pre_reply_plan.json"
    return WorkflowHistory.from_json("review:test:5001_pre_plan", path.read_text())


def test_pre_plan_fixture_has_no_plan_step():
    events = json.loads((FIXTURES / "review_history_pre_reply_plan.json").read_text())["events"]
    scheduled = [
        e["activityTaskScheduledEventAttributes"]["activityType"]["name"]
        for e in events
        if e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"
    ]
    # Recorded on main (4581d99): research, draft, gate, store, then a nudge redraft
    assert scheduled[:4] == ["research_post", "draft_reply", "claim_gate", "store_card"]
    assert "plan_reply" not in scheduled


@pytest.mark.asyncio
async def test_review_started_before_plan_step_replays():
    """An in-flight Review recorded before the plan step (waiting for review, then
    nudged) replays against the current code without a nondeterminism error."""
    await Replayer(workflows=[ReviewWorkflow]).replay_workflow(_pre_plan_history())


@pytest.mark.asyncio
async def test_pre_plan_history_fails_replay_without_patch_guard(monkeypatch):
    """Control: if the plan step ran unconditionally, the old history would not
    replay. This shows the replay test above exercises the patch guard."""
    from temporalio import workflow as temporal_workflow

    real_patched = temporal_workflow.patched

    def always_patched(patch_id: str) -> bool:
        if patch_id == REPLY_PLAN_PATCH_ID:
            return True
        return real_patched(patch_id)

    monkeypatch.setattr(temporal_workflow, "patched", always_patched)
    result = await Replayer(workflows=[ReviewWorkflow]).replay_workflow(
        _pre_plan_history(), raise_on_replay_failure=False
    )
    assert result.replay_failure is not None
    assert "Nondeterminism" in str(result.replay_failure)
    assert "plan_reply" in str(result.replay_failure)


# ---------- draft must follow the plan (Codex round 1) ----------

NO_MENTION = ReplyPlan(
    reply_type="add_observation",
    target_point="Struggling with distributed systems reliability.",
    mention_product=False,
    mention_product_reason="They are venting, not asking for a tool.",
)


class DisobeyingAgent(RecordingAgent):
    """Ignores the plan's product decision for the first `disobey` drafts."""

    def __init__(self, plans, disobey: int, include_claim: bool):
        super().__init__(plans=plans)
        self.disobey = disobey
        self.include_claim = include_claim
        self.steers: List[Optional[str]] = []

    async def draft(self, evidence, steer=None, idempotency_key=None, plan=None):
        self.steers.append(steer)
        self.draft_plans.append(plan)
        if len(self.draft_plans) <= self.disobey:
            # Draft as if the plan said the opposite about the product
            flipped = ReplyPlan(
                reply_type=plan.reply_type,
                target_point=plan.target_point,
                mention_product=self.include_claim,
                mention_product_reason=plan.mention_product_reason,
            )
            return await FakeReplyAgent.draft(self, evidence, steer, idempotency_key, flipped)
        return await FakeReplyAgent.draft(self, evidence, steer, idempotency_key, plan)


@pytest.mark.asyncio
async def test_draft_with_forbidden_claim_is_redrafted_with_reason(make_review_input, x_activities):
    agent = DisobeyingAgent(plans=[NO_MENTION], disobey=1, include_claim=True)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, agent):
            review_input = make_review_input()
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input,
                id=f"review:test:{review_input.post.id}_plan_redraft", task_queue="test",
            )
            await env.sleep(1)

            card = await handle.query("get_card")
            assert card["status"] == "pending"
            assert len(agent.draft_plans) == 2
            assert agent.steers[0] is None
            assert "not to mention the product" in agent.steers[1]
            assert all(s["kind"] == "non_claim" for s in card["evidence"]["draft"]["segments"])
            assert card["evidence"]["draft"]["claim_gate"]["passed"] is True


@pytest.mark.asyncio
async def test_draft_that_keeps_ignoring_the_plan_is_skipped(make_review_input, x_activities):
    agent = DisobeyingAgent(plans=[NO_MENTION], disobey=2, include_claim=True)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, agent):
            review_input = make_review_input()
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input,
                id=f"review:test:{review_input.post.id}_plan_disobey", task_queue="test",
            )
            assert await handle.result() == "skipped"

            card = await handle.query("get_card")
            assert "reply plan says not to mention the product" in card["skip_reason"]
            draft = card["evidence"]["draft"]
            # Codex round 4: the claim gate verdict stays truthful (the claim was
            # a valid founder sentence); the plan failure is recorded separately
            assert draft["claim_gate"]["passed"] is True
            assert draft["plan_conformance_failures"] == [
                "the reply plan says not to mention the product, but the draft has a claim segment"
            ]
            assert _db_card(handle.id)["evidence"]["draft"]["plan_conformance_failures"] == draft["plan_conformance_failures"]
            assert len(agent.draft_plans) == 2


@pytest.mark.asyncio
async def test_draft_missing_planned_claim_is_skipped(make_review_input, x_activities):
    mention = ReplyPlan(
        reply_type="answer_question",
        target_point=QUESTION,
        mention_product=True,
        mention_product_reason="The brief covers consistency.",
    )
    agent = DisobeyingAgent(plans=[mention], disobey=2, include_claim=False)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, agent):
            review_input = make_review_input()
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input,
                id=f"review:test:{review_input.post.id}_plan_noclaim", task_queue="test",
            )
            assert await handle.result() == "skipped"
            card = await handle.query("get_card")
            assert "draft has no claim segment" in card["skip_reason"]


# ---------- pre-plan workflow receiving updates under the new code ----------

@pytest.mark.asyncio
async def test_pre_plan_workflow_handles_nudge_edit_approve_without_plan(
    make_review_input, x_activities, monkeypatch
):
    """Simulates a Review started before the plan step (patch not taken) that
    receives nudge, edit and approve after the new code is deployed."""
    from temporalio import workflow as temporal_workflow

    real_patched = temporal_workflow.patched
    monkeypatch.setattr(
        temporal_workflow,
        "patched",
        lambda patch_id: False if patch_id == REPLY_PLAN_PATCH_ID else real_patched(patch_id),
    )
    agent = RecordingAgent()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, agent):
            review_input = make_review_input()
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input,
                id=f"review:test:{review_input.post.id}_preplan_updates", task_queue="test",
            )
            await env.sleep(1)
            card = await handle.query("get_card")
            assert card["status"] == "pending"
            assert card["evidence"]["reply_plan"] is None
            assert agent.plan_calls == []

            await handle.execute_update("nudge", args=["Make it shorter", str(uuid.uuid4())])
            edited = await handle.execute_update(
                "edit", args=["Thanks for sharing! What stack are you on?", str(uuid.uuid4())]
            )
            assert edited == "Thanks for sharing! What stack are you on?"
            await handle.execute_update(
                "approve", args=["https://x.com/test/status/999", str(uuid.uuid4()), False]
            )
            card = await handle.query("get_card")
            assert card["status"] == "approved"
            assert agent.draft_plans and all(p is None for p in agent.draft_plans)


# ---------- removal duty (ADR-0006): deleting post text also removes the copied span ----------

@pytest.mark.asyncio
async def test_delete_post_text_removes_plan_target_point(make_review_input, x_activities):
    from backend.activities.store_activities import DeletePostTextInput, _delete_post_text_sync

    agent = RecordingAgent()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, agent):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_plan_delete"
            await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test"
            )
            await env.sleep(1)
            assert _db_card(workflow_id)["evidence"]["reply_plan"]["target_point"] == QUESTION

    assert _delete_post_text_sync(DeletePostTextInput(workflow_id=workflow_id, idempotency_key="k"))
    stored = _db_card(workflow_id)
    assert stored["post"]["text"] == "[removed - post deleted]"
    assert stored["evidence"]["reply_plan"]["target_point"] == "[removed - post deleted]"
    assert review_input.post.text not in str(stored)


# ---------- nudge under a plan is checked (Codex round 2) ----------

@pytest.mark.asyncio
async def test_nudge_that_breaks_the_plan_is_rejected_and_draft_kept(make_review_input, x_activities):
    from temporalio.client import WorkflowUpdateFailedError

    # The first draft follows the plan; every nudge redraft adds a claim.
    class NudgeDisobeys(DisobeyingAgent):
        async def draft(self, evidence, steer=None, idempotency_key=None, plan=None):
            self.disobey = 0 if not self.draft_plans else 99
            return await super().draft(evidence, steer, idempotency_key, plan)

    agent = NudgeDisobeys(plans=[NO_MENTION], disobey=0, include_claim=True)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, agent):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_plan_nudge_reject"
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test"
            )
            await env.sleep(1)
            before = await handle.query("get_card")
            assert before["evidence"]["draft"]["text"] == "Thanks! What are you using today?"

            request_id = str(uuid.uuid4())
            with pytest.raises(WorkflowUpdateFailedError) as excinfo:
                await handle.execute_update("nudge", args=["Mention the product", request_id])
            assert "not to mention the product" in str(excinfo.value.cause)

            # Codex round 3: retrying the same request id runs the nudge again
            # (it does not report the rejected nudge as a success)
            calls_before = len(agent.draft_plans)
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("nudge", args=["Mention the product", request_id])
            assert len(agent.draft_plans) == calls_before + 1

            after = await handle.query("get_card")
            assert after["status"] == "pending"
            assert after["evidence"]["draft"] == before["evidence"]["draft"]
            assert _db_card(workflow_id)["evidence"]["draft"]["text"] == before["evidence"]["draft"]["text"]


@pytest.mark.asyncio
async def test_nudge_under_plan_is_gated_and_assembled(make_review_input, x_activities):
    agent = RecordingAgent()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, agent):
            review_input = make_review_input()
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input,
                id=f"review:test:{review_input.post.id}_plan_nudge_ok", task_queue="test",
            )
            await env.sleep(1)
            text = await handle.execute_update("nudge", args=["Make it shorter", str(uuid.uuid4())])
            card = await handle.query("get_card")
            assert card["evidence"]["draft"]["claim_gate"]["passed"] is True
            assert card["evidence"]["draft"]["claim_gate"]["reasoning"].startswith("All ")
            assert text == card["evidence"]["draft"]["text"]
            assert all(s["text"] for s in card["evidence"]["draft"]["segments"])


@pytest.mark.asyncio
async def test_nudge_activity_failure_under_plan_can_be_retried(make_review_input, x_activities):
    """Codex round 4: an activity error (not only a gate rejection) must not
    leave the request id marked done."""
    from temporalio.client import WorkflowUpdateFailedError
    from temporalio.exceptions import ApplicationError

    class FlakyNudgeAgent(RecordingAgent):
        fail_next_nudge = True

        async def draft(self, evidence, steer=None, idempotency_key=None, plan=None):
            if ":nudge:" in (idempotency_key or "") and self.fail_next_nudge:
                self.fail_next_nudge = False
                raise ApplicationError("model unavailable", non_retryable=True)
            return await super().draft(evidence, steer, idempotency_key, plan)

    agent = FlakyNudgeAgent()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, agent):
            review_input = make_review_input()
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input,
                id=f"review:test:{review_input.post.id}_plan_nudge_flaky", task_queue="test",
            )
            await env.sleep(1)
            request_id = str(uuid.uuid4())
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("nudge", args=["Make it shorter", request_id])

            text = await handle.execute_update("nudge", args=["Make it shorter", request_id])
            card = await handle.query("get_card")
            assert text == card["evidence"]["draft"]["text"]
            assert "Make it shorter" in card["evidence"]["draft"]["steers_applied"]


@pytest.mark.asyncio
async def test_reply_404_fails_confirmation_and_keeps_target_post(make_review_input):
    """Codex round 8: a 404 on the founder's reply URL fails the update
    cleanly and does not remove the (still existing) target post's text or
    the plan's copy of it. Previously the update never resolved (ValueError
    in the handler) and the target's text was scheduled for deletion."""
    from temporalio.client import WorkflowUpdateFailedError
    from backend.activities.store_activities import delete_post_text
    from backend.activities.x_activities import XActivities
    from backend.adapters import FakeXClient

    class GoneReplyClient(FakeXClient):
        async def lookup_post(self, post_id):
            if post_id == "404404":
                return None
            return await super().lookup_post(post_id)

    x_acts = XActivities(GoneReplyClient())
    reply_acts = ReplyActivities(RecordingAgent())
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_acts.lookup_post, x_acts.check_followup,
                reply_acts.research_post, reply_acts.plan_reply, reply_acts.draft_reply,
                claim_gate, store_card, update_card, delete_post_text,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_reply_404"
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test"
            )
            await env.sleep(1)
            await handle.execute_update(
                "approve", args=["https://x.com/test/status/999", str(uuid.uuid4()), False]
            )
            with pytest.raises(WorkflowUpdateFailedError) as excinfo:
                await handle.execute_update(
                    "confirm_posted", args=["https://x.com/test/status/404404", str(uuid.uuid4())]
                )
            assert "not found" in str(excinfo.value.cause)

            card = await handle.query("get_card")
            assert card["status"] == "approved"
            assert card["post"]["text"] == review_input.post.text
            assert card["evidence"]["reply_plan"]["target_point"] == QUESTION
            stored = _db_card(workflow_id)
            assert stored["post"]["text"] == review_input.post.text
            assert stored["evidence"]["reply_plan"]["target_point"] == QUESTION

            history = await handle.fetch_history()
            scheduled = [
                e.activity_task_scheduled_event_attributes.activity_type.name
                for e in history.events
                if e.HasField("activity_task_scheduled_event_attributes")
            ]
            assert "delete_post_text" not in scheduled
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_edit_and_approve_are_rejected_while_a_nudge_is_in_flight(make_review_input, x_activities):
    """Codex round 6: a nudge and an edit must not overlap (either could
    overwrite the other across activity awaits)."""
    import asyncio
    from temporalio.client import WorkflowUpdateFailedError, WorkflowUpdateStage

    release = asyncio.Event()

    class SlowNudgeAgent(RecordingAgent):
        async def draft(self, evidence, steer=None, idempotency_key=None, plan=None):
            if ":nudge:" in (idempotency_key or ""):
                await release.wait()
            return await super().draft(evidence, steer, idempotency_key, plan)

    agent = SlowNudgeAgent()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, agent):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_plan_overlap"
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test"
            )
            await env.sleep(1)

            nudge = await handle.start_update(
                "nudge",
                args=["Make it shorter", str(uuid.uuid4())],
                wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            )
            with pytest.raises(WorkflowUpdateFailedError) as edit_err:
                await handle.execute_update(
                    "edit", args=["Thanks for sharing! What stack are you on?", str(uuid.uuid4())]
                )
            assert "nudge is in progress" in str(edit_err.value.cause)
            with pytest.raises(WorkflowUpdateFailedError) as approve_err:
                await handle.execute_update(
                    "approve", args=["https://x.com/test/status/999", str(uuid.uuid4()), False]
                )
            assert "nudge is in progress" in str(approve_err.value.cause)
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("nudge", args=["Make it technical", str(uuid.uuid4())])

            release.set()
            text = await nudge.result()
            card = await handle.query("get_card")
            assert card["evidence"]["draft"]["text"] == text
            assert _db_card(workflow_id)["evidence"]["draft"]["text"] == text

            # Once the nudge is done, edits are accepted again
            edited = await handle.execute_update(
                "edit", args=["Thanks for sharing! What stack are you on?", str(uuid.uuid4())]
            )
            assert edited == "Thanks for sharing! What stack are you on?"


@pytest.mark.asyncio
async def test_failed_confirmation_can_be_retried_with_same_request_id(make_review_input):
    """Codex round 7: a failed confirm_posted must not be reported as
    "Already confirmed" when retried with the same request id."""
    from dataclasses import replace
    from temporalio.client import WorkflowUpdateFailedError
    from backend.activities.store_activities import delete_post_text
    from backend.activities.x_activities import XActivities
    from backend.adapters import FakeXClient

    class NotYetVisibleClient(FakeXClient):
        seen = 0

        async def lookup_post(self, post_id):
            if post_id != "777777":
                return await super().lookup_post(post_id)
            self.seen += 1
            post = await super().lookup_post(post_id)
            # First lookup: wrong conversation; later: a reply to post 1001
            return post if self.seen == 1 else replace(post, conversation_id="1001")

    x_acts = XActivities(NotYetVisibleClient())
    reply_acts = ReplyActivities(RecordingAgent())
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_acts.lookup_post, x_acts.check_followup,
                reply_acts.research_post, reply_acts.plan_reply, reply_acts.draft_reply,
                claim_gate, store_card, update_card, delete_post_text,
            ],
        ):
            review_input = make_review_input()
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input,
                id=f"review:test:{review_input.post.id}_confirm_retry", task_queue="test",
            )
            await env.sleep(1)
            await handle.execute_update(
                "approve", args=["https://x.com/test/status/999", str(uuid.uuid4()), False]
            )
            request_id = str(uuid.uuid4())
            url = "https://x.com/founder/status/777777"
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("confirm_posted", args=[url, request_id])

            message = await handle.execute_update("confirm_posted", args=[url, request_id])
            assert message == "Confirmed reply 777777"


@pytest.mark.asyncio
async def test_gated_nudge_clears_a_failed_edit_gate(make_review_input, x_activities):
    """Codex round 7: after a failing edit, a nudge that passes the gate
    replaces the draft, so approve must not demand post_anyway for the old text."""
    from backend.database import get_db, ReviewCardDB

    agent = RecordingAgent()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_activities, agent):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_plan_nudge_clears_edit"
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test"
            )
            await env.sleep(1)
            await handle.execute_update(
                "edit", args=["I think ours is way cheaper.", str(uuid.uuid4())]
            )
            card = await handle.query("get_card")
            assert card["edited_text_gate_state"] == "failed"

            await handle.execute_update("nudge", args=["Make it shorter", str(uuid.uuid4())])
            card = await handle.query("get_card")
            assert card["edited_text_gate_state"] is None
            assert card["edited_text_gate_results"] is None
            db = get_db()
            try:
                row = db.query(ReviewCardDB).filter(ReviewCardDB.workflow_id == workflow_id).first()
                assert row.edited_text_gate_state is None
                assert row.edited_text_gate_results is None
            finally:
                db.close()

            # Approve without post_anyway is accepted now
            await handle.execute_update(
                "approve", args=["https://x.com/test/status/999", str(uuid.uuid4()), False]
            )
            assert (await handle.query("get_card"))["status"] == "approved"


@pytest.mark.asyncio
async def test_legacy_reply_404_branch_still_deletes_and_redacts_query(make_review_input, monkeypatch):
    """Histories recorded before the keep-target fix scheduled delete_post_text
    on a reply 404. With the patch not taken (as on their replay), the old
    command sequence is kept, the update now fails cleanly, and get_card
    matches the redacted stored card."""
    from temporalio import workflow as temporal_workflow
    from temporalio.client import WorkflowUpdateFailedError
    from backend.activities.store_activities import delete_post_text
    from backend.activities.x_activities import XActivities
    from backend.adapters import FakeXClient
    from backend.workflows.review import KEEP_TARGET_ON_REPLY_404_PATCH_ID

    real_patched = temporal_workflow.patched
    monkeypatch.setattr(
        temporal_workflow,
        "patched",
        lambda pid: False if pid == KEEP_TARGET_ON_REPLY_404_PATCH_ID else real_patched(pid),
    )

    class GoneReplyClient(FakeXClient):
        async def lookup_post(self, post_id):
            if post_id == "404404":
                return None
            return await super().lookup_post(post_id)

    x_acts = XActivities(GoneReplyClient())
    reply_acts = ReplyActivities(RecordingAgent())
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_acts.lookup_post, x_acts.check_followup,
                reply_acts.research_post, reply_acts.plan_reply, reply_acts.draft_reply,
                claim_gate, store_card, update_card, delete_post_text,
            ],
        ):
            review_input = make_review_input()
            workflow_id = f"review:test:{review_input.post.id}_legacy_404"
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test"
            )
            await env.sleep(1)
            await handle.execute_update(
                "approve", args=["https://x.com/test/status/999", str(uuid.uuid4()), False]
            )
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update(
                    "confirm_posted", args=["https://x.com/test/status/404404", str(uuid.uuid4())]
                )
            card = await handle.query("get_card")
            assert card["post"]["text"] == "[removed - post deleted]"
            assert card["evidence"]["reply_plan"]["target_point"] == "[removed - post deleted]"
            assert QUESTION not in json.dumps(_db_card(workflow_id))
