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
from backend.models.written import ClaimVerdict
from backend.tests.written_helpers import brief

BRIEF = brief(
    "Relay can retry a failed delivery up to five times.",
    "Signed deliveries are available on the Team plan.",
    "The replay tool keeps deliveries for 30 days.",
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


def _prepare(text, mention_product=None):
    return prepare_check(text, BRIEF, NAME, ALIASES, TERMS, mention_product)


def _verdicts(*specs):
    return [ClaimVerdict(index=i, **spec) for i, spec in enumerate(specs, start=1) if spec is not None]


def _check(text, *specs, mention_product=None):
    """Judge with the given checker answers. Fails the test if code would not
    have asked the checker at all (use _prepare + judge(prepared, None) then)."""
    prepared = _prepare(text, mention_product)
    assert prepared.needs_checker, (prepared.structural_failures, prepared.plan_failures)
    return judge(prepared, _verdicts(*specs), mention_product)


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
    assert info.brief_sentence_count == 3
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
    prepared = _prepare(GOOD)

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


# ---------- a malformed answer is not repaired ----------


def test_a_citation_list_with_a_non_string_fails_even_if_one_id_is_real():
    """ ["b1", 7] is not read as ["b1"]: a malformed citation list is no citation."""
    verdict = ClaimVerdict(index=2, about_product=True, verdict="supported", brief_sentence_ids=["b1", 7])

    result = judge(_prepare(GOOD), [ClaimVerdict(1, False), verdict, ClaimVerdict(3, False)])

    assert not result.passed
    assert "The checker's answer for it was malformed." in result.sentences[1].reason


def test_a_verdict_marked_malformed_fails_a_triggered_and_an_untriggered_sentence():
    marked = dict(about_product=False, malformed=True)

    triggered = _check(GOOD, NOT_PRODUCT, dict(SUPPORTED_B1, malformed=True), NOT_PRODUCT)
    untriggered = _check(f"{REACTION} {QUESTION}", NOT_PRODUCT, marked)

    assert not triggered.passed and "malformed" in triggered.sentences[1].reason
    # Even an explicit "not about the product" does not count in a malformed answer
    assert not untriggered.passed and untriggered.sentences[1].kind == "claim"


def test_an_answer_without_a_true_or_false_classification_fails_even_if_it_says_supported():
    """Every object must say whether the sentence is about the product. An
    answer that skips that is not read charitably."""
    unclassified = ClaimVerdict(index=2, verdict="supported", brief_sentence_ids=["b1"])  # about_product missing

    result = judge(_prepare(GOOD), [ClaimVerdict(1, False), unclassified, ClaimVerdict(3, False)])

    assert not result.passed
    assert "The checker's answer for it was malformed." in result.sentences[1].reason


@pytest.mark.parametrize("answer", [
    dict(about_product=False, verdict="unsupported", reason="Not in the brief."),
    dict(about_product=False, verdict="supported", brief_sentence_ids=["b1"]),
    dict(about_product=False, brief_sentence_ids=["b1"]),
])
def test_an_answer_that_contradicts_itself_fails_an_untriggered_sentence(answer):
    """ "Not about the product", yet with a verdict or a citation: only a clean
    "not about the product" keeps a sentence out of the checked set."""
    text = f"{REACTION} It retries failed deliveries for you."

    result = _check(text, NOT_PRODUCT, answer)

    assert not result.passed
    check = result.sentences[1]
    assert (check.kind, check.passed, check.verdict) == ("claim", False, "unsupported")
    assert "did not clearly say that it is not about the product" in check.reason


def test_only_two_answers_pass_a_sentence():
    """A clean "not about the product" for an untriggered sentence, and a clean
    "supported" with existing citations. Every other shape fails."""
    untriggered = f"{REACTION} It retries failed deliveries for you."
    shapes = [
        None,
        dict(),
        dict(about_product=None),
        dict(about_product=True),
        dict(about_product=True, verdict="unsupported"),
        dict(about_product=True, verdict="supported"),
        dict(about_product=True, verdict="supported", brief_sentence_ids=["b9"]),
        dict(about_product=True, verdict="supported", brief_sentence_ids=["b1"], malformed=True),
        dict(about_product=False, verdict="unsupported"),
        dict(about_product=False, verdict="supported", brief_sentence_ids=["b1"]),
        dict(about_product=False, malformed=True),
        dict(verdict="supported", brief_sentence_ids=["b1"]),
    ]

    for shape in shapes:
        assert not _check(untriggered, NOT_PRODUCT, shape).passed, shape
    assert _check(untriggered, NOT_PRODUCT, NOT_PRODUCT).passed
    assert _check(untriggered, NOT_PRODUCT, SUPPORTED_B1).passed


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
    assert "A product claim must be a statement, not a question." in result.failures[0][1]
    assert result.failures[0][1].startswith('Checked as a product claim (uses we/our/us/ours ("ours")')
    assert result.info.checker_called is False


def test_a_question_the_checker_flags_fails_even_when_supported():
    text = f"{REACTION} Did you know it resends failed deliveries?"

    result = _check(text, NOT_PRODUCT, SUPPORTED_B1)

    assert not result.passed
    assert "must be a statement, not a question" in result.sentences[1].reason


def test_a_question_in_another_script_that_the_checker_flags_fails_too():
    text = f"{REACTION} Did you know it resends failed deliveries" + chr(0x061F)

    result = _check(text, NOT_PRODUCT, SUPPORTED_B1)

    assert not result.passed
    assert "must be a statement, not a question" in result.sentences[1].reason


def test_a_question_about_the_poster_is_fine():
    assert _check(f"{REACTION} {QUESTION}", NOT_PRODUCT, NOT_PRODUCT).passed


# ---------- numbers in a supported claim ----------


def test_a_number_the_cited_sentences_do_not_state_fails():
    """ "30" is in the brief (b3), but not in the sentence the checker cites."""
    text = "Relay can retry a failed delivery up to 30 times."

    result = _check(text, SUPPORTED_B1)

    assert not result.passed
    assert 'a number the cited brief sentences do not state: "30"' in result.sentences[0].reason
    assert _check(text, dict(about_product=True, verdict="supported", brief_sentence_ids=["b1", "b3"])).passed


def test_a_number_no_brief_sentence_states_fails_before_any_checker_call():
    """Whatever the checker cited, the number rule would reject it: do not pay to ask."""
    prepared = _prepare("Relay can retry a failed delivery up to ten times.")

    assert not prepared.needs_checker
    [[sentence, reason]] = prepared.structural_failures
    assert sentence == "Relay can retry a failed delivery up to ten times."
    assert 'It contains a number no brief sentence states: "ten"' in reason


def test_one_is_a_number_in_a_claim_too():
    prepared = _prepare("Relay can retry a failed delivery one time.")

    assert not prepared.needs_checker
    assert 'a number no brief sentence states: "one"' in prepared.structural_failures[0][1]


def test_a_product_named_with_a_number_word_still_has_its_numbers_checked():
    sentences = brief("One retries failed deliveries.", "One keeps five copies.")

    quantity = prepare_check("One retries two deliveries.", sentences, "One")
    named_only = prepare_check("One retries failed deliveries.", sentences, "One")

    # "two" is in no brief sentence: rejected before the checker call
    assert not quantity.needs_checker
    assert 'a number no brief sentence states: "two"' in quantity.structural_failures[0][1]
    # The name alone is fine: the brief sentences state "One" too
    assert named_only.needs_checker
    assert judge(named_only, _verdicts(SUPPORTED_B1)).passed


def test_a_numeric_alias_does_not_make_a_digit_in_the_name_a_number():
    sentences = brief("Relay retries failed deliveries.")

    prepared = prepare_check("Relay2 retries failed deliveries.", sentences, "Relay2", ["One"])

    assert prepared.needs_checker and prepared.structural_failures == []
    assert judge(prepared, _verdicts(SUPPORTED_B1)).passed


def test_a_triggered_sentence_cannot_be_supported_by_an_empty_brief():
    prepared = prepare_check(CLAIM, [], NAME, ALIASES)

    assert not prepared.needs_checker
    assert "The brief has no sentences, so nothing can support it." in prepared.structural_failures[0][1]


def test_an_empty_brief_and_a_plan_to_mention_the_product_fail_before_any_checker_call():
    """Every checker answer would fail: a claim cannot cite an empty brief,
    and a reply without a claim does not follow the plan."""
    text = f"{REACTION} {QUESTION}"

    with_plan = prepare_check(text, [], NAME, ALIASES, mention_product=True)
    without_plan = prepare_check(text, [], NAME, ALIASES, mention_product=False)

    assert not with_plan.needs_checker
    assert "the brief has no sentences to support a product claim" in with_plan.structural_failures[0][1]
    # With no product mention required the reply can still pass, so the checker is asked
    assert without_plan.needs_checker


def test_numbers_are_compared_as_digits_whatever_surrounds_them():
    sentences = brief("Latency stays under 10ms for 1,000 endpoints.")
    prepared = prepare_check("Relay keeps latency under 10 ms for 1000 endpoints.", sentences, NAME, ALIASES)

    assert prepared.needs_checker
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


# ---------- the limits cannot be dodged by how sentences are joined ----------


@pytest.mark.parametrize("text", [
    "Acme ships.It retries.",  # no space after the period
    "That is hard.Really hard.What are you using?",
    "Acme works.it retries.it ships.it scales.",  # lowercase joins: four sentences, four claims
    "That is hard,right?yes it is.",  # a question mark between letters
    "Acme works!" + chr(0x1F642) + "It scales!" + chr(0x1F642) + "It ships!" + chr(0x1F642) + "Done",  # emoji between
    'He said "it works."It shipped.',  # a closing quote between
    "Plan B.It works.",
    "Hmm...that depends on the queue.",
    "Well... that depends on the queue.",  # an ellipsis may end a sentence
])
def test_a_sentence_that_may_be_two_fails_before_any_checker_call(text):
    prepared = prepare_check(text, BRIEF, "Acme", [], [])

    assert not prepared.needs_checker
    assert any("This may be more than one sentence" in reason for _, reason in prepared.structural_failures)


@pytest.mark.parametrize("text", [
    "Node.js handles that well enough.",
    "ASP.NET does the same.",
    "The docs at docs.example.com cover that.",
    "Put it in config.yaml and restart.",
    "That is roughly 5 of them, e.g. the retry queue.",
    "It shipped in the U.S.A before anywhere else.",
    "A U.S.-based team hit the same wall.",
    "Version 2.0 fixed that.",
    "The fix is in main.py:12 if you want it.",
    "That was a great week!" + chr(0x1F680),  # an emoji after the end is not a join
    "(It works.)",
])
def test_ordinary_dots_inside_a_sentence_are_fine(text):
    prepared = prepare_check(text, brief("We count 5 of them in version 2.0."), "Acme", [], [])

    assert not any("more than one sentence" in r for _, r in prepared.structural_failures)


def test_fullwidth_and_ideographic_terminators_end_a_sentence():
    text = "Acme ships" + chr(0x3002) + "It retries" + chr(0xFF01) + "Does it" + chr(0xFF1F) + "Yes" + chr(0xFF0E) + "Done"

    prepared = prepare_check(text, BRIEF, "Acme", [], [])

    assert len(prepared.sentences) == 5
    assert "at most 3 sentences (got 5)" in prepared.structural_failures[0][1]


def test_other_terminators_end_a_sentence_with_a_space_after_them_too():
    stop = chr(0x3002)  # ideographic full stop
    spaced = prepare_check(f"Acme ships{stop} Acme retries{stop} Acme scales{stop} Acme works{stop}", BRIEF, "Acme", [], [])
    urdu = prepare_check("That is hard" + chr(0x06D4) + " Really hard" + chr(0x061F) + " Yes", BRIEF, "Acme", [], [])

    assert len(spaced.sentences) == 4
    assert any("at most 3 sentences (got 4)" in reason for _, reason in spaced.structural_failures)
    assert any("at most 1 product claim" in reason for _, reason in spaced.structural_failures)
    assert len(urdu.sentences) == 3


@pytest.mark.parametrize("mark", [
    "?", chr(0xFF1F), chr(0x2047), chr(0x2048),  # ASCII, fullwidth, doubled
    chr(0x061F), chr(0x037E), chr(0x055E), chr(0x1367),  # Arabic, Greek, Armenian, Ethiopic
])
def test_a_question_mark_of_any_width_makes_a_triggered_sentence_a_question(mark):
    prepared = _prepare(f"Did you know Relay can retry a failed delivery up to five times{mark}")

    assert not prepared.needs_checker
    assert "must be a statement, not a question" in prepared.structural_failures[0][1]


def test_a_line_break_ends_a_sentence():
    """Four lines are four sentences, with or without full stops."""
    prepared = _prepare("That is hard\nReally hard\nVery hard\nWhat are you using")

    assert len(prepared.sentences) == 4
    assert "at most 3 sentences (got 4)" in prepared.structural_failures[0][1]
    # And two product lines are two claims
    two = _prepare("Relay retries deliveries\nOur dashboard shows them")
    assert "at most 1 product claim" in two.structural_failures[0][1]


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


def test_plan_says_no_product_and_a_triggered_sentence_costs_no_checker_call():
    """Code already knows the sentence is a product claim, so the plan is broken
    before the checker is asked (issue #8: do not pay for a known failure)."""
    prepared = _prepare(GOOD, mention_product=False)

    assert not prepared.needs_checker
    assert prepared.structural_failures == []
    result = judge(prepared, None, False)
    assert not result.passed
    assert result.failures == []  # nothing is wrong with the text itself
    assert result.plan_failures == [
        "the reply plan says not to mention the product, but the draft has a product claim"
    ]
    assert result.info.checker_called is False
    assert all(not c.passed and "does not follow the reply plan" in c.reason for c in result.sentences)


def test_plan_says_no_product_but_the_checker_finds_a_claim():
    """No trigger fires, so only the checker can tell."""
    text = f"{REACTION} It resends failed deliveries for you."

    result = _check(text, NOT_PRODUCT, SUPPORTED_B1, mention_product=False)

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


def test_the_request_carries_only_sentences_brief_names_and_the_must_verify_flags():
    request = _prepare(GOOD, mention_product=True).request()

    assert request.sentences == [REACTION, CLAIM, QUESTION]
    assert [b.id for b in request.brief_sentences] == ["b1", "b2", "b3"]
    assert (request.product_name, request.product_aliases) == (NAME, ALIASES)
    assert request.must_verify == [False, True, False]
    # No post, no plan (not even its product decision), no drafter hints
    assert set(vars(request)) == {
        "sentences", "brief_sentences", "product_name", "product_aliases", "must_verify",
    }
