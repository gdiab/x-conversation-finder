"""
Unit tests for reply plan validation (issue #5).

All post text here is synthetic.
"""

import pytest
from temporalio.testing import ActivityEnvironment

from backend.activities.reply_activities import ReplyActivities, PlanReplyInput, PlanReplyResult
from backend.adapters import FakeReplyAgent
from backend.models import Evidence, ReplyPlan, Segment
from backend.planning import (
    MAX_REASON_CHARS,
    plan_conformance_failures,
    question_spans,
    validate_reply_plan,
)

POST = (
    "Shipped our new queue worker last week. Retries are still painful. "
    "How do you folks handle poison messages?"
)


def plan(**overrides) -> ReplyPlan:
    fields = dict(
        reply_type="answer_question",
        target_point="How do you folks handle poison messages?",
        mention_product=True,
        mention_product_reason="The founder material covers dead-letter handling.",
    )
    fields.update(overrides)
    return ReplyPlan(**fields)


# ---------- valid plans ----------

def test_valid_answer_question_plan_passes():
    assert validate_reply_plan(plan(), POST) == []


def test_valid_observation_plan_passes_without_question_target():
    p = plan(reply_type="add_observation", target_point="Retries are still painful.")
    assert validate_reply_plan(p, POST) == []


def test_mention_product_false_is_a_normal_valid_outcome():
    p = plan(
        reply_type="share_experience",
        target_point="Shipped our new queue worker last week",
        mention_product=False,
        mention_product_reason="They are sharing their own work, not asking for a tool.",
    )
    assert validate_reply_plan(p, POST) == []


def test_answer_question_target_may_be_part_of_the_question():
    assert validate_reply_plan(plan(target_point="handle poison messages"), POST) == []


# ---------- substring check ----------

def test_target_point_must_be_exact_substring():
    errors = validate_reply_plan(plan(target_point="How do you handle poison messages?"), POST)
    assert any("not an exact substring" in e for e in errors)


def test_target_point_paraphrase_with_case_change_fails():
    errors = validate_reply_plan(plan(target_point="how do you folks handle poison messages?"), POST)
    assert any("not an exact substring" in e for e in errors)


def test_empty_target_point_fails():
    errors = validate_reply_plan(plan(target_point="   "), POST)
    assert any("target_point is empty" in e for e in errors)


def test_punctuation_only_target_point_fails():
    errors = validate_reply_plan(plan(reply_type="add_observation", target_point="?"), POST)
    assert any("must contain words" in e for e in errors)


# ---------- question check ----------

def test_answer_question_requires_a_question_in_the_post():
    post = "Shipped our new queue worker last week. Retries are still painful."
    p = plan(target_point="Retries are still painful.")
    errors = validate_reply_plan(p, post)
    assert any("requires a question in the post" in e for e in errors)


def test_answer_question_target_must_overlap_the_question():
    errors = validate_reply_plan(plan(target_point="Retries are still painful."), POST)
    assert any("must be the question being answered" in e for e in errors)


def test_answer_question_target_mostly_outside_the_question_fails():
    # Codex round 1: a statement plus the first character of the question used to pass
    post = "The migration failed. What database should I use?"
    errors = validate_reply_plan(plan(target_point="The migration failed. W"), post)
    assert any("must be the question being answered" in e for e in errors)


def test_answer_question_target_may_span_consecutive_questions():
    post = "Is Postgres enough here? Or do we need a queue?"
    assert validate_reply_plan(plan(target_point=post), post) == []


def test_periods_inside_words_and_numbers_do_not_split_the_question():
    # Codex round 2: "Node.js" used to split the question at the period
    post = "Is Node.js suitable? We run v3.5 today."
    assert [post[s:e] for s, e in question_spans(post)] == ["Is Node.js suitable?"]
    assert validate_reply_plan(plan(target_point="Is Node.js suitable?"), post) == []


def test_closing_quote_after_question_ends_the_sentence():
    # Codex round 3: '?"' merged the question with the next statement
    post = 'They asked, "Does it work?" We use Redis.'
    assert [post[s:e] for s, e in question_spans(post)] == ['They asked, "Does it work?"']
    errors = validate_reply_plan(plan(target_point="We use Redis"), post)
    assert any("must be the question being answered" in e for e in errors)
    assert validate_reply_plan(plan(target_point="Does it work?"), post) == []


def test_question_run_into_a_capital_letter_ends_the_sentence():
    # Codex round 4: "working?Yes." was one question span
    post = "Is this working?Yes. It is."
    assert [post[s:e] for s, e in question_spans(post)] == ["Is this working?"]
    errors = validate_reply_plan(plan(target_point="Yes"), post)
    assert any("must be the question being answered" in e for e in errors)


def test_question_run_into_a_lowercase_letter_ends_the_sentence():
    # Codex round 5
    post = "Do you agree?yes, this works."
    assert [post[s:e] for s, e in question_spans(post)] == ["Do you agree?"]
    errors = validate_reply_plan(plan(target_point="yes, this works."), post)
    assert any("must be the question being answered" in e for e in errors)


def test_question_ending_with_url_is_still_a_question():
    # Codex round 1: the trailing "?" was masked together with the URL
    post = "Have you tried https://example.com?"
    assert question_spans(post) == [(0, len(post))]
    assert validate_reply_plan(plan(target_point=post), post) == []


def test_url_query_string_inside_a_question_is_kept_masked():
    post = "Is https://example.com/a?b=1 the right endpoint?"
    assert [post[s:e] for s, e in question_spans(post)] == [post]


def test_question_mark_inside_url_is_not_a_question():
    post = "Notes on our retry setup: https://example.com/notes?page=2 Worth a read."
    assert question_spans(post) == []
    p = plan(target_point="Notes on our retry setup")
    errors = validate_reply_plan(p, post)
    assert any("requires a question in the post" in e for e in errors)


def test_fullwidth_question_mark_counts_as_question():
    post = "Which queue do you recommend？"
    assert question_spans(post) == [(0, len(post))]
    assert validate_reply_plan(plan(target_point="Which queue do you recommend"), post) == []


def test_question_spans_finds_each_question_sentence():
    post = "First point. Is this normal? Second point! Anyone else?"
    spans = question_spans(post)
    assert [post[s:e].strip() for s, e in spans] == ["Is this normal?", "Anyone else?"]


def test_question_check_only_applies_to_answer_question():
    post = "Shipped our new queue worker last week."
    p = plan(reply_type="add_observation", target_point="Shipped our new queue worker")
    assert validate_reply_plan(p, post) == []


# ---------- other fields ----------

def test_unknown_reply_type_fails_without_echoing_model_text():
    errors = validate_reply_plan(plan(reply_type="How do you folks handle poison messages?"), POST)
    assert any("reply_type must be one of" in e for e in errors)
    assert not any("poison" in e for e in errors)


def test_non_boolean_mention_product_fails():
    errors = validate_reply_plan(plan(mention_product=None), POST)
    assert any("mention_product must be true or false" in e for e in errors)


def test_empty_reason_fails():
    errors = validate_reply_plan(plan(mention_product_reason=""), POST)
    assert any("mention_product_reason is empty" in e for e in errors)


def test_multiline_reason_fails():
    errors = validate_reply_plan(plan(mention_product_reason="Line one.\nLine two."), POST)
    assert any("must be one line" in e for e in errors)


def test_overlong_reason_fails():
    errors = validate_reply_plan(plan(mention_product_reason="x" * (MAX_REASON_CHARS + 1)), POST)
    assert any(f"at most {MAX_REASON_CHARS}" in e for e in errors)


def test_all_errors_are_reported_together():
    p = ReplyPlan(reply_type="", target_point="", mention_product=None, mention_product_reason="")
    assert len(validate_reply_plan(p, POST)) == 4


# ---------- draft conformance to the plan (assembled segments) ----------

WITH_CLAIM = [
    Segment(kind="non_claim", phrase_id="thanks.for_sharing"),
    Segment(kind="claim", source_passage_id="passage_0", sentence_index=0),
    Segment(kind="non_claim", phrase_id="q.current_tool"),
]
PHRASES_ONLY = [
    Segment(kind="non_claim", phrase_id="thanks.for_sharing"),
    Segment(kind="non_claim", phrase_id="q.current_tool"),
]


def test_conformance_passes_when_draft_follows_plan():
    assert plan_conformance_failures(WITH_CLAIM, plan()) == []
    assert plan_conformance_failures(PHRASES_ONLY, plan(mention_product=False)) == []


def test_conformance_flags_claim_when_plan_says_no_mention():
    failures = plan_conformance_failures(WITH_CLAIM, plan(mention_product=False))
    assert failures == ["the reply plan says not to mention the product, but the draft has a claim segment"]


def test_conformance_flags_missing_claim_when_plan_says_mention():
    failures = plan_conformance_failures(PHRASES_ONLY, plan())
    assert failures == ["the reply plan says to mention the product, but the draft has no claim segment"]


def test_conformance_flags_empty_draft():
    assert plan_conformance_failures([], plan(mention_product=False)) == ["the draft has no segments"]


def test_conformance_without_plan_checks_nothing():
    assert plan_conformance_failures([], None) == []
    assert plan_conformance_failures(PHRASES_ONLY, None) == []


# ---------- plan_reply activity ----------

class ScriptedPlanAgent(FakeReplyAgent):
    def __init__(self, reply_plan: ReplyPlan):
        self.reply_plan = reply_plan
        self.feedback_seen = []

    async def plan(self, post, evidence, idempotency_key, feedback=None):
        self.feedback_seen.append(feedback)
        return self.reply_plan


def _evidence() -> Evidence:
    return Evidence(
        post_id="9001",
        matched_topic="queues",
        why_it_fits="Synthetic post about retries.",
        confidence_score=0.8,
        confidence_inputs={},
        founder_materials=[],
    )


@pytest.mark.asyncio
async def test_plan_reply_activity_returns_valid_plan_without_errors(sample_post):
    target = "How do you folks handle eventual consistency?"
    agent = ScriptedPlanAgent(plan(target_point=target))
    activities = ReplyActivities(agent)

    result = await ActivityEnvironment().run(
        activities.plan_reply,
        PlanReplyInput(post=sample_post, evidence=_evidence(), idempotency_key="k"),
    )

    assert isinstance(result, PlanReplyResult)
    assert result.errors == []
    assert result.plan.target_point == target


@pytest.mark.asyncio
async def test_plan_reply_activity_validates_in_code_and_forwards_feedback(sample_post):
    agent = ScriptedPlanAgent(plan(target_point="text that is not in the post"))
    activities = ReplyActivities(agent)

    result = await ActivityEnvironment().run(
        activities.plan_reply,
        PlanReplyInput(
            post=sample_post,
            evidence=_evidence(),
            idempotency_key="k",
            feedback=["previous error"],
        ),
    )

    assert any("not an exact substring" in e for e in result.errors)
    assert agent.feedback_seen == [["previous error"]]


# ---------- skip rule needs_written_drafts (George, 2026-11-24) ----------

from backend.planning import (  # noqa: E402
    NEEDS_WRITTEN_DRAFTS_RULE,
    needs_written_drafts_skip_reason,
)


@pytest.mark.parametrize("reply_type,action", [
    ("answer_question", "answer the question"),
    ("share_experience", "share our experience"),
])
def test_needs_written_drafts_applies_without_product_mention(reply_type, action):
    reason = needs_written_drafts_skip_reason(plan(reply_type=reply_type, mention_product=False))
    assert reason.startswith("Reply needs written drafts")
    assert action in reason
    assert "ADR-0006 amendment 2026-11-24" in reason


@pytest.mark.parametrize("reply_type,mention", [
    ("answer_question", True),
    ("share_experience", True),
    ("add_observation", False),
    ("add_observation", True),
])
def test_needs_written_drafts_does_not_apply_otherwise(reply_type, mention):
    assert needs_written_drafts_skip_reason(plan(reply_type=reply_type, mention_product=mention)) is None


def test_needs_written_drafts_can_be_switched_off():
    p = plan(reply_type="answer_question", mention_product=False)
    assert needs_written_drafts_skip_reason(p, enabled=False) is None
    assert NEEDS_WRITTEN_DRAFTS_RULE == "needs_written_drafts"


@pytest.mark.asyncio
async def test_plan_reply_activity_reports_needs_written_drafts(sample_post):
    target = "How do you folks handle eventual consistency?"
    agent = ScriptedPlanAgent(plan(target_point=target, mention_product=False))
    result = await ActivityEnvironment().run(
        ReplyActivities(agent).plan_reply,
        PlanReplyInput(post=sample_post, evidence=_evidence(), idempotency_key="k"),
    )
    assert result.errors == []
    assert result.skip_rule == "needs_written_drafts"
    assert result.skip_reason.startswith("Reply needs written drafts")


@pytest.mark.asyncio
async def test_plan_reply_activity_does_not_apply_skip_rule_to_invalid_plan(sample_post):
    agent = ScriptedPlanAgent(plan(target_point="not in the post", mention_product=False))
    result = await ActivityEnvironment().run(
        ReplyActivities(agent).plan_reply,
        PlanReplyInput(post=sample_post, evidence=_evidence(), idempotency_key="k"),
    )
    assert result.errors
    assert result.skip_rule is None and result.skip_reason is None
