"""
Issue #4: decide whether to reply before drafting; confidence separates posts.

The fixture (fixtures/reply_fit_posts.json) holds synthetic posts modeled on
the kinds of post in the first live runs, with the features a research model
would propose for each. The research "model" is a stub OpenAI client that
returns those features as JSON-mode content (a bare JSON object string), the
shape the real API returns with response_format={"type": "json_object"}.
"""

import json
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.activities.claim_gate_activity import claim_gate
from backend.activities.reply_activities import ReplyActivities
from backend.activities.store_activities import get_all_cards, store_card, update_card
from backend.adapters import FakeReplyAgent
from backend.adapters.plain_reply_agent import PlainReplyAgent, parse_reply_fit
from backend.config.confidence import CONFIDENCE_RULE_VERSION, CONFIDENCE_THRESHOLD, POST_KINDS
from backend.gate.reply_fit import assess_reply_fit, check_span, thread_position
from backend.models import Author, Evidence, FounderMaterial, Post, ReplyFit
from backend.workflows.review import ReviewWorkflow

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "reply_fit_posts.json").read_text())
NOW = datetime.fromisoformat(FIXTURE["now"])
BRIEF = FIXTURE["product_brief"]
TOPIC = FIXTURE["topic"]

MATERIALS = [{
    "source": "product_brief",
    "passage": "Our product records what each coding agent did in one durable ledger.",
    "relevance": "Tracks agent work",
}]


class JsonModeCompletions:
    """Stub of chat.completions returning a bare JSON object string, as JSON mode does."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs.get("response_format") == {"type": "json_object"}
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.payload)))],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=80),
        )


def make_post(item: dict) -> Post:
    return Post(
        id=item["id"],
        text=item["text"],
        author=Author(
            id=f"u_{item['id']}",
            username=f"synthetic_{item['id']}",
            name="Synthetic Poster",
            followers_count=item["followers"],
        ),
        created_at=(NOW - timedelta(hours=item["hours_ago"])).isoformat(),
        like_count=0,
        reply_count=0,
        repost_count=0,
        quote_count=0,
        conversation_id=item.get("conversation_id", item["id"]),
        in_reply_to_user_id=item.get("in_reply_to_user_id"),
        reply_settings=item.get("reply_settings", "everyone"),
    )


async def research_and_assess(item: dict) -> tuple[Evidence, JsonModeCompletions]:
    payload = {
        "matched_topic": TOPIC,
        "why_it_fits": "Synthetic.",
        "founder_materials": MATERIALS,
        **item["model"],
    }
    completions = JsonModeCompletions(payload)
    agent = PlainReplyAgent(openai_client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    post = make_post(item)
    evidence = await agent.research(post, TOPIC, BRIEF, "key")
    return assess_reply_fit(evidence, post, BRIEF, now=NOW), completions


# --- fixture: skip rules and threshold ---------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("item", FIXTURE["posts"], ids=[p["id"] for p in FIXTURE["posts"]])
async def test_fixture_post_outcome(item):
    evidence, _ = await research_and_assess(item)
    expect = item["expect"]

    assert evidence.reply_fit.skip_rule == expect["skip_rule"], (item["label"], evidence.skip_reason)
    if expect["skip_rule"]:
        assert evidence.skip_reason, "a skip must record its reason"
    else:
        assert evidence.skip_reason is None

    passes = evidence.skip_reason is None and evidence.confidence_score >= CONFIDENCE_THRESHOLD
    assert passes == expect["passes"], (item["label"], evidence.confidence_score)
    assert evidence.reply_fit.rule_version == CONFIDENCE_RULE_VERSION


@pytest.mark.asyncio
async def test_fixture_covers_the_live_failure_kinds():
    """Promos, roundups, an explainer in another thread, a launch and an essay all skip."""
    by_label = {p["label"]: p for p in FIXTURE["posts"]}
    must_skip = [
        "vendor promo with a discount",
        "event promo, not the poster's product",
        "daily tool-update roundup that ends with a question",
        "weekly link roundup",
        "explainer inside someone else's thread",
        "competitor launch post",
        "pricing and economics essay",
        "news about a model release",
    ]
    for label in must_skip:
        evidence, _ = await research_and_assess(by_label[label])
        assert evidence.skip_reason, label


@pytest.mark.asyncio
async def test_confidence_spreads_across_fixture():
    """v1 put every fixture post in 0.79-0.96; v2 must spread them out."""
    scores = [(await research_and_assess(item))[0].confidence_score for item in FIXTURE["posts"]]

    assert max(scores) - min(scores) >= 0.6
    assert statistics.pstdev(scores) >= 0.2
    assert sum(1 for s in scores if s >= 0.85) <= len(scores) // 4

    passing = [
        item for item in FIXTURE["posts"] if item["expect"]["passes"]
    ]
    passing_scores = [(await research_and_assess(item))[0].confidence_score for item in passing]
    assert max(passing_scores) - min(passing_scores) >= 0.2, "passing posts are ranked, not tied"


@pytest.mark.asyncio
async def test_research_prompt_asks_for_features_not_a_strength():
    evidence, completions = await research_and_assess(FIXTURE["posts"][0])

    prompt = completions.calls[0]["messages"][1]["content"]
    assert "topic_match_strength" not in prompt
    for key in ("post_kind", "brief_problem_span", "in_others_thread", "is_vendor_post"):
        assert key in prompt
    for kind in POST_KINDS:
        assert kind in prompt
    assert "topic_match_strength" not in evidence.confidence_inputs


@pytest.mark.asyncio
async def test_model_typed_numbers_are_ignored():
    """A model that still sends topic_match_strength or a confidence gets no say."""
    item = dict(FIXTURE["posts"][0])
    base, _ = await research_and_assess(item)
    item["model"] = {**item["model"], "topic_match_strength": 0.01, "confidence": 0.01}
    typed, _ = await research_and_assess(item)
    assert typed.confidence_score == base.confidence_score


# --- code checks of the proposed features ------------------------------------

POST_TEXT = "Our agents keep “forgetting” what they did &amp; nobody can tell who changed what."


@pytest.mark.parametrize("span,expected", [
    ("nobody can tell who changed what", "ok"),
    ("NOBODY CAN TELL   who changed what.", "ok"),  # case and spacing are normalized
    ("nobody can tell who changed what?", "not_in_post"),  # punctuation is not
    ('"nobody can tell who changed what"', "ok"),  # wrapping quotes are dropped
    ('keep "forgetting" what they did', "ok"),  # curly quotes in the post
    ("what they did & nobody", "ok"),  # HTML entity in the post
    ("who changed", "too_short"),
    ("nobody knows who changed what", "not_in_post"),
    ("", "missing"),
    ("   ", "missing"),
    (None, "missing"),
    (42, "missing"),
])
def test_check_span(span, expected):
    assert check_span(span, POST_TEXT) == expected


def _post(**kw) -> Post:
    defaults = dict(
        id="p1", text="t", author=Author(id="a1", username="synthetic_a", name="A", followers_count=10),
        created_at=NOW.isoformat(), like_count=0, reply_count=0, repost_count=0, quote_count=0,
        conversation_id="p1",
    )
    defaults.update(kw)
    return Post(**defaults)


@pytest.mark.parametrize("post_kw,model_value,expected", [
    ({}, True, (False, "metadata")),  # starts its own conversation: metadata wins
    ({"conversation_id": "root", "in_reply_to_user_id": "other"}, False, (True, "metadata")),
    ({"conversation_id": "root", "in_reply_to_user_id": "a1"}, True, (True, "model")),  # self-reply
    ({"conversation_id": "root", "in_reply_to_user_id": "a1"}, None, (None, "missing")),
    ({"conversation_id": ""}, False, (False, "model")),
])
def test_thread_position(post_kw, model_value, expected):
    assert thread_position(_post(**post_kw), model_value) == expected


def test_parse_reply_fit_rejects_malformed_values():
    fit = parse_reply_fit({
        "post_kind": "Rant",
        "brief_problem_span": 17,
        "in_others_thread": "yes",
        "is_vendor_post": "FALSE",
    })
    assert fit == ReplyFit(post_kind=None, brief_problem_span=None, in_others_thread=None, is_vendor_post=False)
    assert parse_reply_fit({"post_kind": " Question "}).post_kind == "question"


def _evidence(fit, materials=True) -> Evidence:
    return Evidence(
        post_id="p1", matched_topic="t", why_it_fits="", confidence_score=0.99,
        confidence_inputs={"topic_match_strength": 0.99},
        founder_materials=[FounderMaterial(source="b", passage="Our product records agent work.", relevance="r")]
        if materials else [],
        reply_fit=fit,
    )


def test_adapter_without_features_is_skipped_not_guessed():
    post = _post(text="How do you keep a record of what each coding agent did?")
    evidence = assess_reply_fit(_evidence(None), post, BRIEF, now=NOW)
    assert evidence.reply_fit.skip_rule == "research_incomplete"
    assert "topic_match_strength" not in evidence.confidence_inputs


def test_no_founder_material_skips():
    text = "How do you keep a record of what each coding agent did?"
    fit = ReplyFit(post_kind="question", brief_problem_span=text, in_others_thread=False, is_vendor_post=False)
    evidence = assess_reply_fit(_evidence(fit, materials=False), _post(text=text), BRIEF, now=NOW)
    assert evidence.reply_fit.skip_rule == "no_founder_material"
    assert evidence.confidence_score == 0.0


def test_broadcast_post_with_a_quoted_question_is_not_skipped_as_broadcast():
    text = "Shipped our weekly digest. How do you keep a record of what each coding agent did?"
    fit = ReplyFit(
        post_kind="roundup",
        brief_problem_span="How do you keep a record of what each coding agent did?",
        in_others_thread=False,
        is_vendor_post=False,
    )
    evidence = assess_reply_fit(_evidence(fit), _post(text=text), BRIEF, now=NOW)
    assert evidence.reply_fit.skip_rule is None
    # The roundup kind scores 0, so it ranks below the same post read as a question.
    as_question = ReplyFit(**{**fit.__dict__, "post_kind": "question"})
    question = assess_reply_fit(_evidence(as_question), _post(text=text), BRIEF, now=NOW)
    assert evidence.confidence_score < question.confidence_score - 0.3


# --- workflow: a skipped post never reaches draft_reply ----------------------

class CountingFakeAgent(FakeReplyAgent):
    def __init__(self, fit: ReplyFit):
        self.fit = fit
        self.draft_calls = 0

    async def research(self, post, topic, product_brief, idempotency_key):
        evidence = await super().research(post, topic, product_brief, idempotency_key)
        evidence.reply_fit = self.fit
        return evidence

    async def draft(self, evidence, steer=None, idempotency_key=None):
        self.draft_calls += 1
        return await super().draft(evidence, steer, idempotency_key)


def _fresh_post(text: str) -> Post:
    return Post(
        id="7001", text=text,
        author=Author(id="a7", username="synthetic_vendor", name="Synthetic", followers_count=5000),
        created_at=datetime.now(timezone.utc).isoformat(), like_count=0, reply_count=0,
        repost_count=0, quote_count=0, conversation_id="7001",
    )


async def _run_review(agent, make_review_input, x_activities, post, suffix):
    reply_activities = ReplyActivities(agent)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input(post=post, confidence_threshold=CONFIDENCE_THRESHOLD)
            workflow_id = f"review:test:{post.id}_{suffix}_{uuid.uuid4().hex[:6]}"
            handle = await env.client.start_workflow(
                ReviewWorkflow.run,
                review_input,
                id=workflow_id,
                task_queue="test",
            )
            result = await handle.result()
            card = await handle.query("get_card")
            return result, card


@pytest.mark.asyncio
async def test_skipped_post_makes_no_draft_call(make_review_input, x_activities):
    post = _fresh_post("Our agent toolkit is 40% off this week only. Link below.")
    agent = CountingFakeAgent(ReplyFit(
        post_kind="promo", brief_problem_span=None, in_others_thread=False, is_vendor_post=True,
    ))

    result, card = await _run_review(agent, make_review_input, x_activities, post, "vendor")

    assert result == "skipped"
    assert agent.draft_calls == 0
    assert card["status"] == "skipped"
    assert card["skip_reason"].startswith("Vendor post")
    assert card["evidence"]["draft"] is None
    assert card["evidence"]["skip_reason"] == card["skip_reason"]
    assert card["evidence"]["reply_fit"]["skip_rule"] == "vendor_post"

    # The stored card (what the UI's "Show skipped" section reads) has the reason too.
    stored = [c for c in get_all_cards() if c["workflow_id"] == card["workflow_id"]]
    assert len(stored) == 1
    assert stored[0]["skip_reason"].startswith("Vendor post")
    assert stored[0]["evidence"]["reply_fit"]["skip_rule"] == "vendor_post"


@pytest.mark.asyncio
async def test_low_confidence_skip_records_reason_and_makes_no_draft_call(make_review_input, x_activities):
    text = "Hot take: an agent is only as useful as the record it leaves behind."
    post = _fresh_post(text)
    agent = CountingFakeAgent(ReplyFit(
        post_kind="opinion",
        brief_problem_span="an agent is only as useful as the record it leaves behind",
        in_others_thread=False,
        is_vendor_post=False,
    ))

    result, card = await _run_review(agent, make_review_input, x_activities, post, "lowconf")

    assert result == "skipped"
    assert agent.draft_calls == 0
    assert card["skip_reason"].startswith("Low confidence")
    assert card["evidence"]["reply_fit"]["skip_rule"] is None


@pytest.mark.asyncio
async def test_passing_post_is_drafted(make_review_input, x_activities):
    text = "How do you keep a record of what each coding agent did in the same repository?"
    post = _fresh_post(text)
    agent = CountingFakeAgent(ReplyFit(
        post_kind="question", brief_problem_span=text, in_others_thread=False, is_vendor_post=False,
    ))

    reply_activities = ReplyActivities(agent)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[ReviewWorkflow],
            activities=[
                x_activities.lookup_post,
                x_activities.check_followup,
                reply_activities.research_post,
                reply_activities.draft_reply,
                claim_gate,
                store_card,
                update_card,
            ],
        ):
            review_input = make_review_input(
                post=post, confidence_threshold=CONFIDENCE_THRESHOLD, product_brief=BRIEF,
            )
            handle = await env.client.start_workflow(
                ReviewWorkflow.run, review_input,
                id=f"review:test:{post.id}_pass_{uuid.uuid4().hex[:6]}", task_queue="test",
            )
            await env.sleep(1)
            card = await handle.query("get_card")
            assert card["status"] == "pending"
            assert agent.draft_calls == 1
            assert card["evidence"]["confidence_score"] >= CONFIDENCE_THRESHOLD
            assert card["evidence"]["reply_fit"]["span_check"] == "ok"
            await handle.signal("skip", args=["done", str(uuid.uuid4())])
            assert await handle.result() == "skipped"
