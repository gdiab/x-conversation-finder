"""The code side of the written-draft check (issue #12; ADR-0006 amendment
2026-11-24, steps 2 to 4).

The checker's verdicts are an input here, so these tests need no model: they
prove what code does with any answer a checker could give. Synthetic data only.
"""

import pytest

from backend.gate.written_check import (
    MAX_CHARS,
    TRIGGER_RULES_VERSION,
    judge,
    plan_product_failures,
    prepare_check,
)
from backend.gate.sentence_boundary import SPLITTER_VERSION
from backend.models.written import ClaimVerdict, DrafterHint
from backend.tests.written_helpers import brief

BRIEF = brief(
    "Relay can retry a failed delivery up to five times.",
    "Signed deliveries are available on the Team plan.",
)
NAME = "Acme Relay"
ALIASES = ["Relay"]
TERMS = ["Signed deliveries"]

REACTION = "That is a hard thing to get right."
CLAIM = "Relay can retry a failed delivery up to five times."
QUESTION = "What are you using today?"
GOOD = f"{REACTION} {CLAIM} {QUESTION}"

NOT_PRODUCT = dict(about_product=False)
SUPPORTED_B1 = dict(about_product=True, verdict="supported", brief_sentence_ids=["b1"])


def _prepare(text, hints=None):
    return prepare_check(text, BRIEF, NAME, ALIASES, TERMS, hints)


def _verdicts(*specs):
    return [ClaimVerdict(index=i, **spec) for i, spec in enumerate(specs, start=1) if spec is not None]


def _check(text, *specs, mention_product=None):
    return judge(_prepare(text), _verdicts(*specs), mention_product)


# ---------- the passing shape ----------


def test_reaction_one_supported_claim_and_a_question_pass():
    result = _check(GOOD, NOT_PRODUCT, SUPPORTED_B1, NOT_PRODUCT, mention_product=True)

    assert result.passed
    assert result.failures == [] and result.plan_failures == []
    assert [(c.kind, c.verdict, c.passed) for c in result.sentences] == [
        ("non_claim", "not_a_product_claim", True),
        ("claim", "supported", True),
        ("non_claim", "not_a_product_claim", True),
    ]
    claim = result.sentences[1]
    assert [(b.id, b.text) for b in claim.supporting] == [("b1", BRIEF[0].text)]
    assert {t.trigger for t in claim.triggers} == {"product_name", "number"}
    assert claim.reason == ""


def test_the_result_records_how_it_was_checked():
    info = _check(GOOD, NOT_PRODUCT, SUPPORTED_B1, NOT_PRODUCT).info

    assert info.product_name == NAME and info.product_aliases == ALIASES
    assert info.feature_terms == TERMS
    assert info.brief_sentence_count == 2
    assert info.splitter_version == SPLITTER_VERSION
    assert info.trigger_rules_version == TRIGGER_RULES_VERSION
    assert info.checker_called is True


def test_a_reply_with_no_product_sentence_passes():
    result = _check(f"{REACTION} {QUESTION}", NOT_PRODUCT, NOT_PRODUCT, mention_product=False)

    assert result.passed
    assert all(c.kind == "non_claim" for c in result.sentences)


# ---------- step 3: code checks the citations ----------


def test_supported_must_cite_a_brief_sentence():
    result = _check(GOOD, NOT_PRODUCT, dict(about_product=True, verdict="supported"), NOT_PRODUCT)

    assert not result.passed
    assert "cited no brief sentence" in result.sentences[1].reason
    assert result.sentences[1].verdict == "unsupported"


def test_a_cited_id_must_exist_and_is_not_echoed():
    answer = dict(about_product=True, verdict="supported", brief_sentence_ids=["b1", "b99-secret"])

    result = _check(GOOD, NOT_PRODUCT, answer, NOT_PRODUCT)

    assert not result.passed
    reason = result.sentences[1].reason
    assert "1 brief sentence id(s) that do not exist" in reason
    assert "b99-secret" not in reason  # model output is never echoed into stored reasons
    assert result.sentences[1].supporting == []


def test_unsupported_fails_with_the_checkers_reason_on_one_line():
    answer = dict(about_product=True, verdict="unsupported", reason="The brief says can,\n not will.")

    result = _check(GOOD, NOT_PRODUCT, answer, NOT_PRODUCT)

    assert not result.passed
    assert "The brief does not support it: The brief says can, not will." in result.sentences[1].reason
    assert result.failures == [[CLAIM, result.sentences[1].reason]]


def test_a_long_checker_reason_is_cut():
    answer = dict(about_product=True, verdict="unsupported", reason="x" * 1000)

    result = _check(GOOD, NOT_PRODUCT, answer, NOT_PRODUCT)

    assert len(result.sentences[1].reason) < 400


# ---------- step 2: triggers force the check ----------


@pytest.mark.parametrize("answer", [
    NOT_PRODUCT,  # the checker says it is not about the product
    dict(about_product=False, verdict=None),
    dict(about_product=None),  # malformed
    None,  # the checker skipped the sentence
])
def test_a_triggered_sentence_needs_a_supported_verdict_whatever_the_checker_says(answer):
    result = _check(GOOD, NOT_PRODUCT, answer, NOT_PRODUCT)

    assert not result.passed
    claim = result.sentences[1]
    assert claim.kind == "claim" and not claim.passed and claim.verdict == "unsupported"
    assert claim.reason.startswith("Checked as a product claim (names the product")


def test_nothing_can_take_a_triggered_sentence_out_of_the_checked_set():
    """Not the drafter's hints either: a hint is passed on, never trusted."""
    hints = [DrafterHint(sentence=CLAIM, brief_sentence_ids=["b1"])]
    prepared = _prepare(GOOD, hints)

    assert prepared.request().must_verify == [False, True, False]
    result = judge(prepared, _verdicts(NOT_PRODUCT, NOT_PRODUCT, NOT_PRODUCT))
    assert not result.passed


def test_the_checker_can_add_a_sentence_to_the_checked_set():
    text = f"{REACTION} It retries failed deliveries for you."

    flagged = _check(text, NOT_PRODUCT, dict(about_product=True, verdict="unsupported", reason="Not stated."))
    cleared = _check(text, NOT_PRODUCT, NOT_PRODUCT)

    assert not flagged.passed
    assert flagged.sentences[1].kind == "claim" and flagged.sentences[1].checker_flagged
    assert "the checker read it as a statement about the product" in flagged.sentences[1].reason
    assert cleared.passed  # no trigger, and the checker saw no product statement


@pytest.mark.parametrize("answer", [dict(about_product=None), dict(about_product="no"), None])
def test_an_unclear_answer_for_an_untriggered_sentence_fails_closed(answer):
    result = _check(f"{REACTION} {QUESTION}", NOT_PRODUCT, answer)

    assert not result.passed
    assert result.sentences[1].kind == "claim"


def test_a_repeated_answer_cannot_overrule_the_first():
    verdicts = [
        ClaimVerdict(index=2, about_product=True, verdict="unsupported", reason="No."),
        ClaimVerdict(index=2, **SUPPORTED_B1),
        ClaimVerdict(index=1, **NOT_PRODUCT),
        ClaimVerdict(index=3, **NOT_PRODUCT),
    ]

    assert not judge(_prepare(GOOD), verdicts).passed


# ---------- a product claim is a statement ----------


def test_a_triggered_question_fails_in_code_before_any_checker_call():
    prepared = _prepare("Did you know ours can retry a failed delivery up to five times?")

    assert not prepared.needs_checker
    result = judge(prepared, None)
    assert not result.passed
    assert "must be a statement, not a question" in result.failures[0][1]
    assert result.info.checker_called is False


def test_a_question_the_checker_flags_fails_even_when_supported():
    text = f"{REACTION} Did you know it resends failed deliveries?"

    result = _check(text, NOT_PRODUCT, SUPPORTED_B1)

    assert not result.passed
    assert "must be a statement, not a question" in result.sentences[1].reason


def test_a_question_about_the_poster_is_fine():
    assert _check(f"{REACTION} {QUESTION}", NOT_PRODUCT, NOT_PRODUCT).passed


# ---------- numbers in a supported claim ----------


def test_a_number_the_cited_sentences_do_not_state_fails():
    text = "Relay can retry a failed delivery up to ten times."

    result = _check(text, SUPPORTED_B1)

    assert not result.passed
    assert 'a number the cited brief sentences do not state: "ten"' in result.sentences[0].reason


def test_numbers_are_compared_as_digits_whatever_surrounds_them():
    sentences = brief("Latency stays under 10ms for 1,000 endpoints.")
    prepared = prepare_check("Relay keeps latency under 10 ms for 1000 endpoints.", sentences, NAME, ALIASES)

    assert judge(prepared, _verdicts(SUPPORTED_B1)).passed


def test_a_number_in_the_context_of_a_cited_list_item_counts():
    sentences = brief("Free tier")
    sentences[0].context = "Limits for 2 seats"
    prepared = prepare_check("Relay has a free tier for 2 seats.", sentences, NAME, ALIASES)

    assert judge(prepared, _verdicts(SUPPORTED_B1)).passed


# ---------- limits (unchanged from the 2026-11-21 amendment) ----------


def test_an_empty_text_fails_without_a_checker_call_and_names_a_failure():
    prepared = _prepare("   ")

    assert not prepared.needs_checker
    result = judge(prepared, None)
    assert not result.passed
    assert [(c.sentence, c.passed) for c in result.sentences] == [("[reply]", False)]
    assert "at least 1 sentence" in result.failures[0][1]


def test_four_sentences_fail_without_a_checker_call():
    prepared = _prepare(f"{REACTION} {REACTION} {REACTION} {QUESTION}")

    assert not prepared.needs_checker
    result = judge(prepared, None)
    assert "at most 3 sentences (got 4)" in result.failures[0][1]
    assert len(result.sentences) == 4 and not any(c.passed for c in result.sentences)


def test_more_than_280_characters_fail_without_a_checker_call():
    text = "That " + "really " * 45 + "matters."
    assert len(text) > MAX_CHARS

    prepared = _prepare(text)

    assert not prepared.needs_checker
    assert f"at most {MAX_CHARS} characters" in prepared.structural_failures[0][1]


def test_two_triggered_sentences_fail_without_a_checker_call():
    prepared = _prepare(f"{CLAIM} Our dashboard shows it.")

    assert not prepared.needs_checker
    reason = prepared.structural_failures[0][1]
    assert "at most 1 product claim" in reason and "2 sentences are checked as product claims" in reason


def test_two_claims_found_by_the_checker_fail_on_both_sentences():
    text = f"{CLAIM} It also resends old deliveries."

    result = _check(text, SUPPORTED_B1, SUPPORTED_B1)

    assert not result.passed
    assert [c.passed for c in result.sentences] == [False, False]
    assert all("at most 1 product claim" in c.reason for c in result.sentences)


def test_a_failed_check_always_names_a_failing_sentence():
    """The approve validator lists failing sentences; none must be empty."""
    for result in [
        judge(_prepare(""), None),
        judge(_prepare(f"{REACTION} {REACTION} {REACTION} {QUESTION}"), None),
        _check(GOOD, NOT_PRODUCT, NOT_PRODUCT, NOT_PRODUCT),
        _check(f"{CLAIM} It also resends old deliveries.", SUPPORTED_B1, SUPPORTED_B1),
    ]:
        assert not result.passed
        assert any(not c.passed for c in result.sentences)


# ---------- the reply plan's product decision (issue #5) ----------


def test_plan_says_no_product_but_the_draft_has_a_claim():
    result = _check(GOOD, NOT_PRODUCT, SUPPORTED_B1, NOT_PRODUCT, mention_product=False)

    assert not result.passed
    assert result.failures == []  # the claim itself is supported
    assert result.plan_failures == [
        "the reply plan says not to mention the product, but the draft has a product claim"
    ]


def test_plan_says_mention_the_product_but_the_draft_has_no_claim():
    result = _check(f"{REACTION} {QUESTION}", NOT_PRODUCT, NOT_PRODUCT, mention_product=True)

    assert not result.passed
    assert result.plan_failures == [
        "the reply plan says to mention the product, but the draft has no product claim"
    ]


def test_a_founder_edit_is_not_held_to_the_plan():
    assert _check(f"{REACTION} {QUESTION}", NOT_PRODUCT, NOT_PRODUCT, mention_product=None).passed
    assert plan_product_failures([], None) == []


# ---------- the request the checker gets ----------


def test_the_request_carries_only_sentences_brief_names_flags_and_hints():
    hints = [
        DrafterHint(sentence=CLAIM, brief_sentence_ids=["b1", "b77"]),  # b77 does not exist
        DrafterHint(sentence="A sentence that is not in the reply.", brief_sentence_ids=["b2"]),
    ]

    request = _prepare(GOOD, hints).request()

    assert request.sentences == [REACTION, CLAIM, QUESTION]
    assert [b.id for b in request.brief_sentences] == ["b1", "b2"]
    assert (request.product_name, request.product_aliases) == (NAME, ALIASES)
    assert request.must_verify == [False, True, False]
    assert request.suggested_ids == [[], ["b1"], []]  # existing ids only, matched by sentence
    assert set(vars(request)) == {
        "sentences", "brief_sentences", "product_name", "product_aliases", "must_verify", "suggested_ids",
    }
