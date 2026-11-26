"""Issue #41: drafts that name the product end with the watch's product URL.

The Start Watch form's "Product URL" is stored on the watch and passed to its
Reviews. After a draft passes its check, code appends " <url>"; the model is
never asked to write it. Only drafts that name the product get it (the plan
says to mention the product, or a sentence is checked as a product claim).
The check runs on the sentences without the URL, which get 280 - 24 = 256
characters with it. A founder edit keeps the link unless the founder deletes
it, and the check of the edit ignores a trailing product URL without
requiring it. Watches and Reviews started without a URL behave as before.

Everything runs with fake adapters and a temporary database.
"""

import asyncio
import base64
import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError
from temporalio.client import WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from backend import api
from backend.gate.brief import split_brief
from backend.gate.product_link import (
    PRODUCT_LINK_CHARS,
    append_product_link,
    product_url_error,
    strip_product_link,
    writer_max_chars,
)
from backend.gate.written_check import judge, prepare_check
from backend.models import ReplyPlan
from backend.models.written import ClaimVerdict
from backend.review_docs import text_digest
from backend.tests.review_waits import query_card, wait_for_card_ready
from backend.tests.test_edit_regate import fetch_history_past_approve
from backend.tests.test_written_workflow import (
    CLAIM,
    CLOSER,
    FAKE_DRAFT,
    OPENER,
    PRODUCT,
    WritingAgent,
    _worker,
)
from backend.workflows.review import ReviewWorkflow
from backend.workflows.topic_watch import TopicWatchWorkflow

URL = "https://github.com/example-org/acme"
APPROVE_URL = "https://x.com/test/status/999"
FIXTURES = Path(__file__).parent / "fixtures"
# No trigger (no number, name, we/our or comparison), so the fake checker
# reads it as not about the product; long enough to pass 256 with the claim
LONG_QUESTION = (
    "What does your setup look like when a node drops out in the middle of a write and the rest "
    "of the cluster has to keep serving reads for every user in the region?"
)


def mention_plan(mention: bool = True, reply_type: str = "answer_question") -> ReplyPlan:
    """A valid plan for the conftest post."""
    return ReplyPlan(
        reply_type=reply_type,
        target_point="How do you folks handle eventual consistency?",
        mention_product=mention,
        mention_product_reason="They ask how to handle it." if not mention else "They ask; the product does.",
    )


def _linked(text: str) -> str:
    return f"{text} {URL}"


async def _start(env, make_review_input, suffix, **overrides):
    overrides.setdefault("product_name", PRODUCT)
    review_input = make_review_input(**overrides)
    return await env.client.start_workflow(
        ReviewWorkflow.run, review_input, id=f"review:test:{review_input.post_id}_{suffix}", task_queue="test"
    )


async def _approve(handle, text, gate_state=None, post_anyway=False):
    await handle.execute_update("approve", args=[
        APPROVE_URL, str(uuid.uuid4()), post_anyway, {"text_sha256": text_digest(text), "gate_state": gate_state},
    ])
    return await fetch_history_past_approve(handle)


# ---------- the link itself ----------


def test_append_and_strip_the_link():
    assert append_product_link("Hi there. ", URL) == f"Hi there. {URL}"
    assert strip_product_link(f"Hi there. {URL}", URL) == ("Hi there.", True)
    assert strip_product_link(f"Hi there.\n{URL}\n", URL) == ("Hi there.", True)
    # Not the last word, not the same URL, glued to the text, or no URL set
    for text in (f"See {URL} today.", f"Hi. {URL}.", f"Hi. {URL}/docs", f"Hi.{URL}", "Hi."):
        assert strip_product_link(text, URL) == (text, False)
    assert strip_product_link(f"Hi. {URL}", None) == (f"Hi. {URL}", False)


def test_the_writer_budget_leaves_room_for_the_link_only_when_the_plan_names_the_product():
    assert PRODUCT_LINK_CHARS == 24
    assert writer_max_chars(280, URL, True) == 256
    assert writer_max_chars(280, URL, False) == 280
    assert writer_max_chars(280, None, True) == 280


@pytest.mark.parametrize("url,ok", [
    ("https://github.com/clickety-clacks/tightbeam", True),
    ("https://example.com", True),
    ("http://example.com", False),
    ("example.com", False),
    ("https://", False),
    ("https://localhost", False),
    ("https://exa mple.com", False),
    ("ftp://example.com", False),
    ("", False),
])
def test_a_product_url_is_an_https_link_to_a_site(url, ok):
    assert (product_url_error(url) is None) == ok


def _request(**overrides):
    fields = dict(account="founder", topic="reliability", query="reliability", product_brief="A brief.",
                  product_name="Acme Relay", product_url="  https://example.com/relay ")
    fields.update(overrides)
    return api.StartWatchRequest(**fields)


def test_the_start_watch_request_requires_an_https_product_url():
    assert _request().product_url == "https://example.com/relay"  # trimmed
    fields = dict(account="founder", topic="t", query="q", product_brief="b", product_name="Acme")
    with pytest.raises(ValidationError) as missing:
        api.StartWatchRequest(**fields)
    assert "product_url" in str(missing.value)
    with pytest.raises(ValidationError) as insecure:
        _request(product_url="http://example.com")
    assert "must start with https://" in str(insecure.value)


# ---------- the check runs on the sentences without the link ----------


def _prepared(text, **kw):
    return prepare_check(text, split_brief("Acme retries failed deliveries."), "Acme", [], [], **kw)


def test_with_the_link_a_text_that_names_the_product_gets_256_characters():
    claim = "Acme retries failed deliveries."
    filler = "That sounds like a rough week for your team, and I hope the fix holds up."
    text = f"{filler} {claim} " + "x" * (256 - len(filler) - len(claim) - 2)
    assert len(text) == 256
    assert not _prepared(text, mention_product=True, product_link_chars=PRODUCT_LINK_CHARS).structural_failures
    longer = text + "x"
    [failure] = _prepared(longer, mention_product=True, product_link_chars=PRODUCT_LINK_CHARS).structural_failures
    assert failure[0] == "[reply]"
    assert "at most 256 characters" in failure[1] and "(got 257)" in failure[1]
    # A trigger alone (the plan's decision unknown) also means the link follows
    assert _prepared(longer, product_link_chars=PRODUCT_LINK_CHARS).structural_failures
    # No product link: X's 280, as before
    assert not _prepared(longer, mention_product=True).structural_failures


def test_a_text_that_does_not_name_the_product_keeps_280_characters():
    text = "That sounds like a rough week for your team, " * 6
    text = text[:269].strip() + "."
    assert len(text) > 256
    prepared = _prepared(text, mention_product=False, product_link_chars=PRODUCT_LINK_CHARS)
    assert not prepared.structural_failures


def test_a_claim_only_the_checker_found_gets_the_link_budget_too():
    """No trigger fired, but the checker reads a sentence as a supported
    product claim: the link follows, so 256 applies after the verdicts."""
    first = "The retry layer handles failed deliveries for you."
    second = "a" * (260 - len(first) - 1)
    text = f"{first} {second}"
    prepared = _prepared(text, product_link_chars=PRODUCT_LINK_CHARS)
    assert prepared.needs_checker
    verdicts = [
        ClaimVerdict(index=1, about_product=True, verdict="supported", brief_sentence_ids=["b1"]),
        ClaimVerdict(index=2, about_product=False),
    ]
    result = judge(prepared, verdicts)
    assert not result.passed
    assert any("at most 256 characters" in reason for _, reason in result.failures)
    # The same text without the link budget passes
    assert judge(_prepared(text), verdicts).passed


# ---------- written drafts (the live app's DRAFT_MODE) ----------


@pytest.mark.asyncio
async def test_a_written_draft_that_names_the_product_ends_with_exactly_one_link(make_review_input):
    agent = WritingAgent(plans=[mention_plan()])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent):
            handle = await _start(env, make_review_input, "41_written", product_url=URL)
            card = await wait_for_card_ready(handle)
            draft = card["evidence"]["draft"]
            assert draft["text"] == _linked(FAKE_DRAFT)
            assert draft["text"].count(URL) == 1
            # The check ran on the sentences, without the URL
            assert [c["sentence"] for c in draft["sentence_checks"]] == [OPENER, CLAIM, CLOSER]
            assert draft["claim_gate"]["passed"]
            # The writer was given the budget X leaves after the link, and no URL
            assert agent.requests[0].max_chars == 256
            assert URL not in agent.prompts[0]
            assert "Do not write the link" in agent.prompts[0]
            # The founder copies and approves the text with the link
            history = await _approve(handle, draft["text"])
            assert (await query_card(handle))["status"] == "approved"
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_a_written_draft_that_does_not_name_the_product_gets_no_link(make_review_input):
    text = "Good question about distributed clusters. What are you using today?"
    agent = WritingAgent(texts=[text], plans=[mention_plan(False)])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent):
            handle = await _start(env, make_review_input, "41_written_no_mention", product_url=URL)
            card = await wait_for_card_ready(handle)
            assert card["evidence"]["draft"]["text"] == text
            assert agent.requests[0].max_chars == 280
            await _approve(handle, text)


@pytest.mark.asyncio
async def test_a_review_without_a_product_url_drafts_as_before(make_review_input):
    agent = WritingAgent(plans=[mention_plan()])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent):
            handle = await _start(env, make_review_input, "41_no_url")
            card = await wait_for_card_ready(handle)
            assert card["evidence"]["draft"]["text"] == FAKE_DRAFT
            assert agent.requests[0].max_chars == 280
            assert "Do not write the link" not in agent.prompts[0]
            await _approve(handle, FAKE_DRAFT)


@pytest.mark.asyncio
async def test_a_link_the_model_wrote_is_not_doubled(make_review_input):
    agent = WritingAgent(texts=[_linked(FAKE_DRAFT)], plans=[mention_plan()])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent):
            handle = await _start(env, make_review_input, "41_model_link", product_url=URL)
            card = await wait_for_card_ready(handle)
            assert card["evidence"]["draft"]["text"] == _linked(FAKE_DRAFT)


@pytest.mark.asyncio
async def test_a_written_draft_too_long_for_the_link_is_redrafted(make_review_input):
    too_long = f"{OPENER} {CLAIM} {LONG_QUESTION}"
    assert 256 < len(too_long) <= 280
    agent = WritingAgent(texts=[too_long, FAKE_DRAFT], plans=[mention_plan()])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent):
            handle = await _start(env, make_review_input, "41_too_long", product_url=URL)
            card = await wait_for_card_ready(handle)
            assert card["evidence"]["draft"]["text"] == _linked(FAKE_DRAFT)
            assert len(agent.requests) == 2
            # The redraft was told why, and was shown the failed draft without a link
            assert "at most 256 characters" in agent.prompts[1]
            assert agent.previous_texts[1] == too_long


@pytest.mark.asyncio
async def test_without_a_url_the_same_long_draft_passes(make_review_input):
    too_long = f"{OPENER} {CLAIM} {LONG_QUESTION}"
    agent = WritingAgent(texts=[too_long], plans=[mention_plan()])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent):
            handle = await _start(env, make_review_input, "41_long_no_url")
            card = await wait_for_card_ready(handle)
            assert card["evidence"]["draft"]["text"] == too_long


@pytest.mark.asyncio
async def test_an_edit_keeps_the_link_and_its_check_ignores_it(make_review_input):
    agent = WritingAgent(plans=[mention_plan()])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent):
            handle = await _start(env, make_review_input, "41_edit", product_url=URL)
            await wait_for_card_ready(handle)
            body = f"Hard thing to get right. {CLAIM}"

            with_link = _linked(body)
            assert await handle.execute_update("edit", args=[with_link, str(uuid.uuid4())]) == with_link
            linked_card = await query_card(handle)
            assert linked_card["evidence"]["draft"]["text"] == with_link
            assert linked_card["edited_text_gate_state"] == "passed"

            # The founder deletes the link: allowed, and checked the same way
            assert await handle.execute_update("edit", args=[body, str(uuid.uuid4())]) == body
            plain_card = await query_card(handle)
            assert plain_card["edited_text_gate_state"] == "passed"

            def checks(card):
                return [(r["sentence"], r["passed"], r["kind"], r["verdict"]) for r in card["edited_text_gate_results"]]

            assert checks(linked_card) == checks(plain_card)
            assert all(URL not in r["sentence"] for r in linked_card["edited_text_gate_results"])

            # A long edit with the link gets 256 characters before it
            long_edit = _linked(f"{OPENER} {CLAIM} {LONG_QUESTION}")
            await handle.execute_update("edit", args=[long_edit, str(uuid.uuid4())])
            long_card = await query_card(handle)
            assert long_card["edited_text_gate_state"] == "failed"
            assert any("at most 256 characters" in r["reason"] for r in long_card["edited_text_gate_results"])
            history = await _approve(handle, long_edit, gate_state="failed", post_anyway=True)
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_a_nudged_redraft_gets_the_link_once_and_the_drafter_never_sees_it(make_review_input):
    nudged = f"Fair question. {CLAIM}"
    agent = WritingAgent(texts=[FAKE_DRAFT, nudged], plans=[mention_plan()])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent):
            handle = await _start(env, make_review_input, "41_nudge", product_url=URL)
            await wait_for_card_ready(handle)
            await handle.execute_update("nudge", args=["Shorter please", str(uuid.uuid4())])
            card = await query_card(handle)
            assert card["evidence"]["draft"]["text"] == _linked(nudged)
            assert agent.previous_texts[1] == FAKE_DRAFT  # the current draft, without its link
            history = await _approve(handle, _linked(nudged))
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_a_nudge_after_an_edit_shows_the_drafter_the_edit_without_its_link(make_review_input):
    edit = f"Hard thing to get right. {CLAIM}"
    agent = WritingAgent(texts=[FAKE_DRAFT, FAKE_DRAFT], plans=[mention_plan()])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent):
            handle = await _start(env, make_review_input, "41_edit_nudge", product_url=URL)
            await wait_for_card_ready(handle)
            await handle.execute_update("edit", args=[_linked(edit), str(uuid.uuid4())])
            await handle.execute_update("nudge", args=["Warmer please", str(uuid.uuid4())])
            assert agent.previous_texts[1] == edit
            assert (await query_card(handle))["evidence"]["draft"]["text"] == _linked(FAKE_DRAFT)


# ---------- assembled drafts behave the same ----------


@pytest.mark.asyncio
async def test_an_assembled_draft_that_names_the_product_ends_with_exactly_one_link(make_review_input):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, draft_mode="assembled"):
            handle = await _start(env, make_review_input, "41_assembled", product_url=URL)
            card = await wait_for_card_ready(handle)
            draft = card["evidence"]["draft"]
            assert any(seg["kind"] == "claim" for seg in draft["segments"])
            assert draft["text"].endswith(f" {URL}") and draft["text"].count(URL) == 1
            body = draft["text"][: -len(URL) - 1]
            # Gated without the link
            assert URL not in body and draft["claim_gate"]["passed"]

            # An edit that keeps the link is checked without it, like one without
            await handle.execute_update("edit", args=[draft["text"], str(uuid.uuid4())])
            linked = await query_card(handle)
            await handle.execute_update("edit", args=[body, str(uuid.uuid4())])
            plain = await query_card(handle)
            assert linked["edited_text_gate_state"] == plain["edited_text_gate_state"] == "passed"
            assert [r["sentence"] for r in linked["edited_text_gate_results"]] == [
                r["sentence"] for r in plain["edited_text_gate_results"]
            ]

            # A nudged redraft gets the link again
            nudged = await handle.execute_update("nudge", args=["Shorter please", str(uuid.uuid4())])
            assert nudged.endswith(f" {URL}") and nudged.count(URL) == 1
            history = await _approve(handle, nudged)
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_an_assembled_draft_without_a_claim_gets_no_link(make_review_input):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # add_observation: the assembled-mode needs_written_drafts rule keeps it
        agent = WritingAgent(plans=[mention_plan(False, "add_observation")])
        async with _worker(env, agent=agent, draft_mode="assembled"):
            handle = await _start(env, make_review_input, "41_assembled_no_claim", product_url=URL)
            card = await wait_for_card_ready(handle)
            draft = card["evidence"]["draft"]
            assert not any(seg["kind"] == "claim" for seg in draft["segments"])
            assert URL not in draft["text"]


@pytest.mark.asyncio
async def test_an_assembled_review_without_a_product_url_drafts_as_before(make_review_input):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, draft_mode="assembled"):
            handle = await _start(env, make_review_input, "41_assembled_no_url")
            card = await wait_for_card_ready(handle)
            assert "https://" not in card["evidence"]["draft"]["text"]


# ---------- a watch passes the URL to its Reviews ----------


@pytest.mark.asyncio
async def test_a_watch_with_a_product_url_gives_its_reviews_linked_drafts(make_topic_watch_input):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, workflows=[TopicWatchWorkflow, ReviewWorkflow]):
            watch = await env.client.start_workflow(
                TopicWatchWorkflow.run,
                make_topic_watch_input(query="reliability distributed systems", product_name=PRODUCT,
                                       product_url=URL),
                id="watch:test:reliability_41", task_queue="test",
            )
            review = env.client.get_workflow_handle("review:test:1001")
            for _ in range(200):
                try:
                    await review.describe()
                    break
                except Exception:
                    await asyncio.sleep(0.05)
            card = await wait_for_card_ready(review)
            assert card["evidence"]["draft"]["text"].endswith(f" {URL}")
            await watch.signal("stop")
            await watch.result()
            await Replayer(workflows=[TopicWatchWorkflow, ReviewWorkflow]).replay_workflow(
                await watch.fetch_history()
            )


# ---------- replay of histories recorded before the change ----------

# Recorded on main d296ec3 (before issue #23's text-free Reviews got a URL):
# a TopicWatch in flight after its first search, as the live watches are;
# a text-free written Review edited, nudged and approved; a text-free
# assembled Review through its follow-up. Synthetic data; worker and client
# identities scrubbed. The live watches run this shape without a URL.
MAIN_D296EC3_HISTORIES = [
    ("topic_watch_history_main_d296ec3_in_flight.json", "watch:test:reliability_main_d296ec3_in_flight"),
    ("review_history_main_d296ec3_text_free_written_edit_nudge_approve.json",
     "review:test:1001_main_d296ec3_written"),
    ("review_history_main_d296ec3_text_free_assembled_followup.json", "review:test:1001_main_d296ec3_assembled"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture,workflow_id", MAIN_D296EC3_HISTORIES)
async def test_text_free_histories_recorded_before_the_product_url_replay(fixture, workflow_id):
    history = WorkflowHistory.from_json(workflow_id, (FIXTURES / fixture).read_text())
    await Replayer(workflows=[TopicWatchWorkflow, ReviewWorkflow]).replay_workflow(history)


def test_the_recorded_histories_have_no_product_url_and_are_text_free():
    """Control: the fixtures are the shape the live workflows have."""
    for fixture, _ in MAIN_D296EC3_HISTORIES:
        data = (FIXTURES / fixture).read_text()
        assert "product_url" not in data
        events = json.loads(data)["events"]
        started = events[0]["workflowExecutionStartedEventAttributes"]["input"]["payloads"][0]["data"]
        start_input = json.loads(base64.b64decode(started))
        assert start_input.get("post") is None


# ---------- the redraft harness measures with the same budget ----------


@pytest.mark.asyncio
async def test_the_redraft_harness_checks_with_the_link_budget(product_brief):
    from backend.adapters.fake_claim_checker import FakeClaimChecker
    from backend.eval.written_harness import WatchSettings, check_text

    too_long = f"{OPENER} {CLAIM} {LONG_QUESTION}"
    with_url = WatchSettings(product_brief=product_brief, product_name=PRODUCT, product_url=URL)
    without = WatchSettings(product_brief=product_brief, product_name=PRODUCT)
    assert not (await check_text(too_long, with_url, True, FakeClaimChecker(), "k1")).passed
    assert (await check_text(too_long, without, True, FakeClaimChecker(), "k2")).passed
    # Settings exported before the field still load
    assert WatchSettings(**{"product_brief": "b", "product_name": "A"}).product_url is None


@pytest.mark.asyncio
async def test_the_harness_redraft_never_sees_a_link_the_model_wrote(product_brief, sample_post):
    from backend.adapters.fake_claim_checker import FakeClaimChecker
    from backend.eval.written_harness import StoredReview, WatchSettings, run_written_path
    from backend.models import Evidence

    too_long = f"{OPENER} {CLAIM} {LONG_QUESTION}"
    agent = WritingAgent(texts=[_linked(too_long), FAKE_DRAFT])
    review = StoredReview(
        workflow_id="review:test:1001", topic="reliability", post=sample_post,
        evidence=Evidence(post_id="1001", matched_topic="reliability", why_it_fits="It asks.",
                          confidence_score=0.9, confidence_inputs={}, founder_materials=[]),
    )
    settings = WatchSettings(product_brief=product_brief, product_name=PRODUCT, product_url=URL)
    attempts = await run_written_path(
        review, settings, mention_plan(), agent, FakeClaimChecker(), max_attempts=2,
    )
    assert [a.passed for a in attempts] == [False, True]
    assert attempts[0].text == too_long  # checked without the link
    assert agent.previous_texts[1] == too_long  # and redrafted from it without the link
    assert agent.requests[0].max_chars == 256


@pytest.mark.asyncio
async def test_an_assembled_draft_too_long_for_the_link_is_not_carded(make_review_input):
    """A claim sentence that leaves no room for the link: the draft fails its
    gate with the reason, is redrafted, then skipped. Without a URL it passes."""
    long_claim = (
        "Our product helps developers build reliable distributed systems with automatic failover, "
        "strong consistency guarantees and real-time monitoring for every service in the cluster, "
        "across every region the team runs in, without extra configuration."
    )
    brief = f"# Product Brief\n\n{long_claim}\n"
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, draft_mode="assembled"):
            linked = await _start(env, make_review_input, "41_assembled_long", product_url=URL, product_brief=brief)
            card = await wait_for_card_ready(linked)
            assert card["status"] == "skipped"
            assert "at most 256 characters" in (card.get("skip_reason") or "")
            plain = await _start(env, make_review_input, "41_assembled_long_no_url", product_brief=brief)
            card = await wait_for_card_ready(plain)
            assert card["status"] == "pending"
            assert len(card["evidence"]["draft"]["text"]) > 256


def test_the_link_budget_counts_the_whitespace_typed_before_the_link():
    from backend.gate.product_link import linked_max_chars

    for separator, expected in ((" ", 256), ("\n\n", 255), ("   ", 254)):
        text = f"Hi.{separator}{URL}"
        body, linked = strip_product_link(text, URL)
        assert linked and linked_max_chars(text, URL, body, 280) == expected


def test_a_body_that_already_holds_the_url_gets_no_second_copy():
    body = f"Acme is at {URL} for anyone who wants it."
    assert append_product_link(body, URL) == body


def test_the_harness_counts_a_link_budget_failure_as_too_long():
    from backend.eval.written_harness import failure_kinds

    text = "Acme retries failed deliveries. " + "x" * 240
    result = judge(_prepared(text, mention_product=True, product_link_chars=PRODUCT_LINK_CHARS), None, True)
    assert "too_long" in failure_kinds(result)


@pytest.mark.asyncio
async def test_an_edit_with_extra_whitespace_before_the_link_leaves_x_room(make_review_input):
    agent = WritingAgent(plans=[mention_plan()])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, agent=agent):
            handle = await _start(env, make_review_input, "41_edit_newlines", product_url=URL)
            await wait_for_card_ready(handle)
            # 256 characters before the link: fits after one space, not after two line breaks
            body = f"{OPENER} {CLAIM} " + "z" * (256 - len(OPENER) - len(CLAIM) - 3) + "."
            assert len(body) == 256
            await handle.execute_update("edit", args=[f"{body} {URL}", str(uuid.uuid4())])
            assert (await query_card(handle))["edited_text_gate_state"] == "passed"
            await handle.execute_update("edit", args=[f"{body}\n\n{URL}", str(uuid.uuid4())])
            assert (await query_card(handle))["edited_text_gate_state"] == "failed"


@pytest.mark.asyncio
async def test_an_assembled_claim_that_quotes_the_url_carries_it_once(make_review_input, product_brief):
    brief = product_brief.replace(
        "Our product helps developers build reliable distributed systems.",
        f"Our product helps developers build reliable distributed systems, open source at {URL} today.",
    )
    assert URL in brief
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env, draft_mode="assembled"):
            handle = await _start(env, make_review_input, "41_assembled_quoted_url", product_url=URL,
                                  product_brief=brief)
            card = await wait_for_card_ready(handle)
            assert card["status"] == "pending", card.get("skip_reason")
            draft = card["evidence"]["draft"]
            assert any(URL in (seg.get("text") or "") for seg in draft["segments"])  # the claim quotes it
            assert draft["text"].count(URL) == 1
