"""Switch-criteria tests for written drafts (issue #12; ADR-0006 amendment
2026-11-24, "Switch criteria").

The fixture (fixtures/written_switch_criteria.json) lists every must-REJECT
case of the 2026-11-21 amendment's edit-path list and the 2026-11-24
adversarial paraphrase set. For each case it records the answer a correct
checker must give, and a deterministic fake checker returns exactly that. So
these tests prove the code side of steps 2 to 4 without a model:

- which sentences the triggers force into the checked set;
- that a forced sentence is rejected unless the checker returns "supported"
  with brief sentence ids that exist;
- which cases code rejects with no checker call at all.

What they cannot prove is that a real checker model returns the correct
answer. test_what_code_alone_rejects... pins which cases would slip through a
checker that wrongly calls everything supported; those rest on the model and
are measured only by a live run (the blind comparison, criterion 3).

Synthetic data only.
"""

import dataclasses
import json
import re
from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment

from backend.activities.written_check_activities import CheckWrittenDraftInput, WrittenCheckActivities
from backend.gate.brief import brief_feature_terms, split_brief
from backend.gate.written_check import judge, prepare_check
from backend.models.written import ClaimVerdict
from backend.tests.written_helpers import (
    ScriptedChecker,
    no_answer,
    not_about_product,
    supported_citing,
    supported_without_citation,
    verdict_from,
)

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = json.loads((FIXTURES / "written_switch_criteria.json").read_text())
CASES = FIXTURE["cases"]
ADR = Path(__file__).resolve().parents[2] / "docs" / "adr" / "0006-evidence-claim-gate-and-baseline-eval.md"

E21 = "2026-11-21 edit-path must-REJECT"
A24 = "2026-11-24 adversarial set"

# The classes the 2026-11-24 amendment's second criterion names
ADVERSARIAL_KINDS = {
    "adds a qualifier",
    "drops a qualifier",
    "changes a modal",
    "swaps a comparison",
    "attributes a claim to a different feature",
    "hides a claim in a question",
}

# 2026-11-21 edit-path cases that steps 2 to 4 do NOT reject, because they
# make no product claim (step 4: such sentences are not gated against the
# brief; founder review is the backstop). The amendment's first criterion
# says "every must-REJECT case ... is rejected by steps 2-4", so as worded it
# cannot hold for these five. Whether to reword the criterion or add a rule is
# George's decision (tasks/report-12.md); this list pins the current behavior
# so the gap cannot grow unnoticed.
NOT_PRODUCT_CLAIMS_UNDER_STEP_4 = {
    "What stack are you on.",
    "What stack are you on!",
    "I like the idea.",
    "I think you are the problem.",
    "Why is the issue the same?",
}

# Must-REJECT cases code still rejects when the checker wrongly calls every
# sentence it had to verify "supported" (citing real brief sentences). Every
# other must-REJECT case rests on the checker's judgement of entailment.
REJECTED_BY_CODE_WHATEVER_THE_CHECKER_SAYS = {
    "e21-passage-sentence-as-question",  # a triggered question
    "e21-is-ours-10x-faster-than-kafka",  # a triggered question
    "e21-ours-is-10x-quicker",  # a number no brief sentence states
    "e21-twenty-integrations",  # a number no brief sentence states
    "a24-claim-in-question-ours",  # a triggered question
    "a24-changed-number",  # a number no brief sentence states
}


def _by(source):
    return [c for c in CASES if c["source"] == source]


def _ids(cases):
    return [c["id"] for c in cases]


def _brief(case):
    return FIXTURE["briefs"][case["brief"]]


def _prepared(case):
    b = _brief(case)
    return prepare_check(
        case["text"],
        split_brief(b["text"]),
        b["product_name"],
        b["product_aliases"],
        brief_feature_terms(b["text"]),
    )


def _correct_verdicts(case):
    return [verdict_from(n, spec) for n, spec in enumerate(case["correct_checker"], start=1)]


def _check(case, answer=None):
    """Check a case; `answer` replaces the correct checker (a lenient fake)."""
    prepared = _prepared(case)
    if not prepared.needs_checker:
        return judge(prepared, None)
    verdicts = answer(prepared.request()) if answer else _correct_verdicts(case)
    return judge(prepared, verdicts)


def rubber_stamp(request):
    """A wrong checker: whatever it must verify is "supported", citing every
    brief sentence; nothing else is about the product."""
    every_id = [b.id for b in request.brief_sentences]
    return [
        ClaimVerdict(index=i, about_product=True, verdict="supported", brief_sentence_ids=every_id)
        if must else ClaimVerdict(index=i, about_product=False)
        for i, must in enumerate(request.must_verify, start=1)
    ]


# ---------- the fixture covers the criteria ----------


def _adr_edit_path_must_reject():
    """The quoted cases of the 2026-11-21 amendment's edit-path must-REJECT list."""
    text = ADR.read_text()
    block = text[text.index("**Regression tests for edit path (must REJECT"):text.index("**Regression tests (must PASS):**")]
    cases = []
    for line in block.splitlines():
        m = re.match(r"^- '(.*)' → REJECTED", line)
        if m:
            cases.extend(m.group(1).split("' / '"))
    return cases


def test_fixture_has_every_edit_path_must_reject_case_of_the_2026_11_21_amendment():
    from_adr = _adr_edit_path_must_reject()

    assert len(from_adr) == 17
    assert sorted(c["text"] for c in _by(E21)) == sorted(from_adr)


def test_fixture_has_every_class_of_the_2026_11_24_adversarial_set():
    kinds = {c["kind"] for c in _by(A24)}

    assert ADVERSARIAL_KINDS <= kinds
    for kind in ADVERSARIAL_KINDS:
        assert any(c["kind"] == kind for c in _by(A24))
    # Each adversarial case paraphrases a sentence its brief really has
    for case in _by(A24):
        brief_ids = {s.id for s in split_brief(_brief(case)["text"])}
        assert case["paraphrases"] in brief_ids, case["id"]


def test_fixture_briefs_are_numbered_as_the_cases_assume():
    relay = split_brief(FIXTURE["briefs"]["relay"]["text"])

    assert [s.id for s in relay] == [f"b{i}" for i in range(1, 9)]
    assert relay[1].text == "Relay can retry a failed delivery up to five times."
    assert relay[5].text == "Our dashboard shows delivery latency per endpoint."
    assert brief_feature_terms(FIXTURE["briefs"]["relay"]["text"]) == ["Signed deliveries", "Delivery replay"]


# ---------- step 2: the triggers, with no model involved ----------


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_triggers_fire_as_listed(case):
    prepared = _prepared(case)

    assert [sorted({h.trigger for h in hits}) for hits in prepared.triggers] == case["triggers"]


# ---------- the outcome with the answer a correct checker must give ----------


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_outcome_with_the_correct_checker(case):
    result = _check(case)

    assert ("pass" if result.passed else "reject") == case["expect"], result.reasoning
    if case["decided_by"] == "code":
        # Rejected in code: the checker is never called, so no answer is listed
        assert case["correct_checker"] is None
        assert not _prepared(case).needs_checker and result.info.checker_called is False
    else:
        assert len(case["correct_checker"]) == len(_prepared(case).sentences)


def test_criterion_2_every_adversarial_paraphrase_is_rejected():
    for case in _by(A24):
        assert case["expect"] == "reject"
        assert not _check(case).passed, case["id"]


def test_criterion_1_every_2026_11_21_case_that_makes_a_product_claim_is_rejected():
    for case in _by(E21):
        if case["text"] in NOT_PRODUCT_CLAIMS_UNDER_STEP_4:
            continue
        assert case["expect"] == "reject"
        assert not _check(case).passed, case["id"]


def test_criterion_1_gap_cases_that_are_not_product_claims_are_not_rejected():
    """See NOT_PRODUCT_CLAIMS_UNDER_STEP_4: the criterion as worded does not hold for these."""
    not_rejected = {c["text"] for c in _by(E21) if c["expect"] == "pass"}

    assert not_rejected == NOT_PRODUCT_CLAIMS_UNDER_STEP_4
    for case in _by(E21):
        if case["text"] in NOT_PRODUCT_CLAIMS_UNDER_STEP_4:
            assert case["decided_by"] == "step-4"
            assert case["triggers"] == [[]]  # no trigger fires on any of them
            assert _check(case).passed


def test_replies_that_should_pass_do():
    for case in _by("must-PASS"):
        result = _check(case)
        assert result.passed, (case["id"], result.reasoning)
        assert sum(1 for c in result.sentences if c.kind == "claim") <= 1


# ---------- code-side guarantees, whatever the checker answers ----------

TRIGGER_CASES = [c for c in CASES if c["decided_by"] == "trigger"]
CODE_CASES = [c for c in CASES if c["decided_by"] == "code"]
CHECKER_REJECTS = [c for c in CASES if c["decided_by"] == "checker" and c["expect"] == "reject"]

LENIENT_ANSWERS = {
    "says it is not about the product": not_about_product,
    "says supported without a citation": supported_without_citation,
    "says supported citing an id that does not exist": supported_citing("b999"),
    "returns nothing usable": no_answer,
}


@pytest.mark.parametrize("answer", list(LENIENT_ANSWERS.values()), ids=list(LENIENT_ANSWERS))
@pytest.mark.parametrize("case", TRIGGER_CASES, ids=_ids(TRIGGER_CASES))
def test_a_triggered_case_is_rejected_unless_the_checker_cites_real_support(case, answer):
    """Triggers force the check; the cited ids must exist; supported must cite one."""
    result = _check(case, answer)

    assert not result.passed
    failing = [c for c in result.sentences if not c.passed]
    assert failing and all(c.kind == "claim" for c in failing)


@pytest.mark.parametrize("case", CODE_CASES, ids=_ids(CODE_CASES))
def test_a_code_rejected_case_never_reaches_the_checker(case):
    prepared = _prepared(case)

    assert not prepared.needs_checker
    # And code would reject it even with a rubber-stamp answer in hand
    assert not judge(prepared, rubber_stamp(prepared.request())).passed


@pytest.mark.parametrize("case", CHECKER_REJECTS, ids=_ids(CHECKER_REJECTS))
def test_a_case_with_no_trigger_rests_on_the_checkers_classification(case):
    """No trigger fires, so only the checker can put it in the checked set.
    With a checker that wrongly waves it through, it passes: this is the part
    a live model run has to validate."""
    assert all(hits == [] for hits in _prepared(case).triggers)
    assert not _check(case).passed
    assert _check(case, not_about_product).passed


def test_what_code_alone_rejects_when_a_wrong_checker_calls_everything_supported():
    must_reject = [c for c in CASES if c["expect"] == "reject"]

    rejected = {c["id"] for c in must_reject if not _check(c, rubber_stamp).passed}

    assert rejected == REJECTED_BY_CODE_WHATEVER_THE_CHECKER_SAYS
    # The rest need a checker that judges entailment correctly: 18 of 24
    assert (len(must_reject), len(must_reject) - len(rejected)) == (24, 18)


# ---------- the same cases through the activity ----------


def _env():
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info, workflow_id="review:synthetic-switch", workflow_run_id="run-1", activity_id="1"
    )
    return env


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
async def test_check_written_draft_activity_gives_the_same_outcome(case):
    b = _brief(case)
    sentences = _prepared(case).sentences
    checker = ScriptedChecker(
        by_sentence=dict(zip(sentences, case["correct_checker"] or [])),
        default={},  # an unscripted sentence gets no answer, which fails closed
    )
    activities = WrittenCheckActivities(checker)

    result = await _env().run(
        activities.check_written_draft,
        CheckWrittenDraftInput(
            text=case["text"],
            product_brief=b["text"],
            product_name=b["product_name"],
            product_aliases=b["product_aliases"],
            idempotency_key=case["id"],
        ),
    )

    assert ("pass" if result.passed else "reject") == case["expect"], result.reasoning
    if case["decided_by"] == "code":
        assert checker.requests == []  # no paid call for a text code already rejected
    else:
        [request] = checker.requests
        assert request.sentences == sentences
        assert request.product_name == b["product_name"]
