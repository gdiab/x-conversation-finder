"""
PlainReplyAgent: plan() and plan-steered draft() (issue #5).

The OpenAI client is stubbed with JSON-mode shaped content (a bare JSON object
string in choices[0].message.content); see test_plain_reply_agent.py. No real
model calls. Post text is synthetic.
"""

from types import SimpleNamespace

import pytest

from backend.models import Evidence, FounderMaterial, ReplyPlan
from backend.planning import validate_reply_plan
from backend.tests.test_plain_reply_agent import make_agent


def _evidence() -> Evidence:
    return Evidence(
        post_id="1001",
        matched_topic="reliability",
        why_it_fits="The post asks how to handle eventual consistency.",
        confidence_score=0.8,
        confidence_inputs={},
        founder_materials=[
            FounderMaterial(
                source="product_brief",
                passage="Our product provides automatic failover. Our product guarantees strong consistency.",
                relevance="consistency",
                passage_id="passage_0",
            )
        ],
    )


DRAFT_WITH_CLAIM = {
    "segments": [
        {"kind": "non_claim", "phrase_id": "opener.hey"},
        {"kind": "claim", "source_passage_id": "passage_0", "sentence_index": 1},
        {"kind": "non_claim", "phrase_id": "offer.share_details"},
    ]
}


def _prompt(completions, call=0) -> str:
    return completions.calls[call]["messages"][1]["content"]


# ---------- plan() ----------

@pytest.mark.asyncio
async def test_plan_uses_json_mode_and_parses(sample_post):
    payload = {
        "reply_type": "answer_question",
        "target_point": "How do you folks handle eventual consistency?",
        "mention_product": True,
        "mention_product_reason": "The brief covers strong consistency.",
    }
    agent, completions = make_agent([payload])

    plan = await agent.plan(sample_post, _evidence(), "key")

    assert completions.calls[0]["response_format"] == {"type": "json_object"}
    assert plan.reply_type == "answer_question"
    assert plan.target_point == payload["target_point"]
    assert plan.mention_product is True
    assert plan.input_tokens == 100 and plan.output_tokens == 20
    assert validate_reply_plan(plan, sample_post.text) == []
    prompt = _prompt(completions)
    assert sample_post.text in prompt
    assert "Our product guarantees strong consistency." in prompt


@pytest.mark.asyncio
async def test_plan_does_not_coerce_bad_values(sample_post):
    payload = {"reply_type": 7, "target_point": None, "mention_product": "false"}
    agent, _ = make_agent([payload])

    plan = await agent.plan(sample_post, _evidence(), "key")

    assert plan.reply_type == ""
    assert plan.target_point == ""
    assert plan.mention_product is None  # the string "false" is not a boolean
    assert plan.mention_product_reason == ""
    assert len(validate_reply_plan(plan, sample_post.text)) == 4


@pytest.mark.asyncio
async def test_plan_with_unparseable_content_returns_empty_plan(sample_post):
    agent, completions = make_agent([{}])

    async def create(**kwargs):
        completions.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))],
            usage=None,
        )

    completions.create = create
    plan = await agent.plan(sample_post, _evidence(), "key")

    assert plan.reply_type == "" and plan.mention_product is None
    assert validate_reply_plan(plan, sample_post.text)


@pytest.mark.asyncio
async def test_plan_retry_prompt_carries_feedback(sample_post):
    agent, completions = make_agent([{}])

    await agent.plan(sample_post, _evidence(), "key", feedback=["target_point is not an exact substring"])

    prompt = _prompt(completions)
    assert "previous plan was rejected" in prompt
    assert "target_point is not an exact substring" in prompt


# ---------- draft() with a plan ----------

@pytest.mark.asyncio
async def test_draft_without_plan_keeps_existing_rules():
    agent, completions = make_agent([DRAFT_WITH_CLAIM])

    evidence = await agent.draft(_evidence())

    prompt = _prompt(completions)
    assert "Reply plan" not in prompt
    assert "Include exactly one claim: the sentence that speaks most directly to this post." in prompt
    assert [s.kind for s in evidence.draft.segments] == ["non_claim", "claim", "non_claim"]


@pytest.mark.asyncio
async def test_draft_with_plan_puts_plan_and_closer_rule_in_prompt():
    plan = ReplyPlan(
        reply_type="answer_question",
        target_point="How do you folks handle eventual consistency?",
        mention_product=True,
        mention_product_reason="The brief covers strong consistency.",
    )
    agent, completions = make_agent([DRAFT_WITH_CLAIM])

    evidence = await agent.draft(_evidence(), plan=plan)

    prompt = _prompt(completions)
    assert prompt.startswith("Reply plan (decided before drafting; follow it):")
    assert '"How do you folks handle eventual consistency?"' in prompt
    assert "Mention the product: yes (The brief covers strong consistency.)" in prompt
    assert "close with an offer" in prompt
    assert "exactly one claim: the sentence that speaks most directly to the\n  target point" in prompt
    assert "{segments_rule}" not in prompt and "{" + "example}" not in prompt
    assert '"kind": "claim"' in prompt  # example shows a claim
    assert any(s.kind == "claim" for s in evidence.draft.segments)


@pytest.mark.asyncio
async def test_draft_with_no_mention_plan_forbids_claims_in_prompt():
    plan = ReplyPlan(
        reply_type="add_observation",
        target_point="Struggling with distributed systems reliability.",
        mention_product=False,
        mention_product_reason="They are venting, not asking for a tool.",
    )
    # The model disobeys and returns a claim anyway. The adapter returns what the
    # model chose; the draft_reply activity flags it (see test_reply_plan_workflow).
    agent, completions = make_agent([DRAFT_WITH_CLAIM])

    evidence = await agent.draft(_evidence(), plan=plan)

    prompt = _prompt(completions)
    assert "Mention the product: no" in prompt
    assert "Include NO claim segment" in prompt
    assert "1-3 segments, no claim" in prompt
    example = prompt.split("Example (shape only")[1].split("Be conversational")[0]
    assert '"kind": "claim"' not in example  # the example has no claim either
    assert "use\n  q.problem only when the target point describes a problem" in prompt
    assert [s.kind for s in evidence.draft.segments] == ["non_claim", "claim", "non_claim"]
