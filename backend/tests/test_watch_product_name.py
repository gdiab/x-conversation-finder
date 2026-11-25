"""Product name and "Also called" on a watch (issue #12; George's decision of
2026-11-24 on question (b)), the DRAFT_MODE setting, and worker registration.

The two fields are entered in the Start Watch form, stored on the TopicWatch
input and passed to each Review. Fake adapters and synthetic data only.
"""

import asyncio
import base64
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend import api
from backend.activities.claim_gate_activity import claim_gate, verify_edited_text
from backend.activities.reply_activities import ReplyActivities
from backend.activities.store_activities import get_all_cards, store_card, update_card
from backend.activities.written_check_activities import WrittenCheckActivities
from backend.activities.x_activities import XActivities
from backend.adapters import FakeClaimChecker, FakeReplyAgent, FakeXClient
from backend.config import Settings
from backend.tests.review_waits import WAIT_TIMEOUT_SEC
from backend.worker import build_activities
from backend.workflows.review import ReviewWorkflow
from backend.workflows.topic_watch import TopicWatchWorkflow

BACKEND = Path(__file__).resolve().parents[1]


# ---------- the Start Watch request ----------


def _request(**overrides):
    fields = dict(account="founder", topic="reliability", query="reliability", product_brief="A brief.",
                  product_name="Acme Relay")
    fields.update(overrides)
    return api.StartWatchRequest(**fields)


def test_product_name_is_required():
    with pytest.raises(ValidationError) as missing:
        api.StartWatchRequest(account="founder", topic="t", query="q", product_brief="b")
    assert "product_name" in str(missing.value)

    for blank in ("", "   ", "\n"):
        with pytest.raises(ValidationError, match="Product name is required"):
            _request(product_name=blank)


def test_product_name_and_aliases_are_trimmed_and_aliases_are_optional():
    assert _request().product_aliases == []

    request = _request(product_name="  Acme   Relay ", product_aliases=[" Relay ", "", "relay", "AR"])

    assert request.product_name == "Acme Relay"
    assert request.product_aliases == ["Relay", "AR"]


def test_names_have_a_length_and_count_limit():
    with pytest.raises(ValidationError, match="at most 80 characters"):
        _request(product_name="x" * 81)
    with pytest.raises(ValidationError, match="at most 80 characters"):
        _request(product_aliases=["y" * 81])
    with pytest.raises(ValidationError, match="At most 10 aliases"):
        _request(product_aliases=[f"alias {i}" for i in range(11)])


class _RecordingTemporal:
    def __init__(self, error=None):
        self.started = []
        self.error = error

    async def start_workflow(self, run, arg, id, task_queue):
        if self.error:
            raise self.error
        self.started.append((arg, id, task_queue))


@pytest.mark.asyncio
async def test_start_watch_puts_the_fields_on_the_topic_watch_input(monkeypatch):
    temporal = _RecordingTemporal()
    monkeypatch.setattr(api, "temporal_client", temporal)

    response = await api.start_watch(_request(product_aliases=["Relay"]))

    [(watch_input, workflow_id, task_queue)] = temporal.started
    assert (watch_input.product_name, watch_input.product_aliases) == ("Acme Relay", ["Relay"])
    assert workflow_id == "watch:founder:reliability" == response.workflow_id
    assert task_queue == "x-conversation-finder"


@pytest.mark.asyncio
async def test_starting_a_running_watch_says_its_product_name_was_not_changed(monkeypatch):
    monkeypatch.setattr(api, "temporal_client", _RecordingTemporal(RuntimeError("Workflow execution already exists")))

    response = await api.start_watch(_request())

    assert "already running" in response.message
    assert "product name and brief were not changed; stop the watch first" in response.message


# ---------- TopicWatch passes the fields to each Review ----------


def _watch_worker(env, draft_mode):
    x_acts = XActivities(FakeXClient())
    reply = ReplyActivities(FakeReplyAgent(), draft_mode=draft_mode)
    check = WrittenCheckActivities(FakeClaimChecker())
    return Worker(
        env.client,
        task_queue="test",
        workflows=[TopicWatchWorkflow, ReviewWorkflow],
        activities=[
            x_acts.search_recent, x_acts.lookup_post, x_acts.check_followup,
            reply.research_post, reply.plan_reply, reply.draft_reply, check.check_written_draft,
            claim_gate, verify_edited_text, store_card, update_card,
        ],
    )


async def _children_started(handle):
    async def _poll():
        while True:
            history = await handle.fetch_history()
            started = [
                e.start_child_workflow_execution_initiated_event_attributes
                for e in history.events
                if e.HasField("start_child_workflow_execution_initiated_event_attributes")
            ]
            if started and any(e.HasField("timer_started_event_attributes") for e in history.events):
                return started
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(_poll(), WAIT_TIMEOUT_SEC)


async def _drafted_cards(count):
    """Wait until `count` cards with a draft are stored (each Review stores its
    pending card after its draft passed the check)."""
    async def _poll():
        while True:
            drafted = [c for c in get_all_cards() if c["evidence"].get("draft")]
            if len(drafted) >= count:
                return drafted
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(_poll(), WAIT_TIMEOUT_SEC)


@pytest.mark.asyncio
async def test_a_watch_with_a_product_name_gives_its_reviews_written_drafts(make_topic_watch_input):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _watch_worker(env, draft_mode="written"):
            handle = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                make_topic_watch_input(
                    query="reliability distributed systems",
                    product_name="Acme", product_aliases=["Acme Cloud"],
                ),
                id="watch:test:reliability_named", task_queue="test",
            )
            children = await _children_started(handle)

            for child in children:
                review_input = json.loads(child.input.payloads[0].data)
                assert review_input["product_name"] == "Acme"
                assert review_input["product_aliases"] == ["Acme Cloud"]

            [card, *_] = await _drafted_cards(1)
            assert card["evidence"]["draft_mode"] == "written"
            assert card["evidence"]["draft"]["mode"] == "written"
            assert card["evidence"]["draft"]["written"]["product_aliases"] == ["Acme Cloud"]
            await handle.signal("stop")
            await handle.result()


@pytest.mark.asyncio
async def test_a_watch_without_a_product_name_keeps_assembled_drafts_in_written_mode(make_topic_watch_input):
    """An existing watch: its input has no product name."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _watch_worker(env, draft_mode="written"):
            handle = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                make_topic_watch_input(query="reliability distributed systems"),
                id="watch:test:reliability_unnamed", task_queue="test",
            )
            await _children_started(handle)

            [card, *_] = await _drafted_cards(1)
            assert card["evidence"]["draft_mode"] == "assembled"
            assert "started without a product name" in card["evidence"]["draft_mode_note"]
            assert card["evidence"]["draft"]["mode"] == "assembled"
            await handle.signal("stop")
            await handle.result()


@pytest.mark.asyncio
async def test_continue_as_new_carries_the_product_name(make_topic_watch_input):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _watch_worker(env, draft_mode="assembled"):
            handle = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                make_topic_watch_input(
                    query="reliability distributed systems", continue_as_new_after=1,
                    product_name="Acme", product_aliases=["Acme Cloud"],
                ),
                id="watch:test:reliability_can", task_queue="test",
            )
            first = env.client.get_workflow_handle(handle.id, run_id=handle.first_execution_run_id)
            await _children_started(first)

            async def _continued():
                while True:
                    for e in (await first.fetch_history()).events:
                        if e.HasField("workflow_execution_continued_as_new_event_attributes"):
                            return e.workflow_execution_continued_as_new_event_attributes
                    try:
                        await env.sleep(timedelta(minutes=20))
                    except Exception:
                        await asyncio.sleep(0.05)  # the test server can time out a skip while Reviews run

            continued = await asyncio.wait_for(_continued(), WAIT_TIMEOUT_SEC)
            next_input = json.loads(continued.input.payloads[0].data)
            assert next_input["product_name"] == "Acme"
            assert next_input["product_aliases"] == ["Acme Cloud"]
            await env.client.get_workflow_handle(handle.id).signal("stop")


# ---------- the DRAFT_MODE setting ----------


def test_draft_mode_defaults_to_assembled_and_accepts_only_the_two_modes(monkeypatch):
    monkeypatch.delenv("DRAFT_MODE", raising=False)
    assert Settings(_env_file=None).draft_mode == "assembled"
    assert Settings(_env_file=None, draft_mode="written").draft_mode == "written"
    assert Settings(_env_file=None, draft_mode=" Written ").draft_mode == "written"
    with pytest.raises(ValidationError, match="draft_mode must be 'assembled' or 'written'"):
        Settings(_env_file=None, draft_mode="free")


def test_draft_mode_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("DRAFT_MODE", "written")
    assert Settings(_env_file=None).draft_mode == "written"


def test_check_max_output_tokens_has_a_default():
    assert Settings(_env_file=None).check_max_output_tokens == 600


# ---------- the worker registers every activity the workflows call ----------


def _activity_names_called_by_workflows():
    """Activity names in workflow code: string names and imported activity functions."""
    names = set()
    for path in (BACKEND / "workflows").glob("*.py"):
        source = path.read_text()
        names |= set(re.findall(r'execute_activity\(\s*"([a-z_]+)"', source))
        names |= set(re.findall(r"execute_activity\(\s*([a-z_]+),", source))
    return names


def test_worker_registers_every_activity_the_workflows_call():
    registered = {
        fn.__temporal_activity_definition.name
        for fn in build_activities(FakeXClient(), FakeReplyAgent(), FakeClaimChecker())
    }
    called = _activity_names_called_by_workflows()

    # The parse found the calls (a rename would otherwise pass this test vacuously)
    assert {"research_post", "plan_reply", "draft_reply", "check_written_draft", "claim_gate",
            "verify_edited_text", "store_card", "update_card", "delete_post_text", "lookup_post",
            "search_recent", "check_followup"} <= called
    assert called <= registered, f"not registered on the worker: {sorted(called - registered)}"


def test_worker_gives_reply_activities_the_draft_mode():
    activities = build_activities(FakeXClient(), FakeReplyAgent(), FakeClaimChecker(), "written")

    plan_reply = next(fn for fn in activities if fn.__temporal_activity_definition.name == "plan_reply")
    assert plan_reply.__self__._draft_mode == "written"
