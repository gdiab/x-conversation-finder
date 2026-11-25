"""The written drafter (issue #12; ADR-0006 amendment 2026-11-24, step 1).

The model writes the reply as free text and returns, per product sentence,
the brief sentence ids it says support it. That list is never a verdict; it
is kept on the draft as provenance and is not shown to the checker. All calls are stubbed (bare JSON object string in
choices[0].message.content, JSON mode). Synthetic data only.
"""

import dataclasses
import json
from types import SimpleNamespace

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from backend.activities.reply_activities import DraftReplyInput, PlanReplyInput, ReplyActivities
from backend.adapters import FakeReplyAgent
from backend.adapters.plain_reply_agent import PlainReplyAgent, build_written_draft_prompt, parse_written_draft
from backend.gate.brief import split_brief
from backend.models import Evidence, FounderMaterial, ReplyPlan
from backend.models.written import DrafterHint
from backend.planning import NO_PRODUCT_NAME_NOTE, resolve_draft_mode
from backend.ports.reply_agent import WrittenDraftRequest
from backend.tests.written_helpers import RELAY_ALIASES, RELAY_BRIEF, RELAY_NAME

PLAN = ReplyPlan(
    reply_type="answer_question",
    target_point="How do you folks handle eventual consistency?",
    mention_product=True,
    mention_product_reason="The brief covers retries.",
)
NO_MENTION = dataclasses.replace(PLAN, reply_type="add_observation", mention_product=False,
                                 target_point="Struggling with distributed systems reliability.")

DRAFT_JSON = {
    "reply": "Retries are the part people skip.\nRelay can retry a failed delivery up to five times.  How do you notice a miss today?",
    "support": [
        {"sentence": "Relay can retry a failed delivery up to five times.", "brief_sentence_ids": ["b2"]},
    ],
}
PLAN_JSON = {
    "reply_type": "answer_question",
    "target_point": "How do you folks handle eventual consistency?",
    "mention_product": False,
    "mention_product_reason": "Synthetic reason.",
}


class Completions:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self._payloads.pop(0)
        content = payload if isinstance(payload, str) else json.dumps(payload)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=200, completion_tokens=40),
        )


def _agent(payloads):
    completions = Completions(payloads)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return PlainReplyAgent(openai_client=client, require_budget_meter=False), completions


def _evidence():
    return Evidence(
        post_id="1001",
        matched_topic="reliability",
        why_it_fits="The post asks how to handle eventual consistency.",
        confidence_score=0.8,
        confidence_inputs={},
        founder_materials=[FounderMaterial(
            source="product_brief", passage="Relay can retry a failed delivery up to five times.",
            relevance="retries", passage_id="passage_0",
        )],
    )


def _request(sample_post, plan=PLAN, steer=None, evidence=None):
    return WrittenDraftRequest(
        post=sample_post,
        evidence=evidence or _evidence(),
        plan=plan,
        brief_sentences=split_brief(RELAY_BRIEF),
        product_name=RELAY_NAME,
        product_aliases=RELAY_ALIASES,
        feature_terms=["Signed deliveries", "Delivery replay"],
        steer=steer,
    )


def _env(activity_id="3"):
    """One activity attempt. Each paid call in a test needs its own activity
    id: the budget keys a reservation by workflow, run, activity and attempt."""
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info, workflow_id="review:synthetic-draft", workflow_run_id="run-1", activity_id=activity_id
    )
    return env


# ---------- the plain adapter ----------


@pytest.mark.asyncio
async def test_written_draft_is_free_text_with_hints_and_no_segments(sample_post):
    agent, completions = _agent([DRAFT_JSON])

    evidence = await agent.draft_written(_request(sample_post), "key")

    draft = evidence.draft
    assert draft.mode == "written"
    # Spaces are tidied; the line break stays, because the check reads it as a sentence end
    assert draft.text == (
        "Retries are the part people skip.\n"
        "Relay can retry a failed delivery up to five times. How do you notice a miss today?"
    )
    assert draft.segments == []
    assert draft.claim_gate.passed is False  # nothing is checked yet
    assert draft.written.drafter_hints == [
        DrafterHint(sentence="Relay can retry a failed delivery up to five times.", brief_sentence_ids=["b2"])
    ]
    assert draft.written.product_name == RELAY_NAME
    assert (draft.input_tokens, draft.output_tokens) == (200, 40)
    assert completions.calls[0]["response_format"] == {"type": "json_object"}


def test_the_prompt_shows_the_post_the_plan_and_the_numbered_brief(sample_post):
    prompt = build_written_draft_prompt(_request(sample_post))

    assert sample_post.text in prompt
    assert "Reply plan (decided before drafting; follow it):" in prompt
    assert f'Respond to this part of the post: "{PLAN.target_point}"' in prompt
    assert "[b2] (Acme Relay) Relay can retry a failed delivery up to five times." in prompt
    assert "[b7] (Acme Relay > Key features) Signed deliveries" in prompt
    assert "founder of Acme Relay (also called Relay)" in prompt
    assert "At most 3 sentences and at most 280 characters" in prompt
    assert "Exactly one sentence says something about Acme Relay" in prompt
    assert "key features: Signed deliveries, Delivery replay" in prompt
    assert '"support"' in prompt


def test_the_prompt_follows_a_plan_that_does_not_mention_the_product(sample_post):
    prompt = build_written_draft_prompt(_request(sample_post, plan=NO_MENTION))

    assert "Do not mention Acme Relay" in prompt
    assert "Exactly one sentence says something about" not in prompt


def test_a_steer_shows_the_previous_draft_and_the_instruction(sample_post):
    first = build_written_draft_prompt(_request(sample_post))
    assert "Previous draft" not in first and "Additional instruction" not in first

    evidence = FakeEvidenceWithDraft.build()
    prompt = build_written_draft_prompt(_request(sample_post, steer="Make it shorter", evidence=evidence))

    assert "Previous draft (rewrite it, do not start over):\nAn earlier synthetic draft." in prompt
    assert "Additional instruction: Make it shorter" in prompt


class FakeEvidenceWithDraft:
    @staticmethod
    def build():
        from backend.models import ClaimGateResult, Draft
        evidence = _evidence()
        evidence.draft = Draft(
            text="An earlier synthetic draft.", segments=[], mode="written",
            claim_gate=ClaimGateResult(passed=True, segments=[], failed_sentences=[], reasoning=""),
            steers_applied=["an earlier steer"],
        )
        return evidence


@pytest.mark.asyncio
async def test_steers_accumulate_on_the_draft(sample_post):
    agent, _ = _agent([DRAFT_JSON])

    evidence = await agent.draft_written(
        _request(sample_post, steer="Make it shorter", evidence=FakeEvidenceWithDraft.build()), "key"
    )

    assert evidence.draft.steers_applied == ["an earlier steer", "Make it shorter"]


@pytest.mark.parametrize("content,text,hints", [
    ("not json", "", []),
    ("[]", "", []),
    ('{"reply": 5, "support": "none"}', "", []),
    ('{"reply": "One sentence.", "support": [{"sentence": 1}, "x", {"sentence": "One sentence.", "brief_sentence_ids": "b1"}]}',
     "One sentence.", [DrafterHint(sentence="One sentence.", brief_sentence_ids=[])]),
])
def test_malformed_output_is_read_as_empty_not_raised(content, text, hints):
    assert parse_written_draft(content) == (text, hints)


def test_line_breaks_survive_so_unpunctuated_lines_still_count_as_sentences():
    """Four lines without full stops are four sentences to the check; joined
    with spaces they would be one."""
    from backend.gate.written_check import prepare_check

    content = json.dumps({"reply": "Acme retries \n\n  Acme ships\nAcme scales\nAcme works", "support": []})

    text, _ = parse_written_draft(content)

    assert text == "Acme retries\nAcme ships\nAcme scales\nAcme works"
    prepared = prepare_check(text, [], "Acme")
    assert len(prepared.sentences) == 4
    assert any("at most 3 sentences (got 4)" in reason for _, reason in prepared.structural_failures)


@pytest.mark.asyncio
async def test_an_unusable_answer_gives_an_empty_draft_that_the_check_will_fail(sample_post):
    agent, _ = _agent(["not json"])

    evidence = await agent.draft_written(_request(sample_post), "key")

    assert evidence.draft.text == "" and evidence.draft.mode == "written"


# ---------- the draft_reply activity ----------


@pytest.mark.asyncio
async def test_draft_reply_writes_when_the_reviews_mode_is_written(sample_post):
    agent, completions = _agent([DRAFT_JSON])
    activities = ReplyActivities(PlainReplyAgent(openai_client=agent.client))

    evidence = await _env().run(activities.draft_reply, DraftReplyInput(
        evidence=_evidence(), plan=PLAN, idempotency_key="k", draft_mode="written",
        post=sample_post, product_brief=RELAY_BRIEF, product_name=" Acme  Relay ", product_aliases=["Relay", ""],
    ))

    assert evidence.draft.mode == "written"
    assert evidence.draft.written.product_name == "Acme Relay"
    assert evidence.draft.written.product_aliases == ["Relay"]
    # Plan conformance of a written draft is decided by check_written_draft
    assert evidence.draft.plan_conformance_failures == []
    assert sample_post.text in completions.calls[0]["messages"][1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "assembled"])
async def test_draft_reply_assembles_unless_the_mode_is_written(mode, sample_post):
    """A DraftReplyInput recorded before the field existed has no mode."""
    activities = ReplyActivities(FakeReplyAgent())

    evidence = await _env().run(activities.draft_reply, DraftReplyInput(
        evidence=_evidence(), plan=PLAN, idempotency_key="k", draft_mode=mode,
    ))

    assert evidence.draft.mode == "assembled"
    assert evidence.draft.segments and evidence.draft.written is None


@pytest.mark.asyncio
async def test_a_written_draft_needs_the_plan_and_the_post(sample_post):
    activities = ReplyActivities(FakeReplyAgent())

    with pytest.raises(ApplicationError) as info:
        await _env().run(activities.draft_reply, DraftReplyInput(
            evidence=_evidence(), plan=None, idempotency_key="k", draft_mode="written", post=sample_post,
        ))

    assert info.value.non_retryable


# ---------- the fake adapter ----------


@pytest.mark.asyncio
async def test_fake_written_draft_is_a_reaction_a_brief_sentence_and_a_question(sample_post):
    evidence = await FakeReplyAgent().draft_written(_request(sample_post), "key")

    assert evidence.draft.text == (
        "Good question, and a hard thing to get right. Acme Relay delivers webhooks to your services. "
        "What are you using today?"
    )
    assert evidence.draft.written.drafter_hints == [
        DrafterHint(sentence="Acme Relay delivers webhooks to your services.", brief_sentence_ids=["b1"])
    ]


@pytest.mark.asyncio
async def test_fake_written_draft_follows_the_plan_and_the_steers(sample_post):
    agent = FakeReplyAgent()

    no_mention = await agent.draft_written(_request(sample_post, plan=NO_MENTION), "key")
    shorter = await agent.draft_written(_request(sample_post, steer="Make it shorter"), "key")
    technical = await agent.draft_written(_request(sample_post, steer="More technical"), "key")

    assert no_mention.draft.text == "That is a hard thing to get right. What are you using today?"
    assert shorter.draft.text == (
        "Good question, and a hard thing to get right. Acme Relay delivers webhooks to your services."
    )
    assert "Signed deliveries are available on the Team plan." in technical.draft.text


# ---------- the draft mode, fixed by plan_reply ----------


@pytest.mark.parametrize("setting,name,expected", [
    ("assembled", "Acme Relay", ("assembled", None)),
    ("assembled", "", ("assembled", None)),
    ("written", "Acme Relay", ("written", None)),
    ("written", "", ("assembled", NO_PRODUCT_NAME_NOTE)),
    ("written", "   ", ("assembled", NO_PRODUCT_NAME_NOTE)),
    ("written", None, ("assembled", NO_PRODUCT_NAME_NOTE)),
])
def test_resolve_draft_mode(setting, name, expected):
    assert resolve_draft_mode(setting, name) == expected


def _plan_input(sample_post, product_name):
    return PlanReplyInput(
        post=sample_post, evidence=_evidence(), idempotency_key="k", product_name=product_name
    )


@pytest.mark.asyncio
async def test_plan_reply_fixes_written_mode_and_switches_needs_written_drafts_off(sample_post):
    """answer_question without the product is skipped under assembled drafts
    (issue #5) and carried out under written drafts."""
    agent, _ = _agent([PLAN_JSON, PLAN_JSON])
    written = ReplyActivities(agent, draft_mode="written")
    assembled = ReplyActivities(agent, draft_mode="assembled")

    in_written = await _env("1").run(written.plan_reply, _plan_input(sample_post, "Acme Relay"))
    in_assembled = await _env("2").run(assembled.plan_reply, _plan_input(sample_post, "Acme Relay"))

    assert (in_written.draft_mode, in_written.skip_rule, in_written.skip_reason) == ("written", None, None)
    assert in_assembled.draft_mode == "assembled"
    assert in_assembled.skip_rule == "needs_written_drafts"


@pytest.mark.asyncio
async def test_a_watch_without_a_product_name_stays_assembled_with_a_note(sample_post):
    agent, _ = _agent([PLAN_JSON])
    activities = ReplyActivities(agent, draft_mode="written")

    result = await _env().run(activities.plan_reply, _plan_input(sample_post, ""))

    assert result.draft_mode == "assembled"
    assert result.draft_mode_note == NO_PRODUCT_NAME_NOTE
    assert result.skip_rule == "needs_written_drafts"  # the assembled rule still applies


@pytest.mark.asyncio
async def test_plan_reply_reads_the_draft_mode_setting_by_default(sample_post, monkeypatch):
    from backend.config import get_settings

    agent, _ = _agent([PLAN_JSON, PLAN_JSON])
    activities = ReplyActivities(agent)

    assert (await _env("1").run(activities.plan_reply, _plan_input(sample_post, "Acme"))).draft_mode == "assembled"
    monkeypatch.setattr(get_settings(), "draft_mode", "written")
    assert (await _env("2").run(activities.plan_reply, _plan_input(sample_post, "Acme"))).draft_mode == "written"
