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
    assert "exactly one sentence is about Acme Relay" in prompt
    assert "key features: Signed deliveries, Delivery replay" in prompt
    assert '"support"' in prompt


def test_the_prompt_follows_a_plan_that_does_not_mention_the_product(sample_post):
    prompt = build_written_draft_prompt(_request(sample_post, plan=NO_MENTION))

    assert "Do not mention Acme Relay" in prompt
    assert "exactly one sentence is about" not in prompt
    assert "The product sentence" not in prompt
    # A pronoun is a mention too
    assert 'not through "it", "this"' in prompt


def test_a_steer_shows_the_previous_draft_and_the_instruction(sample_post):
    first = build_written_draft_prompt(_request(sample_post))
    assert "Previous draft" not in first and "Additional instruction" not in first

    evidence = FakeEvidenceWithDraft.build()
    prompt = build_written_draft_prompt(_request(sample_post, steer="Make it shorter", evidence=evidence))

    assert "Previous draft (rewrite it, do not start over):\nAn earlier synthetic draft." in prompt
    assert "Additional instruction: Make it shorter" in prompt


# ---------- issue #29: the one-claim rule and the redraft steer ----------


def test_the_prompt_puts_every_product_statement_in_one_sentence(sample_post):
    """The first live run's drafts described the product in a second sentence
    through "it"; the checker counted it as a second product claim."""
    prompt = build_written_draft_prompt(_request(sample_post))

    assert "exactly one sentence is about Acme Relay" in prompt
    assert "Everything the reply says\n  about Acme Relay or its makers goes in that one sentence" in prompt
    assert 'also not through "it", "this", "that tool", "they"' in prompt
    assert "counts each such sentence as a second product claim" in prompt
    assert "Every other sentence is about the post and the poster, not about Acme Relay" in prompt
    # The checker reads a general statement about what a tool does as a product claim
    assert 'do not start a\n  sentence with "It", "This", "That" or "They"' in prompt
    assert "Do not say in general what a tool, setup,\n  approach or practice does or achieves" in prompt
    assert "do not use the words the brief uses for what Acme Relay is or is not" in prompt


def test_the_prompt_keeps_the_product_sentence_close_to_the_brief(sample_post):
    """The first live run's drafts added qualities the brief does not state."""
    prompt = build_written_draft_prompt(_request(sample_post))

    assert "close to the wording of one or two brief sentences" in prompt
    assert "start from a brief sentence's text and change as few words as the\n  reply needs" in prompt
    # A brief sentence under a negating heading never supports the opposite claim
    assert "is not, and never supports the opposite" in prompt
    assert "Do not add a quality, benefit, outcome, number,\n  comparison, customer or integration" in prompt
    assert '"streamlines"' in prompt
    assert "say less, not something else" in prompt
    assert "It must be a\n  statement, not a question." in prompt


def _failed_written_evidence():
    from backend.models import ClaimGateResult, Draft
    from backend.models.written import SentenceCheck

    evidence = _evidence()
    evidence.draft = Draft(
        text="Retries matter. Relay retries forever and it makes queues painless.",
        segments=[], mode="written",
        claim_gate=ClaimGateResult(
            passed=False, segments=[],
            failed_sentences=[[
                "Relay retries forever and it makes queues painless.",
                "The brief does not state that retries are unlimited.",
            ]],
            reasoning="1 failure(s)",
        ),
        sentence_checks=[
            SentenceCheck(sentence="Retries matter.", passed=True, kind="non_claim", verdict="not_a_product_claim"),
            SentenceCheck(
                sentence="Relay retries forever and it makes queues painless.", passed=False, kind="claim",
                reason="The brief does not state that retries are unlimited.", verdict="unsupported",
            ),
        ],
        plan_conformance_failures=["the reply plan says to mention the product, but the draft has no product claim"],
        steers_applied=[],
    )
    return evidence


def test_a_redraft_after_a_failed_check_restates_the_rules_with_the_whole_failed_sentence(sample_post):
    """The workflow's steer cuts each failed sentence to 50 characters; the
    redraft prompt lists the whole sentence, its reason and the rules."""
    steer = "Previous draft failed claim gate. Fix these issues: 'Relay retries forever and it makes queues painl...': x"
    prompt = build_written_draft_prompt(_request(sample_post, steer=steer, evidence=_failed_written_evidence()))

    assert "The previous draft failed the product-claim check:" in prompt
    assert (
        '- "Relay retries forever and it makes queues painless.": '
        "The brief does not state that retries are unlimited."
    ) in prompt
    assert "- The reply as a whole: the reply plan says to mention the product" in prompt
    # The passing sentence is not listed as a failure
    assert '- "Retries matter."' not in prompt
    assert "at most one sentence is about\nthe product" in prompt
    assert "drop that claim instead of rewording it" in prompt
    assert "or says in\ngeneral what a tool or approach does" in prompt
    # The workflow's steer, recorded in history, is still shown as it was
    assert f"Additional instruction: {steer}" in prompt
    # The product rule itself is in the prompt too
    assert "exactly one sentence is about Acme Relay" in prompt


def test_a_nudge_of_a_passing_draft_lists_no_failures(sample_post):
    prompt = build_written_draft_prompt(
        _request(sample_post, steer="Make it shorter", evidence=FakeEvidenceWithDraft.build())
    )

    assert "failed the product-claim check" not in prompt


def test_a_failed_check_without_sentence_reasons_shows_its_reasoning(sample_post):
    evidence = _failed_written_evidence()
    evidence.draft.sentence_checks = []
    evidence.draft.claim_gate.failed_sentences = []
    evidence.draft.plan_conformance_failures = []
    evidence.draft.claim_gate.reasoning = "The reply has 4 sentences; the limit is 3."

    prompt = build_written_draft_prompt(_request(sample_post, steer="x", evidence=evidence))

    assert "failed the product-claim check:\n- The reply has 4 sentences; the limit is 3." in prompt


def test_a_failure_found_before_the_checker_names_only_what_failed(sample_post):
    """Before the checker call every sentence row is marked failed; the
    redraft prompt lists only the failures the check found."""
    from backend.gate.written_check import judge, prepare_check
    from backend.models import ClaimGateResult, Draft
    from backend.workflows.review import record_written_check

    text = "Retries are the part people skip. Does Acme Relay retry a failed delivery?"
    prepared = prepare_check(text, split_brief(RELAY_BRIEF), RELAY_NAME, RELAY_ALIASES, None, True)
    assert not prepared.needs_checker  # a product claim that is a question fails in code
    evidence = _evidence()
    evidence.draft = Draft(
        text=text, segments=[], mode="written", steers_applied=[],
        claim_gate=ClaimGateResult(passed=True, segments=[], failed_sentences=[], reasoning=""),
    )
    record_written_check(evidence.draft, judge(prepared, None, True))
    assert all(not c.passed for c in evidence.draft.sentence_checks)  # every row, as the card shows

    prompt = build_written_draft_prompt(_request(sample_post, steer="x", evidence=evidence))

    assert '- "Does Acme Relay retry a failed delivery?": Checked as a product claim' in prompt
    assert '- "Retries are the part people skip."' not in prompt


def test_a_failure_of_the_whole_reply_is_listed_as_such(sample_post):
    evidence = _failed_written_evidence()
    evidence.draft.claim_gate.failed_sentences = [["[reply]", "The reply must have at most 3 sentences (got 4)"]]
    evidence.draft.plan_conformance_failures = []

    prompt = build_written_draft_prompt(_request(sample_post, steer="x", evidence=evidence))

    assert "- The reply as a whole: The reply must have at most 3 sentences (got 4)" in prompt
    assert '"[reply]"' not in prompt


def test_a_failed_sentence_in_brackets_is_listed_as_a_sentence(sample_post):
    evidence = _failed_written_evidence()
    evidence.draft.claim_gate.failed_sentences = [["[Relay handles 8 tasks]", "a synthetic reason"]]
    evidence.draft.plan_conformance_failures = []

    prompt = build_written_draft_prompt(_request(sample_post, steer="x", evidence=evidence))

    assert '- "[Relay handles 8 tasks]": a synthetic reason' in prompt


def test_an_assembled_previous_draft_lists_no_written_failures(sample_post):
    evidence = _failed_written_evidence()
    evidence.draft.mode = "assembled"

    prompt = build_written_draft_prompt(_request(sample_post, steer="x", evidence=evidence))

    assert "failed the product-claim check" not in prompt


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
