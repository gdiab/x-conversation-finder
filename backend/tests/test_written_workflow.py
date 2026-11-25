"""Review workflow tests for written drafts (issue #12; ADR-0006 amendment
2026-11-24).

- a written draft is checked by check_written_draft and stored with its
  per-sentence check;
- a failed check is redrafted once with the reasons, then the post is skipped;
- founder edits and nudges go through the same check, and approve keeps the
  issue #15 contract (draft_text + draft_gate_state, post_anyway);
- a watch without a product name stays assembled;
- a removal redacts the model-written text (issue #9);
- Reviews and TopicWatches recorded before this change replay.

Fake adapters and synthetic posts only.
"""

import copy
import dataclasses
import json
import uuid
from pathlib import Path
from typing import List, Optional

import pytest
from temporalio.client import WorkflowHistory, WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from backend.activities.claim_gate_activity import claim_gate, verify_edited_text
from backend.activities.reply_activities import ReplyActivities
from backend.activities.store_activities import (
    GATE_DETAILS_REMOVED,
    POST_TEXT_REMOVED,
    POST_REMOVED_SKIP_REASON,
    delete_post_text,
    get_all_cards,
    redact_removed_card,
    store_card,
    update_card,
)
from backend.activities.written_check_activities import WrittenCheckActivities
from backend.activities.x_activities import XActivities
from backend.adapters import FakeClaimChecker, FakeReplyAgent, FakeXClient
from backend.models import ReplyPlan
from backend.planning import NO_PRODUCT_NAME_NOTE
from backend.workflows.review import (
    DRAFT_CHANGED_ERROR,
    POST_ANYWAY_REQUIRED_ERROR,
    POST_REMOVED_ERROR,
    ReviewWorkflow,
)
from backend.workflows.topic_watch import TopicWatchWorkflow
from backend.tests.review_waits import wait_for_card_ready, query_card, rejection_message
from backend.tests.test_edit_regate import fetch_history_past_approve

FIXTURES = Path(__file__).parent / "fixtures"
PRODUCT = "Acme"
QUESTION = "How do you folks handle eventual consistency?"  # from conftest sample_post
POST_FRAGMENTS = ("Struggling with distributed", "eventual consistency")
APPROVE_URL = "https://x.com/test/status/999"

# The fake written draft for the conftest post and brief
OPENER = "Good question, and a hard thing to get right."
CLAIM = "Our product helps developers build reliable distributed systems."  # brief sentence b1
CLOSER = "What are you using today?"
FAKE_DRAFT = f"{OPENER} {CLAIM} {CLOSER}"

# A claim the brief does not make. No number in it: a number that no brief
# sentence states is rejected in code, and these tests exercise the checker.
UNSUPPORTED_CLAIM = "Our product is quicker than the rest."
UNSUPPORTED_DRAFT = f"{OPENER} {UNSUPPORTED_CLAIM} {CLOSER}"
PASSING_EDIT = "That is a hard thing to get right. What does your setup look like today?"
FAILING_EDIT = "Ours is quicker than the rest."


class WritingAgent(FakeReplyAgent):
    """Fake agent with scripted written drafts and plans; records what it was asked."""

    def __init__(self, texts: Optional[List[str]] = None, plans: Optional[List[ReplyPlan]] = None):
        self._texts = list(texts or [])
        self._plans = list(plans or [])
        self.requests = []
        self.previous_texts = []  # the draft each request asked to rewrite, if any
        self.assembled_calls = 0

    async def plan(self, post, evidence, idempotency_key, feedback=None):
        if self._plans:
            return self._plans.pop(0)
        return await super().plan(post, evidence, idempotency_key, feedback)

    async def draft(self, evidence, steer=None, idempotency_key=None, plan=None):
        self.assembled_calls += 1
        return await super().draft(evidence, steer, idempotency_key, plan)

    async def draft_written(self, request, idempotency_key=None):
        self.requests.append(request)
        # Read now: the call below replaces request.evidence.draft
        self.previous_texts.append(request.evidence.draft.text if request.evidence.draft else None)
        evidence = await super().draft_written(request, idempotency_key)
        if self._texts:
            evidence.draft.text = self._texts.pop(0)
            evidence.draft.written.drafter_hints = []
        return evidence


class RecordingChecker(FakeClaimChecker):
    """The exact-match fake checker; records requests and can be made to fail."""

    def __init__(self):
        self.requests = []
        self.error: Optional[BaseException] = None

    async def check(self, request, idempotency_key):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return await super().check(request, idempotency_key)


class GoneClient(FakeXClient):
    """The target post is gone (404) on every lookup."""

    async def lookup_post(self, post_id):
        return None if post_id == "1001" else await super().lookup_post(post_id)


def _worker(env, x_client=None, agent=None, checker=None, draft_mode="written", workflows=None):
    x_acts = XActivities(x_client or FakeXClient())
    reply = ReplyActivities(agent or FakeReplyAgent(), draft_mode=draft_mode)
    check = WrittenCheckActivities(checker or FakeClaimChecker())
    return Worker(
        env.client,
        task_queue="test",
        workflows=workflows or [ReviewWorkflow],
        activities=[
            x_acts.search_recent,
            x_acts.lookup_post,
            x_acts.check_followup,
            reply.research_post,
            reply.plan_reply,
            reply.draft_reply,
            check.check_written_draft,
            claim_gate,
            verify_edited_text,
            store_card,
            update_card,
            delete_post_text,
        ],
    )


async def _start(env, make_review_input, suffix, **overrides):
    overrides.setdefault("product_name", PRODUCT)
    review_input = make_review_input(**overrides)
    workflow_id = f"review:test:{review_input.post_id}_{suffix}"
    handle = await env.client.start_workflow(
        ReviewWorkflow.run, review_input, id=workflow_id, task_queue="test"
    )
    return handle, workflow_id


def _db_card(workflow_id):
    return next(c for c in get_all_cards() if c["workflow_id"] == workflow_id)


def _scheduled(history):
    return [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]


def _rejection(error: WorkflowUpdateFailedError):
    return error.cause


# ---------- the written draft path ----------


@pytest.mark.asyncio
async def test_written_review_stores_a_card_with_the_per_sentence_check(make_review_input):
    checker = RecordingChecker()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, checker=checker):
            handle, workflow_id = await _start(
                env, make_review_input, "written", product_aliases=["Acme Cloud"],
                key_features=[" Real-time   monitoring ", "Audit log", "real-time monitoring"],
            )
            card = await wait_for_card_ready(handle)

            assert card["status"] == "pending"
            evidence = card["evidence"]
            assert evidence["draft_mode"] == "written" and evidence["draft_mode_note"] is None
            draft = evidence["draft"]
            assert draft["mode"] == "written"
            assert draft["text"] == FAKE_DRAFT
            assert draft["segments"] == []
            assert draft["claim_gate"]["passed"] is True
            assert draft["plan_conformance_failures"] == []

            # Amendment item 6: per sentence, claim or not, why checked, verdict, support
            checks = draft["sentence_checks"]
            assert [(c["sentence"], c["kind"], c["verdict"], c["passed"]) for c in checks] == [
                (OPENER, "non_claim", "not_a_product_claim", True),
                (CLAIM, "claim", "supported", True),
                (CLOSER, "non_claim", "not_a_product_claim", True),
            ]
            assert checks[1]["triggers"] == [{"trigger": "first_person_plural", "match": "our"}]
            assert checks[1]["supporting"] == [{"id": "b1", "text": CLAIM, "context": "Product Brief"}]
            assert draft["written"]["product_name"] == PRODUCT
            assert draft["written"]["product_aliases"] == ["Acme Cloud"]
            assert draft["written"]["checker_called"] is True
            # The drafter's own citation is kept on the draft as provenance
            assert draft["written"]["drafter_hints"] == [{"sentence": CLAIM, "brief_sentence_ids": ["b1"]}]
            # The watch's "Key features", cleaned; the brief's own "Key features:" list is not read
            assert draft["written"]["feature_terms"] == ["Real-time monitoring", "Audit log"]

            # The stored card (what the list API serves) carries the same check
            stored = _db_card(workflow_id)["evidence"]
            assert stored["draft_mode"] == "written"
            assert stored["draft"]["sentence_checks"] == checks
            assert stored["draft"]["written"] == draft["written"]
            assert stored["draft"]["mode"] == "written"

            # One checker call; the claim gate does not run on a written draft
            assert len(checker.requests) == 1
            history = await handle.fetch_history()
            assert _scheduled(history) == [
                "research_post", "plan_reply", "draft_reply", "check_written_draft", "store_card",
            ]
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_the_drafter_sees_the_post_and_the_checker_does_not(make_review_input, sample_post):
    agent, checker = WritingAgent(), RecordingChecker()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent, checker=checker):
            handle, _ = await _start(env, make_review_input, "isolation")
            await wait_for_card_ready(handle)

    # The drafter responds to the post itself, with the plan from issue #5
    [request] = agent.requests
    assert request.post.text == sample_post.text
    assert request.plan.target_point == QUESTION
    assert request.product_name == PRODUCT
    assert [b.id for b in request.brief_sentences][:2] == ["b1", "b2"]

    # The checker gets sentences, brief, product name and aliases, and the must-verify flags: nothing of the post
    [check] = checker.requests
    sent = json.dumps(dataclasses.asdict(check))
    for fragment in POST_FRAGMENTS + (QUESTION, sample_post.author.username):
        assert fragment not in sent
    assert check.sentences == [OPENER, CLAIM, CLOSER]
    assert check.must_verify == [False, True, False]
    # Nor the brief sentence the drafter pointed at (b1), although the draft records it
    assert set(dataclasses.asdict(check)) == {
        "sentences", "brief_sentences", "product_name", "product_aliases", "must_verify",
    }


@pytest.mark.asyncio
async def test_a_failed_check_is_redrafted_once_with_the_reasons(make_review_input):
    agent, checker = WritingAgent(texts=[UNSUPPORTED_DRAFT]), RecordingChecker()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent, checker=checker):
            handle, _ = await _start(env, make_review_input, "redraft")
            card = await wait_for_card_ready(handle)

    assert card["status"] == "pending"
    assert card["evidence"]["draft"]["text"] == FAKE_DRAFT  # the second draft passed
    first, second = agent.requests
    assert first.steer is None
    assert second.steer.startswith("Previous draft failed claim gate. Fix these issues:")
    assert "Checked as a product claim (uses we/our/us/ours" in second.steer
    assert "The brief does not support it" in second.steer
    assert agent.previous_texts == [None, UNSUPPORTED_DRAFT]  # the drafter is shown what failed
    assert len(checker.requests) == 2
    assert card["evidence"]["draft"]["steers_applied"] == [second.steer]


@pytest.mark.asyncio
async def test_two_failed_checks_skip_the_post_with_the_reasons(make_review_input):
    # Two different drafts, both unsupported (an identical redraft is not checked again; see below)
    second = f"{OPENER} Our product is quicker than anything. {CLOSER}"
    agent, checker = WritingAgent(texts=[second, UNSUPPORTED_DRAFT]), RecordingChecker()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent, checker=checker):
            handle, workflow_id = await _start(env, make_review_input, "skip")
            assert await handle.result() == "skipped"

            card = await query_card(handle)
            assert card["status"] == "skipped"
            assert card["skip_reason"].startswith("Not enough evidence: Our product is quicker than")
            draft = card["evidence"]["draft"]
            assert draft["claim_gate"]["passed"] is False
            assert draft["sentence_checks"][1]["verdict"] == "unsupported"
            assert _db_card(workflow_id)["status"] == "skipped"
            assert _scheduled(await handle.fetch_history()) == [
                "research_post", "plan_reply",
                "draft_reply", "check_written_draft",
                "draft_reply", "check_written_draft",
                "store_card",
            ]
    assert len(agent.requests) == 2 and len(checker.requests) == 2


@pytest.mark.asyncio
async def test_a_draft_code_rejects_costs_no_checker_call(make_review_input):
    four = f"{OPENER} {OPENER} {OPENER} {CLOSER}"
    agent, checker = WritingAgent(texts=[four]), RecordingChecker()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent, checker=checker):
            handle, _ = await _start(env, make_review_input, "structural")
            card = await wait_for_card_ready(handle)

    assert card["status"] == "pending"
    assert "at most 3 sentences (got 4)" in agent.requests[1].steer
    assert len(checker.requests) == 1  # only the second draft reached the checker


@pytest.mark.asyncio
async def test_a_draft_that_ignores_the_plans_product_decision_is_redrafted(make_review_input):
    """mention_product=True, but the first draft makes no product claim."""
    agent = WritingAgent(texts=[f"{OPENER} {CLOSER}"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent):
            handle, _ = await _start(env, make_review_input, "plan")
            card = await wait_for_card_ready(handle)

    assert card["status"] == "pending"
    assert "[reply plan]" in agent.requests[1].steer
    assert "says to mention the product, but the draft has no product claim" in agent.requests[1].steer


@pytest.mark.asyncio
async def test_written_mode_drafts_a_plan_that_assembled_mode_must_skip(make_review_input):
    """needs_written_drafts (issue #5) is off in written mode: answering the
    question without mentioning the product is now possible."""
    plan = ReplyPlan(
        reply_type="answer_question", target_point=QUESTION, mention_product=False,
        mention_product_reason="They ask about practice, not tools.",
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=WritingAgent(plans=[plan])):
            handle, _ = await _start(env, make_review_input, "no_mention")
            written = await wait_for_card_ready(handle)
        async with _worker(env, agent=WritingAgent(plans=[plan]), draft_mode="assembled"):
            handle, _ = await _start(env, make_review_input, "no_mention_assembled")
            assembled = await wait_for_card_ready(handle)

    assert written["status"] == "pending"
    assert written["evidence"]["reply_plan_skip_rule"] is None
    assert written["evidence"]["draft"]["text"] == f"{OPENER} {CLOSER}"
    assert all(c["kind"] == "non_claim" for c in written["evidence"]["draft"]["sentence_checks"])

    assert assembled["status"] == "skipped"
    assert assembled["evidence"]["reply_plan_skip_rule"] == "needs_written_drafts"
    assert assembled["evidence"]["draft_mode"] == "assembled"


# ---------- existing watches: no product name ----------


@pytest.mark.asyncio
async def test_a_review_without_a_product_name_stays_assembled(make_review_input):
    """A watch started before the Product name field existed passes no name.
    Written mode is unavailable to its Reviews until it is restarted with one."""
    agent, checker = WritingAgent(), RecordingChecker()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent, checker=checker, draft_mode="written"):
            handle, workflow_id = await _start(env, make_review_input, "no_name", product_name="")
            card = await wait_for_card_ready(handle)

            assert card["status"] == "pending"
            assert card["evidence"]["draft_mode"] == "assembled"
            assert card["evidence"]["draft_mode_note"] == NO_PRODUCT_NAME_NOTE
            assert card["evidence"]["draft"]["mode"] == "assembled"
            assert card["evidence"]["draft"]["segments"]
            assert card["evidence"]["draft"]["sentence_checks"] == []
            assert _db_card(workflow_id)["evidence"]["draft_mode_note"] == NO_PRODUCT_NAME_NOTE
            scheduled = _scheduled(await handle.fetch_history())
            assert "claim_gate" in scheduled and "check_written_draft" not in scheduled

            # Its edits use the assembled re-gate too
            await handle.execute_update("edit", args=["Thanks! Happy to help.", str(uuid.uuid4())])
            assert "verify_edited_text" in _scheduled(await handle.fetch_history())
    assert agent.requests == [] and agent.assembled_calls == 1 and checker.requests == []


@pytest.mark.asyncio
async def test_assembled_is_the_default_mode_with_a_product_name(make_review_input):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, draft_mode=None):  # None: read DRAFT_MODE, which defaults to assembled
            handle, _ = await _start(env, make_review_input, "default")
            card = await wait_for_card_ready(handle)

    assert card["evidence"]["draft_mode"] == "assembled"
    assert card["evidence"]["draft_mode_note"] is None
    assert card["evidence"]["draft"]["mode"] == "assembled"


# ---------- founder edits (amendment item 5; issues #15 and #20) ----------


@pytest.mark.asyncio
async def test_a_passing_edit_is_checked_and_approve_names_the_draft_shown(make_review_input):
    checker = RecordingChecker()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, checker=checker):
            handle, workflow_id = await _start(env, make_review_input, "edit_pass")
            await wait_for_card_ready(handle)

            assert await handle.execute_update("edit", args=[PASSING_EDIT, str(uuid.uuid4())]) == PASSING_EDIT

            card = await query_card(handle)
            assert card["evidence"]["draft"]["text"] == PASSING_EDIT
            assert card["edited_text_gate_state"] == "passed"
            assert [(r["kind"], r["verdict"], r["passed"]) for r in card["edited_text_gate_results"]] == [
                ("non_claim", "not_a_product_claim", True),
                ("non_claim", "not_a_product_claim", True),
            ]
            stored = _db_card(workflow_id)
            assert stored["edited_text_gate_state"] == "passed"
            assert stored["edited_text_gate_results"] == card["edited_text_gate_results"]
            # The edit ran the written check, not the assembled re-gate
            scheduled = _scheduled(await handle.fetch_history())
            assert scheduled.count("check_written_draft") == 2 and "verify_edited_text" not in scheduled
            assert checker.requests[-1].must_verify == [False, False]

            # Issue #15: approve is bound to the draft the founder saw
            with pytest.raises(WorkflowUpdateFailedError) as stale:
                await handle.execute_update("approve", args=[
                    APPROVE_URL, str(uuid.uuid4()), False, {"text": FAKE_DRAFT, "gate_state": None},
                ])
            assert _rejection(stale.value).type == DRAFT_CHANGED_ERROR

            assert await handle.execute_update("approve", args=[
                APPROVE_URL, str(uuid.uuid4()), False, {"text": PASSING_EDIT, "gate_state": "passed"},
            ]) == "Approved"

            history = await fetch_history_past_approve(handle)
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_a_failing_edit_needs_post_anyway_which_is_recorded(make_review_input):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env):
            handle, workflow_id = await _start(env, make_review_input, "edit_fail")
            await wait_for_card_ready(handle)

            await handle.execute_update("edit", args=[FAILING_EDIT, str(uuid.uuid4())])

            card = await query_card(handle)
            assert card["edited_text_gate_state"] == "failed"
            [result] = card["edited_text_gate_results"]
            assert (result["kind"], result["verdict"], result["passed"]) == ("claim", "unsupported", False)
            assert {t["trigger"] for t in result["triggers"]} == {"first_person_plural", "comparison"}
            assert result["reason"].startswith("Checked as a product claim (uses we/our/us/ours")

            shown = {"text": FAILING_EDIT, "gate_state": "failed"}
            with pytest.raises(WorkflowUpdateFailedError) as refused:
                await handle.execute_update("approve", args=[APPROVE_URL, str(uuid.uuid4()), False, shown])
            cause = _rejection(refused.value)
            assert cause.type == POST_ANYWAY_REQUIRED_ERROR
            # issue #23: the workflow holds no sentences; the API adds the stored ones
            details = cause.details[0]
            assert (details["gate_state"], details["failing_sentences"]) == ("failed", [])
            assert set(details["results_ref"]) == {"check_slot"}  # a slot name, no text
            from backend.api import stored_failing_sentences
            assert stored_failing_sentences(workflow_id, details["results_ref"]) == [FAILING_EDIT]

            assert await handle.execute_update(
                "approve", args=[APPROVE_URL, str(uuid.uuid4()), True, shown]
            ) == "Approved"
            await fetch_history_past_approve(handle)

            card = await query_card(handle)
            assert card["post_anyway"] is True
            assert card["post_anyway_failing_sentences"] == [FAILING_EDIT]
            stored = _db_card(workflow_id)
            assert stored["post_anyway"] is True
            assert stored["post_anyway_failing_sentences"] == [FAILING_EDIT]


KEY_FEATURE_EDIT = "Automatic failover would have caught that."


@pytest.mark.asyncio
async def test_a_key_feature_puts_the_drafters_and_the_founders_sentences_in_the_check(make_review_input):
    """The drafter is told the watch's key features, and the check of a
    founder edit reads the same list."""
    agent = WritingAgent()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent):
            handle, _ = await _start(env, make_review_input, "key_feature", key_features=["Automatic failover"])
            card = await wait_for_card_ready(handle)
            assert card["evidence"]["draft"]["written"]["feature_terms"] == ["Automatic failover"]
            assert agent.requests[0].feature_terms == ["Automatic failover"]

            await handle.execute_update("edit", args=[KEY_FEATURE_EDIT, str(uuid.uuid4())])

            card = await query_card(handle)
            [result] = card["edited_text_gate_results"]
            assert result["triggers"] == [{"trigger": "feature_term", "match": "Automatic failover"}]
            assert (result["kind"], result["passed"]) == ("claim", False)
            assert result["reason"].startswith('Checked as a product claim (uses a key feature ("Automatic failover"))')


@pytest.mark.asyncio
async def test_a_review_without_key_features_has_no_feature_terms(make_review_input):
    """A watch started without the field (or before it existed): the brief's
    own "Key features:" list does not make its items triggers."""
    agent = WritingAgent()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent):
            handle, _ = await _start(env, make_review_input, "no_key_features")
            card = await wait_for_card_ready(handle)
            assert card["evidence"]["draft"]["written"]["feature_terms"] == []
            assert agent.requests[0].feature_terms == []

            await handle.execute_update("edit", args=[KEY_FEATURE_EDIT, str(uuid.uuid4())])

            card = await query_card(handle)
            [result] = card["edited_text_gate_results"]
            assert result["triggers"] == []


@pytest.mark.asyncio
async def test_an_edit_whose_check_cannot_run_is_not_checked(make_review_input):
    checker = RecordingChecker()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, checker=checker):
            handle, _ = await _start(env, make_review_input, "edit_error")
            await wait_for_card_ready(handle)
            checker.error = RuntimeError("synthetic: checker unavailable")

            await handle.execute_update("edit", args=[PASSING_EDIT, str(uuid.uuid4())])

            card = await query_card(handle)
            assert card["edited_text_gate_state"] == "not_checked"
            assert card["evidence"]["draft"]["text"] == PASSING_EDIT
            with pytest.raises(WorkflowUpdateFailedError) as refused:
                await handle.execute_update("approve", args=[
                    APPROVE_URL, str(uuid.uuid4()), False, {"text": PASSING_EDIT, "gate_state": "not_checked"},
                ])
            assert _rejection(refused.value).type == POST_ANYWAY_REQUIRED_ERROR


class FlippingChecker(RecordingChecker):
    """Answers differently each time it is asked about FAILING_EDIT: first the
    usual "unsupported", afterwards "supported"."""

    async def check(self, request, idempotency_key):
        response = await super().check(request, idempotency_key)
        asked_before = sum(1 for r in self.requests[:-1] if r.sentences == request.sentences)
        if request.sentences == [FAILING_EDIT] and asked_before:
            from backend.models.written import ClaimVerdict
            response.verdicts = [ClaimVerdict(index=1, about_product=True, verdict="supported", brief_sentence_ids=["b1"])]
        return response


@pytest.mark.asyncio
async def test_the_same_text_keeps_one_check_result_so_approve_names_what_was_seen(make_review_input):
    """The checker is a model: asked twice about one text it may answer twice.
    The Review keeps the first result per text, so the draft text and gate
    state the founder saw still identify one result (issue #15)."""
    checker = FlippingChecker()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, checker=checker):
            handle, _ = await _start(env, make_review_input, "same_text")
            await wait_for_card_ready(handle)

            await handle.execute_update("edit", args=[FAILING_EDIT, str(uuid.uuid4())])
            first = (await query_card(handle))["edited_text_gate_results"]
            await handle.execute_update("edit", args=[PASSING_EDIT, str(uuid.uuid4())])
            await handle.execute_update("edit", args=[FAILING_EDIT, str(uuid.uuid4())])  # the same text again

            card = await query_card(handle)
            assert card["edited_text_gate_state"] == "failed"
            assert card["edited_text_gate_results"] == first  # not the checker's second opinion
            asked = [r.sentences for r in checker.requests]
            assert asked.count([FAILING_EDIT]) == 1  # and no second paid call for it

            # An edit is checked as an edit (not held to the plan), once per text
            await handle.execute_update("edit", args=[FAKE_DRAFT, str(uuid.uuid4())])
            calls = len(checker.requests)
            await handle.execute_update("edit", args=[PASSING_EDIT, str(uuid.uuid4())])
            await handle.execute_update("edit", args=[FAKE_DRAFT, str(uuid.uuid4())])
            card = await query_card(handle)
            assert card["edited_text_gate_state"] == "passed"
            assert [r["verdict"] for r in card["edited_text_gate_results"]] == [
                "not_a_product_claim", "supported", "not_a_product_claim",
            ]
            assert len(checker.requests) == calls
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(await handle.fetch_history())


class SecondOpinionChecker(RecordingChecker):
    """Supports CLAIM with b1 the first time it is asked, with b5 afterwards:
    a model that passes the same text twice with different citations."""

    async def check(self, request, idempotency_key):
        response = await super().check(request, idempotency_key)
        asked_before = sum(1 for r in self.requests[:-1] if r.sentences == request.sentences)
        if asked_before:
            for verdict in response.verdicts:
                if verdict.verdict == "supported":
                    verdict.brief_sentence_ids = ["b5"]
        return response


@pytest.mark.asyncio
async def test_a_nudge_that_returns_the_same_text_keeps_the_check_the_founder_saw(make_review_input):
    """Approve names a draft by its text and gate state. A nudge that gives
    back the same text must not swap the check under it."""
    agent, checker = WritingAgent(), SecondOpinionChecker()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent, checker=checker):
            handle, workflow_id = await _start(env, make_review_input, "nudge_same")
            before = (await wait_for_card_ready(handle))["evidence"]["draft"]

            new_text = await handle.execute_update("nudge", args=["Say it again", str(uuid.uuid4())])

            assert new_text == before["text"] == FAKE_DRAFT
            after = (await query_card(handle))["evidence"]["draft"]
            assert after["sentence_checks"] == before["sentence_checks"]
            assert after["sentence_checks"][1]["supporting"][0]["id"] == "b1"
            assert _db_card(workflow_id)["evidence"]["draft"]["sentence_checks"] == before["sentence_checks"]
            assert len(checker.requests) == 1  # the nudged text was not sent to the checker again


@pytest.mark.asyncio
async def test_a_redraft_identical_to_the_failed_draft_is_not_checked_again(make_review_input):
    agent, checker = WritingAgent(texts=[UNSUPPORTED_DRAFT, UNSUPPORTED_DRAFT]), FlippingAllChecker()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent, checker=checker):
            handle, _ = await _start(env, make_review_input, "same_redraft")
            # The second opinion would have passed the same text; the first verdict stands
            assert await handle.result() == "skipped"
    assert len(agent.requests) == 2 and len(checker.requests) == 1


class FlippingAllChecker(RecordingChecker):
    """Calls a text unsupported the first time and supported every time after."""

    async def check(self, request, idempotency_key):
        response = await super().check(request, idempotency_key)
        if sum(1 for r in self.requests[:-1] if r.sentences == request.sentences):
            from backend.models.written import ClaimVerdict
            response.verdicts = [
                ClaimVerdict(index=i, about_product=must, verdict="supported" if must else None,
                             brief_sentence_ids=["b1"] if must else [])
                for i, must in enumerate(request.must_verify, start=1)
            ]
        return response


@pytest.mark.asyncio
async def test_an_edit_that_could_not_be_checked_is_checked_again(make_review_input):
    checker = RecordingChecker()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, checker=checker):
            handle, _ = await _start(env, make_review_input, "retry_check")
            await wait_for_card_ready(handle)
            checker.error = RuntimeError("synthetic: checker unavailable")
            await handle.execute_update("edit", args=[PASSING_EDIT, str(uuid.uuid4())])
            assert (await query_card(handle))["edited_text_gate_state"] == "not_checked"

            checker.error = None
            await handle.execute_update("edit", args=[PASSING_EDIT, str(uuid.uuid4())])

            assert (await query_card(handle))["edited_text_gate_state"] == "passed"


# ---------- nudges ----------


@pytest.mark.asyncio
async def test_a_nudge_is_rewritten_checked_and_replaces_the_draft(make_review_input):
    agent = WritingAgent()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent):
            handle, workflow_id = await _start(env, make_review_input, "nudge")
            await wait_for_card_ready(handle)
            await handle.execute_update("edit", args=[FAILING_EDIT, str(uuid.uuid4())])

            new_text = await handle.execute_update("nudge", args=["Make it shorter", str(uuid.uuid4())])

            assert new_text == f"{OPENER} {CLAIM}"
            card = await query_card(handle)
            assert card["evidence"]["draft"]["text"] == new_text
            assert [c["verdict"] for c in card["evidence"]["draft"]["sentence_checks"]] == [
                "not_a_product_claim", "supported",
            ]
            # The checked redraft replaces the failing edit and its results
            assert card["edited_text_gate_state"] is None and card["edited_text_gate_results"] is None
            stored = _db_card(workflow_id)
            assert stored["evidence"]["draft"]["text"] == new_text
            assert stored["edited_text_gate_state"] is None
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(await handle.fetch_history())

    nudge = agent.requests[-1]
    assert nudge.steer == "Make it shorter"
    assert agent.previous_texts[-1] == FAILING_EDIT  # the drafter rewrites the current text
    assert nudge.plan.target_point == QUESTION


@pytest.mark.asyncio
async def test_a_nudged_draft_that_fails_the_check_is_rejected(make_review_input):
    agent = WritingAgent()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent):
            handle, workflow_id = await _start(env, make_review_input, "nudge_fail")
            await wait_for_card_ready(handle)
            agent._texts = [UNSUPPORTED_DRAFT]

            with pytest.raises(WorkflowUpdateFailedError) as refused:
                await handle.execute_update("nudge", args=["Make it bolder", str(uuid.uuid4())])

            # issue #23: the reasons are stored; the API adds them to the message
            assert "brief" not in _rejection(refused.value).message
            message = rejection_message(handle, refused.value)
            assert message.startswith("Nudged draft rejected; the current draft is unchanged:")
            assert "The brief does not support it" in message
            card = await query_card(handle)
            assert card["evidence"]["draft"]["text"] == FAKE_DRAFT
            assert _db_card(workflow_id)["evidence"]["draft"]["text"] == FAKE_DRAFT


# ---------- removal duty (issue #9): model-written text can quote the post ----------


def _written_card():
    """A stored written-mode card as store_card writes it, with an edit and an override."""
    check = {
        "sentence": "You said 'eventual consistency' bites.", "passed": False, "kind": "claim",
        "reason": "Checked as a product claim (contains a number (\"3\")). The brief does not support it: no.",
        "kept_segment": False,
        "triggers": [{"trigger": "number", "match": "3"}, {"trigger": "made_up", "match": "x"}],
        "checker_flagged": True, "verdict": "unsupported",
        "supporting": [{"id": "b1", "text": CLAIM, "context": "Product Brief"}],
    }
    return {
        "workflow_id": "review:test:1001_card",
        "post": {"id": "1001", "text": "Struggling with distributed systems reliability."},
        "evidence": {
            "post_id": "1001",
            "matched_topic": "reliability",
            "why_it_fits": "Quotes the post.",
            "founder_materials": [],
            "reply_fit": {"post_kind": "question"},
            "draft_mode": "written",
            "draft_mode_note": None,
            "draft": {
                "mode": "written",
                "text": "You said 'eventual consistency' bites. Our product helps.",
                "segments": [],
                "claim_gate": {"passed": False, "segments": [], "failed_sentences": [
                    {"sentence": "You said 'eventual consistency' bites.", "reason": "no"}], "reasoning": "r"},
                "steers_applied": [
                    "Previous draft failed claim gate. Fix these issues: 'You said...': no",
                    "Drop the part about 'eventual consistency'",  # a founder nudge quoting the draft
                    "manual_edit",
                ],
                "sentence_checks": [check],
                "written": {
                    "product_name": PRODUCT, "product_aliases": [], "feature_terms": ["Automatic failover"],
                    "drafter_hints": [{"sentence": "You said 'eventual consistency' bites.", "brief_sentence_ids": ["b1"]}],
                },
            },
            "skip_reason": None,
        },
        "skip_reason": "Not enough evidence: You said 'eventual... (no)",
        "edited_text_gate_results": [copy.deepcopy(check)],
        "post_anyway_failing_sentences": ["You said 'eventual consistency' bites."],
    }


def test_removal_redacts_everything_a_model_wrote_on_a_written_card():
    card = _written_card()

    redacted = redact_removed_card(card)

    assert "eventual consistency" not in json.dumps(redacted)
    assert redacted["post"] == {"id": "1001", "text": POST_TEXT_REMOVED, "text_removed": True}
    draft = redacted["evidence"]["draft"]
    assert draft["text"] == POST_TEXT_REMOVED
    [check] = draft["sentence_checks"]
    assert check["sentence"] == POST_TEXT_REMOVED and check["reason"] == GATE_DETAILS_REMOVED
    # Kept: the verdict, which triggers fired (known ids only, no matched text) and
    # the supporting brief sentences, which are the founder's own text
    assert (check["kind"], check["verdict"], check["passed"]) == ("claim", "unsupported", False)
    assert check["triggers"] == [{"trigger": "number", "match": ""}, {"trigger": "<unknown>", "match": ""}]
    assert check["supporting"] == [{"id": "b1", "text": CLAIM, "context": "Product Brief"}]
    assert draft["written"]["drafter_hints"] == []
    assert draft["written"]["product_name"] == PRODUCT  # the founder's own field
    # A nudge was typed while looking at the model's text, so it goes; code's markers stay
    assert draft["steers_applied"] == [
        "Previous draft failed claim gate. details removed with the post", POST_TEXT_REMOVED, "manual_edit",
    ]
    # The card-level copies of the edited sentences go too
    assert redacted["edited_text_gate_results"] == [check]
    assert redacted["post_anyway_failing_sentences"] == [POST_TEXT_REMOVED]
    assert redacted["skip_reason"] == "Not enough evidence: details removed with the post"
    assert card["evidence"]["draft"]["text"].startswith("You said")  # the input is not mutated
    assert redact_removed_card(redacted) == redacted  # idempotent


def test_removal_keeps_an_assembled_cards_draft_and_edit_results():
    """Unchanged from issue #9: an assembled draft is code-inserted text and
    its edit results are the founder's own words."""
    card = _written_card()
    card["evidence"]["draft_mode"] = "assembled"
    card["evidence"]["draft"].update(mode="assembled", text="Thanks! Happy to help.", sentence_checks=[], written=None)
    card["evidence"]["draft"]["steers_applied"] = ["Make it shorter", "manual_edit"]
    card["edited_text_gate_results"] = [{"sentence": "Thanks!", "passed": True, "kind": "non_claim", "reason": ""}]
    card["post_anyway_failing_sentences"] = ["Founder's own sentence."]

    redacted = redact_removed_card(card)

    assert redacted["evidence"]["draft"]["text"] == "Thanks! Happy to help."
    assert redacted["evidence"]["draft"]["steers_applied"] == ["Make it shorter", "manual_edit"]
    assert redacted["edited_text_gate_results"] == card["edited_text_gate_results"]
    assert redacted["post_anyway_failing_sentences"] == ["Founder's own sentence."]


@pytest.mark.asyncio
async def test_a_removal_at_approve_redacts_the_written_draft_and_its_edit(make_review_input):
    quoting = "You wrote 'eventual consistency', and that phrase hides a lot."
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, x_client=GoneClient()):
            handle, workflow_id = await _start(env, make_review_input, "removed")
            await wait_for_card_ready(handle)
            await handle.execute_update("edit", args=[quoting, str(uuid.uuid4())])
            assert _db_card(workflow_id)["evidence"]["draft"]["text"] == quoting

            with pytest.raises(WorkflowUpdateFailedError) as refused:
                await handle.execute_update("approve", args=[
                    APPROVE_URL, str(uuid.uuid4()), False, {"text": quoting, "gate_state": "passed"},
                ])
            assert _rejection(refused.value).type == POST_REMOVED_ERROR
            assert await handle.result() == "skipped"

            for card in (_db_card(workflow_id), await query_card(handle)):
                text = json.dumps(card)
                for fragment in POST_FRAGMENTS:
                    assert fragment not in text
                assert card["post"]["text_removed"] is True
                assert card["evidence"]["draft"]["text"] == POST_TEXT_REMOVED
                assert all(c["sentence"] == POST_TEXT_REMOVED for c in card["evidence"]["draft"]["sentence_checks"])
                assert [r["sentence"] for r in card["edited_text_gate_results"]] == [POST_TEXT_REMOVED]
                # Brief sentences on the card are the founder's own and stay
                assert card["evidence"]["draft"]["sentence_checks"][1]["supporting"][0]["text"] == CLAIM


def _record_removal_by_another_review(post_id="1001"):
    """A card of the same post under another account, with its removal recorded."""
    from backend.database import ReviewCardDB, get_db

    db = get_db()
    try:
        db.add(ReviewCardDB(
            workflow_id=f"review:other-account:{post_id}",
            post_data={"id": post_id, "text": POST_TEXT_REMOVED, "text_removed": True},
            evidence_data={},
            status="skipped",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        ))
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_written_review_is_skipped_when_another_review_recorded_the_posts_removal(make_review_input):
    """This Review has no card yet, so only a post-wide lookup can know. The
    written drafter is not sent the post, and the Review ends as "post removed"."""
    agent, checker = WritingAgent(), RecordingChecker()
    _record_removal_by_another_review()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent, checker=checker):
            # A Review whose input carries the post (recorded before issue
            # #23) researches and plans, and draft_reply refuses; a text-free
            # Review stops at research (the parametrized test above)
            handle, workflow_id = await _start(env, make_review_input, "removed_elsewhere", legacy=True)
            assert await handle.result() == "skipped"

            assert agent.requests == [] and checker.requests == []
            for card in (_db_card(workflow_id), await query_card(handle)):
                assert card["status"] == "skipped"
                assert card["skip_reason"].startswith("Post removed")
                assert card["post"]["text_removed"] is True and card["post"]["text"] == POST_TEXT_REMOVED
                assert card["evidence"]["draft"] is None
                for fragment in POST_FRAGMENTS:
                    assert fragment not in json.dumps(card)
            assert _scheduled(await handle.fetch_history()) == [
                "research_post", "plan_reply", "draft_reply", "store_card",
            ]
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(await handle.fetch_history())


class RemovedWhileDrafting(WritingAgent):
    """The other Review records the removal while this one's draft is being written."""

    async def draft_written(self, request, idempotency_key=None):
        evidence = await super().draft_written(request, idempotency_key)
        _record_removal_by_another_review(request.post.id)
        return evidence


@pytest.mark.asyncio
async def test_a_draft_is_not_sent_to_the_checker_once_the_post_is_removed(make_review_input):
    agent, checker = RemovedWhileDrafting(), RecordingChecker()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent, checker=checker):
            handle, workflow_id = await _start(env, make_review_input, "removed_mid_draft")
            assert await handle.result() == "skipped"

            assert len(agent.requests) == 1 and checker.requests == []
            card = _db_card(workflow_id)
            assert card["skip_reason"].startswith("Post removed")
            assert card["evidence"]["draft"] is None  # the draft written from the removed post is dropped
            assert FAKE_DRAFT not in json.dumps(card)


@pytest.mark.asyncio
async def test_an_assembled_review_is_unchanged_by_the_post_wide_lookup(make_review_input):
    """Assembled mode keeps issue #9's behavior: the Review drafts, and its
    card is stored redacted. (Its drafter is not sent the post text.)"""
    _record_removal_by_another_review()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, draft_mode="assembled"):
            handle, workflow_id = await _start(env, make_review_input, "removed_assembled", legacy=True)
            await wait_for_card_ready(handle)

            card = _db_card(workflow_id)
            assert card["status"] == "pending"
            assert card["post"]["text_removed"] is True
            assert "claim_gate" in _scheduled(await handle.fetch_history())


@pytest.mark.asyncio
@pytest.mark.parametrize("draft_mode", ["assembled", "written"])
async def test_a_text_free_review_of_a_removed_post_stops_before_any_model_call(make_review_input, draft_mode):
    """issue #23: a Review started with only a post id reads the post from
    the database, which refuses once the removal is recorded. It ends as
    skipped "post removed" at research, in both modes, without a model call."""
    _record_removal_by_another_review()
    agent = WritingAgent()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent, draft_mode=draft_mode):
            handle, workflow_id = await _start(env, make_review_input, f"removed_text_free_{draft_mode}")
            assert await handle.result() == "skipped"
            card = _db_card(workflow_id)
            assert card["skip_reason"] == POST_REMOVED_SKIP_REASON
            assert card["post"]["text_removed"] is True
            assert card["evidence"]["draft"] is None
            assert _scheduled(await handle.fetch_history()) == ["research_post", "store_card"]
            assert agent.requests == [] and agent.assembled_calls == 0


# ---------- replay safety ----------

# Recorded on main 2179673, before written drafts: a Review with the plan
# step that was edited, nudged and approved (waiting for the reply URL), a
# TopicWatch sleeping after its first search, and one that continued as new.
# Synthetic data; worker and client identities scrubbed.
REVIEW_HISTORY = (
    "review_history_main_2179673_edit_nudge_approve.json",
    "review:test:5200_main_2179673_edit_nudge_approve",
)
WATCH_HISTORIES = [
    ("topic_watch_history_main_2179673_in_flight.json", "watch:test:reliability_main_2179673_in_flight"),
    ("topic_watch_history_main_2179673_continued_as_new.json", "watch:test:reliability_main_2179673_continued"),
]
# Every Review history recorded on earlier mains
OLDER_REVIEW_HISTORIES = [
    ("review_history_pre_reply_plan.json", "review:test:5001_pre_plan"),
    ("review_history_main_32ff75e.json", "review:test:5032_main_32ff75e"),
    ("review_history_main_9f4df4a_approved.json", "review:test:5090_main_9f4df4a_approved"),
    ("review_history_main_f9d58ec_edit_before_store.json", "review:test:5120_main_f9d58ec_edit_before_store"),
]


def _history(fixture, workflow_id):
    return WorkflowHistory.from_json(workflow_id, (FIXTURES / fixture).read_text())


def _events(fixture):
    return json.loads((FIXTURES / fixture).read_text())["events"]


def _payload(event, key):
    import base64
    return json.loads(base64.b64decode(event[key]["input"]["payloads"][0]["data"]))


def test_review_fixture_covers_the_paths_this_change_touches():
    events = _events(REVIEW_HISTORY[0])
    scheduled = [
        e["activityTaskScheduledEventAttributes"]["activityType"]["name"]
        for e in events if e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"
    ]
    updates = [
        e["workflowExecutionUpdateAcceptedEventAttributes"]["acceptedRequest"]["input"]["name"]
        for e in events if e["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED"
    ]

    # plan, draft, gate, the edit re-gate, a nudge redraft with its gate, approve
    assert scheduled == [
        "research_post", "plan_reply", "draft_reply", "claim_gate", "store_card",
        "verify_edited_text", "update_card", "draft_reply", "claim_gate", "update_card",
        "lookup_post", "update_card",
    ]
    assert updates == ["edit", "nudge", "approve"]
    assert "check_written_draft" not in scheduled
    # Its input predates the product name fields and the key features
    started = _payload(events[0], "workflowExecutionStartedEventAttributes")
    assert not {"product_name", "product_aliases", "key_features"} & set(started)


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture,workflow_id", [REVIEW_HISTORY, *OLDER_REVIEW_HISTORIES])
async def test_reviews_recorded_before_written_drafts_replay(fixture, workflow_id):
    await Replayer(workflows=[ReviewWorkflow]).replay_workflow(_history(fixture, workflow_id))


@pytest.mark.asyncio
async def test_old_review_history_fails_replay_if_it_were_read_as_written(monkeypatch):
    """Control: the written path is chosen by the draft mode recorded in the
    plan_reply result, which old histories lack. If a missing mode counted as
    "written", the old history would not replay: this shows the replay test
    above runs through the branch points."""
    import backend.models.written as written_models

    monkeypatch.setattr(written_models, "DRAFT_MODE_WRITTEN", None)  # None == a missing mode
    result = await Replayer(workflows=[ReviewWorkflow]).replay_workflow(
        _history(*REVIEW_HISTORY), raise_on_replay_failure=False
    )

    assert result.replay_failure is not None
    assert "Nondeterminism" in str(result.replay_failure)
    assert "check_written_draft" in str(result.replay_failure)


def test_watch_fixtures_predate_the_product_name_fields():
    for fixture, _ in WATCH_HISTORIES:
        events = _events(fixture)
        started = _payload(events[0], "workflowExecutionStartedEventAttributes")
        assert not {"product_name", "product_aliases", "key_features"} & set(started)
        children = [e for e in events if e["eventType"] == "EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED"]
        assert children, fixture
    assert _events(WATCH_HISTORIES[1][0])[-1]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW"


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture,workflow_id", WATCH_HISTORIES)
async def test_watches_recorded_before_the_product_name_fields_replay(fixture, workflow_id):
    await Replayer(workflows=[TopicWatchWorkflow, ReviewWorkflow]).replay_workflow(_history(fixture, workflow_id))


def test_inputs_recorded_before_the_new_fields_decode_to_assembled_defaults():
    """What Temporal's converter makes of an old payload: the new fields take their defaults."""
    from temporalio.converter import value_to_type

    from backend.activities import DraftReplyInput, PlanReplyResult
    from backend.activities.written_check_activities import CheckWrittenDraftInput
    from backend.workflows.review import ReviewInput
    from backend.workflows.topic_watch import TopicWatchInput

    watch = value_to_type(TopicWatchInput, _payload(_events(WATCH_HISTORIES[0][0])[0], "workflowExecutionStartedEventAttributes"))
    review = value_to_type(ReviewInput, _payload(_events(REVIEW_HISTORY[0])[0], "workflowExecutionStartedEventAttributes"))

    assert (watch.product_name, watch.product_aliases, watch.key_features) == ("", None, None)
    assert (review.product_name, review.product_aliases, review.key_features) == ("", None, None)
    # An input recorded with a product name but before the "Key features"
    # field (2026-11-25) decodes to no key features: no feature terms
    named = dict(_payload(_events(REVIEW_HISTORY[0])[0], "workflowExecutionStartedEventAttributes"),
                 product_name="Acme", product_aliases=["Acme Cloud"])
    assert value_to_type(ReviewInput, named).key_features is None
    check_input = value_to_type(CheckWrittenDraftInput, {
        "text": "x", "product_brief": "A brief.", "product_name": "Acme", "idempotency_key": "k",
        "product_aliases": [], "mention_product": None, "post_id": "1",
    })
    assert check_input.key_features is None
    plan = {"reply_type": "add_observation", "target_point": "x", "mention_product": True, "mention_product_reason": "y"}
    old_result = value_to_type(PlanReplyResult, {"plan": plan, "errors": []})
    assert (old_result.draft_mode, old_result.draft_mode_note) == (None, None)
    defaults = {f.name: f.default for f in dataclasses.fields(DraftReplyInput)}
    assert (defaults["draft_mode"], defaults["key_features"]) == (None, None)
