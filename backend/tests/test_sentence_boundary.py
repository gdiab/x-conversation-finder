"""
Sentence splitting after abbreviations (issue #10, ADR-0006 claim gate contract).

The claim gate's splitter (founder passages, edited drafts) and the reply
plan's question-span splitter (post text) share one rule for periods:
backend.gate.sentence_boundary. All text here is synthetic.
"""

import time
from pathlib import Path

import pytest

from backend.activities.claim_gate_activity import (
    UNCLEAR_BOUNDARY_REASON,
    ClaimGateInput,
    EditedTextGateInput,
    claim_gate,
    claim_selectability_error,
    ends_with_abbreviation,
    insert_claim_sentence,
    split_into_sentences,
    verify_edited_text,
)
from backend.adapters.plain_reply_agent import format_materials_for_prompt
from backend.gate.legacy_split_v1 import split_into_sentences_v1
from backend.gate.non_claim_set import NON_CLAIM_PHRASES
from backend.gate.sentence_boundary import (
    CONTINUE,
    END,
    LIKELY_CONTINUE,
    LIKELY_END,
    SPLITTER_VERSION,
    abbreviation_kind,
    period_boundary,
    sentence_has_unclear_boundary,
    starts_new_sentence,
)
from backend.models import FounderMaterial, ReplyPlan, Segment
from backend.planning import question_spans, validate_reply_plan


def material(passage: str, passage_id: str = "passage_0") -> FounderMaterial:
    return FounderMaterial(source="brief.md", passage=passage, relevance="relevant", passage_id=passage_id)


def claim(index: int, version=SPLITTER_VERSION) -> Segment:
    return Segment(kind="claim", source_passage_id="passage_0", sentence_index=index, splitter_version=version)


# ---------- golden splits ----------
#
# Sentence indices are positions in these splits, and claim segments store
# them. A change to any expected split here changes the indices that drafts
# reference: bump SPLITTER_VERSION and read ADR-0006 (2026-11-21 amendment,
# item 2) before updating one. The third value lists the sentences that
# start, end or contain an unclear boundary, which are not selectable.

GOLDEN_SPLITS = [
    # Initialisms inside a sentence (the issue's example).
    ("Is the U.S.A. market ready?", ["Is the U.S.A. market ready?"], []),
    ("We sell in the U.S. market today.", ["We sell in the U.S. market today."], []),
    ("Our U.K. team ships on Fridays.", ["Our U.K. team ships on Fridays."], []),
    ("Standups start at 9 a.m. sharp.", ["Standups start at 9 a.m. sharp."], []),
    ("We operate in the U.S.A.", ["We operate in the U.S.A."], []),
    # Known limit: a lowercase word after an initialism continues the
    # sentence (the issue's rule), even when it starts a new one.
    ("We ship in the U.S.A. customers pay monthly.", ["We ship in the U.S.A. customers pay monthly."], []),
    ("Is it live in the U.S.? Yes.", ["Is it live in the U.S.?", "Yes."], []),
    # An initialism before a capitalized word is unclear: split before a
    # sentence starter, joined otherwise; either way not selectable.
    ("We sell in the U.S. We ship weekly.", ["We sell in the U.S.", "We ship weekly."], [0, 1]),
    ("We ship to the U.S. The Netherlands, and France.", ["We ship to the U.S.", "The Netherlands, and France."], [0, 1]),
    ("We ship in the U.S. Customers pay monthly.", ["We ship in the U.S. Customers pay monthly."], [0]),
    ("We ship in the U.S.” Customers pay monthly.", ["We ship in the U.S.” Customers pay monthly."], [0]),
    # Prefix abbreviations (titles, Latin) never end a sentence.
    ("Ask Dr. Smith about it.", ["Ask Dr. Smith about it."], []),
    ("We met at Mt. Everest base camp.", ["We met at Mt. Everest base camp."], []),
    ("Use a queue, e.g. Postgres or Redis.", ["Use a queue, e.g. Postgres or Redis."], []),
    ("Pick one store, i.e. Postgres.", ["Pick one store, i.e. Postgres."], []),
    ("Foo vs. It is shipped.", ["Foo vs. It is shipped."], [0]),
    ("We use e.g. We ship weekly.", ["We use e.g. We ship weekly."], [0]),
    # etc., St., Jr. and months can end a sentence.
    ("We support Slack, Teams, etc. and more.", ["We support Slack, Teams, etc. and more."], []),
    ("We support Slack, Teams, etc. We add more monthly.", ["We support Slack, Teams, etc.", "We add more monthly."], [0, 1]),
    ("We support Slack, Teams, etc. Pricing is flat.", ["We support Slack, Teams, etc. Pricing is flat."], [0]),
    ("Our office is on Main St. We host meetups.", ["Our office is on Main St.", "We host meetups."], [0, 1]),
    ("We launched in Jan. and grew fast.", ["We launched in Jan. and grew fast."], []),
    # Single-letter initials, either case.
    ("Built by J. Smith last year.", ["Built by J. Smith last year."], [0]),
    ("Built by j. smith in 2021.", ["Built by j. smith in 2021."], []),
    # Codex thermo r5: initials are any Unicode letter.
    ("Our founder is \u00c9. Dubois, who built the product.", ["Our founder is \u00c9. Dubois, who built the product."], [0]),
    ("We serve the \u00c9.U. market.", ["We serve the \u00c9.U. market."], []),
    ("Built by J. The artist Smith in 2021.", ["Built by J. The artist Smith in 2021."], [0]),
    ("We chose Plan B. Customers need help.", ["We chose Plan B. Customers need help."], [0]),
    ("Pick option B. or C.", ["Pick option B. or C."], []),
    # Ph.D. (QA R11 test).
    ("Founded by Jo Lee, Ph.D. The company is small.", ["Founded by Jo Lee, Ph.D.", "The company is small."], [0, 1]),
    # Numbers (S-5 decimals): a number before a capitalized word ends the sentence.
    (
        "Our product has 99.99% uptime. It handles 1.5 million requests per second. Great performance.",
        ["Our product has 99.99% uptime.", "It handles 1.5 million requests per second.", "Great performance."],
        [],
    ),
    ("Uptime was 99.9. We fixed that.", ["Uptime was 99.9.", "We fixed that."], []),
    ("We launched in 2021. We now serve teams.", ["We launched in 2021.", "We now serve teams."], []),
    ("We launched in 2021. Customers followed.", ["We launched in 2021.", "Customers followed."], []),
    # Codex thermo r4: a number before a lowercase word ends the sentence.
    ("We launched in 2021. customers pay monthly.", ["We launched in 2021.", "customers pay monthly."], []),
    # List markers stay with their item; a mid-sentence one is unclear.
    ("1. install the CLI first.", ["1. install the CLI first."], []),
    ("1. The product supports SSO.", ["1. The product supports SSO."], [0]),
    ("1. The product supports SSO. 2. The API is fast.", ["1. The product supports SSO.", "2. The API is fast."], [0, 1]),
    ("Steps: 1. Install the CLI.", ["Steps: 1. Install the CLI."], [0]),
    ("We have 12. The rest are pending.", ["We have 12. The rest are pending."], [0]),
    # Company suffixes (QA R14) split before a capitalized word; unclear.
    ("We are Acme Inc. Our product supports SSO.", ["We are Acme Inc.", "Our product supports SSO."], [0, 1]),
    ("We are Acme Inc. and others.", ["We are Acme Inc. and others."], []),
    # Codex thermo r4: a company suffix before a digit is unclear.
    ("We partnered with Acme Inc. 2024 revenue doubled.", ["We partnered with Acme Inc. 2024 revenue doubled."], [0]),
    ("We are Acme Corp. Pricing is flat.", ["We are Acme Corp.", "Pricing is flat."], [0, 1]),
    # Terminators before closing quotes and brackets.
    ('We said "ship it." Customers agreed.', ['We said "ship it."', "Customers agreed."], []),
    # Codex review r8: a quoted terminator before a lowercase word is unclear.
    ('Our product is "fast." according to customers.', ['Our product is "fast." according to customers.'], [0]),
    ('"Is it fast?" asked a customer. Yes.', ['"Is it fast?" asked a customer.', "Yes."], [0]),
    # Codex thermo r8: a missing space joins two sentences; unclear.
    ("Our product is fast.It saves money.", ["Our product is fast.It saves money."], [0]),
    ("We ship fast.AI saves time.", ["We ship fast.AI saves time."], [0]),
    # Codex thermo r11: uppercase-ending words, Unicode capitals, ellipses.
    ("We use AI.It saves time.", ["We use AI.It saves time."], [0]),
    ("We ship today.\u00c9lodie runs sales.", ["We ship today.\u00c9lodie runs sales."], [0]),
    ("We launched\u2026 What do you think?", ["We launched\u2026", "What do you think?"], []),
    ("We launched\u2026 and then grew.", ["We launched\u2026 and then grew."], [0]),
    ("We launched... and then grew.", ["We launched... and then grew."], [0]),
    ("We build on Node.js and ASP.NET today.", ["We build on Node.js and ASP.NET today."], []),
    # Codex thermo r9: emoji after a terminator do not hide it.
    ("Our product saves time!\U0001F680 Customers pay monthly.", ["Our product saves time!\U0001F680", "Customers pay monthly."], []),
    ("We ship in the U.S.\U0001F1FA\U0001F1F8 Customers pay monthly.", ["We ship in the U.S.\U0001F1FA\U0001F1F8 Customers pay monthly."], [0]),
    ("Fast setup (really!) Clear pricing.", ["Fast setup (really!)", "Clear pricing."], []),
    # Ordinary sentence ends.
    ("Fast setup. Clear pricing! Any questions?", ["Fast setup.", "Clear pricing!", "Any questions?"], []),
    ("Wait... Then it worked.", ["Wait...", "Then it worked."], []),
]


@pytest.mark.parametrize("text,expected,unclear", GOLDEN_SPLITS)
def test_golden_splits(text, expected, unclear):
    assert split_into_sentences(text) == expected


@pytest.mark.parametrize("text,expected,unclear", GOLDEN_SPLITS)
def test_golden_unclear_sentences(text, expected, unclear):
    sentences = split_into_sentences(text)
    assert [i for i in range(len(sentences)) if sentence_has_unclear_boundary(sentences, i)] == unclear


@pytest.mark.parametrize("text,expected,unclear", GOLDEN_SPLITS)
def test_unclear_sentences_are_not_selectable(text, expected, unclear):
    sentences = split_into_sentences(text)
    for i in unclear:
        error = claim_selectability_error(sentences, i)
        assert error, sentences[i]


def test_split_is_deterministic():
    text = " ".join(t for t, _, _ in GOLDEN_SPLITS)
    first = split_into_sentences(text)
    assert all(split_into_sentences(text) == first for _ in range(5))


def test_sentence_start_after_quote_or_newline():
    assert split_into_sentences('We sell in the U.S. "We ship weekly."') == ["We sell in the U.S.", '"We ship weekly."']
    assert split_into_sentences("We sell in the U.S.\nWe ship weekly.") == ["We sell in the U.S.", "We ship weekly."]
    assert split_into_sentences("We sell in the U.S.\nmarket today.") == ["We sell in the U.S.\nmarket today."]


def test_workflow_code_never_splits_sentences():
    """Split changes are replay-safe only while the splitters run in activities.

    Replay reuses recorded activity results, so a new split rule cannot change
    a replayed Review. If workflow code called a splitter, it would. The
    recorded-history replay tests are in test_reply_plan_workflow.py.
    """
    splitter_names = (
        "split_into_sentences", "ends_with_abbreviation", "insert_claim_sentence",
        "claim_selectability_error", "question_spans", "validate_reply_plan",
        "sentence_boundary", "period_boundary", "legacy_split",
    )
    workflows = Path(__file__).resolve().parents[1] / "workflows"
    for path in workflows.glob("*.py"):
        source = path.read_text()
        for name in splitter_names:
            assert name not in source, f"{path.name} references {name}"


# ---------- the shared rule ----------

@pytest.mark.parametrize(
    "word,kind",
    [
        ("U.S.A.", "terminal"),
        ("u.s.", "terminal"),
        ("etc.", "terminal"),
        ("Ph.D.", "terminal"),
        ("Jr.", "terminal"),
        ("(U.S.)", "terminal"),
        ("J.", "initial"),
        ("j.", "initial"),
        ("\u00c9.", "initial"),
        ("\u00c9.U.", "terminal"),
        ("Dr.", "prefix"),
        ("Mt.", "prefix"),
        ("e.g.", "prefix"),
        ("Inc.", "company"),
        ("1.", "list_marker"),
        ("12.", "list_marker"),
        ("2021.", "number"),
        ("$5.", "number"),
        ("99.9.", "number"),
        ("done.", None),
        ("Zinc.", None),
        ("wait...", None),
        ("U.S.?", None),
    ],
)
def test_abbreviation_kind(word, kind):
    assert abbreviation_kind(word) == kind


@pytest.mark.parametrize(
    "word,next_word,expected",
    [
        ("U.S.", None, END),
        ("Dr.", None, END),
        ("done.", "Customers", END),
        ("U.S.", "market", CONTINUE),
        ("U.S.", "We", LIKELY_END),
        ("U.S.", "It’s", LIKELY_END),
        ("U.S.", "The", LIKELY_END),
        ("U.S.", "Customers", LIKELY_CONTINUE),
        ("U.S.", "$5", LIKELY_CONTINUE),
        ("J.", "2024", LIKELY_CONTINUE),
        ('U.S."', "Customers", LIKELY_CONTINUE),
        ("2021.", "We", END),
        ("2021.", "Customers", END),
        ("2021.", "we", END),
        ("2021.", "$5", LIKELY_CONTINUE),
        ("Dr.", "Smith", CONTINUE),
        ("Dr.", "We", LIKELY_CONTINUE),
        ("approx.", "10", CONTINUE),
        ("J.", "Smith", LIKELY_CONTINUE),
        ("J.", "The", LIKELY_CONTINUE),
        ("j.", "smith", CONTINUE),
        ("Inc.", "Customers", LIKELY_END),
        ("Inc.", "and", CONTINUE),
        ("Inc.", "2024", LIKELY_CONTINUE),
        ("Fig.", "3", CONTINUE),
        ("12.", "The", LIKELY_CONTINUE),
    ],
)
def test_period_boundary(word, next_word, expected):
    assert period_boundary(word, next_word) == expected


def test_list_marker_at_sentence_start():
    assert period_boundary("1.", "install", sentence_start=True) == CONTINUE
    assert period_boundary("1.", "install", sentence_start=False) == LIKELY_CONTINUE
    # Codex thermo r7: "99. Is that good?" may be a score before a question.
    assert period_boundary("99.", "Is", sentence_start=True) == LIKELY_CONTINUE


def test_every_fixed_phrase_starts_a_new_sentence():
    """Fixed phrases begin with sentence starters (the starter list names them)."""
    for phrase in NON_CLAIM_PHRASES:
        assert starts_new_sentence(phrase.text.split()[0]), phrase.id


# ---------- selectability ----------

def test_sentence_ending_in_an_abbreviation_is_not_selectable():
    assert ends_with_abbreviation("We operate in the U.S.A.")
    assert ends_with_abbreviation("We sell in the U.S.")
    assert ends_with_abbreviation("We support Slack, Teams, etc.")
    assert ends_with_abbreviation("We offer Plan B.")
    assert ends_with_abbreviation("We are Acme Inc.")
    assert ends_with_abbreviation("We are Acme inc.")


def test_sentence_with_a_mid_sentence_abbreviation_is_selectable():
    for sentence in [
        "Is the U.S.A. market ready?",
        "We sell in the U.S. market today.",
        "Is it live in the U.S.?",
        "We launched in 2021.",
        "Our team has 12.",
        "1. install the CLI first.",
        "Built by j. smith in 2021.",
        "Use a queue, e.g. Postgres or Redis.",
    ]:
        assert claim_selectability_error([sentence], 0) == "", sentence


def test_unclear_reason_does_not_trigger_the_abbreviation_explanation():
    """ReviewCard.tsx shows any reason containing 'abbreviation' as ends-with-an-abbreviation."""
    assert "abbreviation" not in UNCLEAR_BOUNDARY_REASON.lower()


def test_insert_rejects_both_sides_of_an_unclear_split():
    """Codex thermo r3: "The Netherlands, and France." is a fragment, not a claim."""
    materials = {"passage_0": material("We ship to the U.S. The Netherlands, and France. Our API is fast.")}
    ok, _, error = insert_claim_sentence(claim(0), materials)
    assert not ok and "abbreviation" in error.lower()
    ok, _, error = insert_claim_sentence(claim(1), materials)
    assert not ok and error == UNCLEAR_BOUNDARY_REASON
    ok, text, _ = insert_claim_sentence(claim(2), materials)
    assert ok and text == "Our API is fast"


def test_insert_keeps_a_mid_sentence_initialism_whole():
    """Before issue #10 the splitter cut this to "We sell in the U.S.A" (a partial span)."""
    materials = {"passage_0": material("We sell in the U.S.A. market today. Our API is fast.")}
    ok, text, error = insert_claim_sentence(claim(0), materials)
    assert ok, error
    assert text == "We sell in the U.S.A. market today"


@pytest.mark.parametrize(
    "passage",
    [
        "Built by J. The artist Smith in 2021.",  # no partial span after an initial
        "We ship in the U.S. Customers pay monthly.",  # no two sentences as one claim
        "We use e.g. We ship weekly.",
        "We ship in the U.S.” Customers pay monthly.",
    ],
)
def test_insert_rejects_a_sentence_with_an_unclear_boundary(passage):
    materials = {"passage_0": material(passage)}
    assert len(split_into_sentences(passage)) == 1
    ok, _, error = insert_claim_sentence(claim(0), materials)
    assert not ok
    assert error == UNCLEAR_BOUNDARY_REASON


def test_list_marker_is_never_a_claim_by_itself():
    """Codex review r1: "1." must not split off and be quoted as the claim "1"."""
    materials = {"passage_0": material("1. The product supports SSO. 2. The API is fast.")}
    assert split_into_sentences(materials["passage_0"].passage)[0] == "1. The product supports SSO."
    ok, _, error = insert_claim_sentence(claim(0), materials)
    assert not ok and error == UNCLEAR_BOUNDARY_REASON


def test_prompt_lists_only_selectable_sentences_under_the_new_numbers():
    listing = format_materials_for_prompt([material(
        "We sell in the U.S.A. market today. We ship in the U.S. Customers pay monthly. Our API is fast."
    )])
    assert "  0: We sell in the U.S.A. market today." in listing
    assert "Customers pay monthly" not in listing
    assert "  2: Our API is fast." in listing


# ---------- splitter version guard (deploy window) ----------

AFFECTED = "We sell in the U.S.A. market today. Acme ships weekly. Acme prices fairly."


def test_affected_passage_numbers_differently_under_v1():
    assert split_into_sentences_v1(AFFECTED)[1] == "market today."
    assert split_into_sentences(AFFECTED)[1] == "Acme ships weekly."


def test_unversioned_index_into_a_changed_split_is_refused():
    """A draft numbered under the old split must not quote a different sentence."""
    ok, _, error = insert_claim_sentence(claim(1, version=None), {"passage_0": material(AFFECTED)})
    assert not ok
    assert "splitter version 1" in error and "redraft" in error


def test_unversioned_index_with_the_same_text_is_accepted():
    """Passages the change does not affect keep working for drafts made before it."""
    passage = "Our product helps developers build reliable systems. Our API is fast."
    ok, text, _ = insert_claim_sentence(claim(1, version=None), {"passage_0": material(passage)})
    assert ok and text == "Our API is fast"
    # In an affected passage, an index before the changed split point still matches.
    ok, text, _ = insert_claim_sentence(
        claim(1, version=None), {"passage_0": material("Our API is fast. We ship weekly. We sell in the U.S.A. market.")}
    )
    assert ok and text == "We ship weekly"


def test_index_from_another_splitter_version_is_refused():
    ok, _, error = insert_claim_sentence(claim(0, version=SPLITTER_VERSION + 1), {"passage_0": material(AFFECTED)})
    assert not ok and "redraft" in error


async def test_claim_gate_fails_an_unversioned_draft_into_a_changed_split():
    result = await claim_gate(ClaimGateInput(
        segments=[Segment(kind="claim", source_passage_id="passage_0", sentence_index=1)],
        founder_materials=[material(AFFECTED)],
        idempotency_key="issue10_window",
    ))
    assert not result.passed
    assert "redraft" in result.failed_sentences[0][1]


# ---------- claim gate and edit re-gate ----------

async def test_claim_gate_rejects_two_sentences_hidden_by_a_closing_quote():
    """Codex thermo r2: 'U.S.\u201d Customers' is checked like 'U.S. Customers'."""
    result = await claim_gate(ClaimGateInput(
        segments=[claim(0)],
        founder_materials=[material("We ship in the U.S.\u201d Customers pay monthly.")],
        idempotency_key="issue10_closer",
    ))
    assert not result.passed
    assert result.failed_sentences[0][1] == UNCLEAR_BOUNDARY_REASON


async def test_edit_regate_splits_after_a_quoted_sentence_end():
    results = await verify_edited_text(EditedTextGateInput(
        edited_text='We said "ship it." Customers agreed.',
        gated_segments=[],
        founder_materials=[material("Customers agreed.")],
        idempotency_key="issue10_edit_closer",
    ))
    assert [r.sentence for r in results] == ['We said "ship it."', "Customers agreed."]
    assert [r.passed for r in results] == [False, True]


async def test_edit_regate_does_not_keep_a_v1_fragment_verdict():
    """Codex thermo r2: a card gated under the pre-#10 split kept "We sell in the U.S.A"."""
    passage = "We sell in the U.S.A. market today."
    v1_fragment = split_into_sentences_v1(passage)[0].rstrip(".")
    assert v1_fragment == "We sell in the U.S.A"
    stored = Segment(kind="claim", text=v1_fragment, source_passage_id="passage_0", sentence_index=0)
    results = await verify_edited_text(EditedTextGateInput(
        edited_text="We sell in the U.S.A.",
        gated_segments=[stored],
        founder_materials=[material(passage)],
        idempotency_key="issue10_v1_fragment",
    ))
    assert len(results) == 1
    assert not results[0].passed
    assert not results[0].kept_segment


async def test_edit_regate_accepts_a_selectable_duplicate_in_any_passage():
    """Codex review r6: the same sentence after an unclear boundary in another passage."""
    results = await verify_edited_text(EditedTextGateInput(
        edited_text="We ship weekly.",
        gated_segments=[],
        founder_materials=[
            material("Our API is fast. We ship weekly.", "passage_0"),
            material("We are Acme Inc. We ship weekly.", "passage_1"),
        ],
        idempotency_key="issue10_duplicate",
    ))
    assert results[0].passed, results[0].reason
    assert (results[0].source_passage_id, results[0].sentence_index) == ("passage_0", 1)


def test_unversioned_guard_compares_exact_text():
    """Codex thermo r6 (nit): normalization must not hide a changed sentence."""
    passage = "We sell in the U.S.A. market today. Market today."
    assert split_into_sentences_v1(passage)[1] == "market today."
    ok, _, error = insert_claim_sentence(claim(1, version=None), {"passage_0": material(passage)})
    assert not ok and "redraft" in error


async def test_edit_regate_keeps_an_unversioned_whole_sentence_verdict():
    """In-flight cards gated before #10 keep claims that are still whole sentences."""
    passage = "Our API is fast. We ship weekly."
    stored = Segment(kind="claim", text="We ship weekly", source_passage_id="passage_0", sentence_index=1)
    results = await verify_edited_text(EditedTextGateInput(
        edited_text="We ship weekly.",
        gated_segments=[stored],
        founder_materials=[material(passage)],
        idempotency_key="issue10_v1_whole",
    ))
    assert results[0].passed and results[0].kept_segment

async def test_claim_gate_passes_a_claim_with_a_mid_sentence_initialism():
    result = await claim_gate(ClaimGateInput(
        segments=[
            Segment(kind="non_claim", phrase_id="thanks.for_sharing"),
            claim(0),
            Segment(kind="non_claim", phrase_id="q.current_tool"),
        ],
        founder_materials=[material("We sell in the U.S.A. market today. Our API is fast.")],
        idempotency_key="issue10_gate",
    ))
    assert result.passed, result.failed_sentences
    assert result.draft_text == (
        "Thanks for sharing. We sell in the U.S.A. market today. What are you using today?"
    )
    assert result.segments[1].splitter_version == SPLITTER_VERSION


@pytest.mark.parametrize("phrase_id", [p.id for p in NON_CLAIM_PHRASES if p.category in ("offer", "question_to_poster")])
async def test_claim_ending_in_a_number_maps_before_every_closer(phrase_id):
    """Before issue #10, "We launched in 2021. Happy to help." never split and failed the gate."""
    result = await claim_gate(ClaimGateInput(
        segments=[claim(0), Segment(kind="non_claim", phrase_id=phrase_id)],
        founder_materials=[material("We launched in 2021.")],
        idempotency_key=f"issue10_number_{phrase_id}",
    ))
    assert result.passed, result.failed_sentences


async def test_edit_regate_matches_a_passage_sentence_with_an_initialism():
    results = await verify_edited_text(EditedTextGateInput(
        edited_text="Thanks for sharing. We sell in the U.S.A. market today.",
        gated_segments=[],
        founder_materials=[material("We sell in the U.S.A. market today. Our API is fast.")],
        idempotency_key="issue10_edit",
    ))
    assert [r.passed for r in results] == [True, True]
    assert results[1].kind == "claim"
    assert results[1].sentence_index == 0


async def test_edit_regate_rejects_a_passage_sentence_ending_in_an_initialism():
    results = await verify_edited_text(EditedTextGateInput(
        edited_text="We sell in the U.S.",
        gated_segments=[],
        founder_materials=[material("We sell in the U.S. We ship weekly.")],
        idempotency_key="issue10_edit_end",
    ))
    assert len(results) == 1
    assert not results[0].passed
    assert "abbreviation" in results[0].reason.lower()


@pytest.mark.parametrize(
    "passage",
    [
        "We ship in the U.S. Customers pay monthly.",
        "Built by J. The artist Smith in 2021.",
        "We partnered with Acme Inc. 2024 revenue doubled.",
        "Our founder is \u00c9. Dubois, who built the product.",
    ],
)
async def test_edit_regate_rejects_a_passage_sentence_with_an_unclear_boundary(passage):
    """Two founder sentences cannot pass the edit path as one claim."""
    results = await verify_edited_text(EditedTextGateInput(
        edited_text=f"Thanks for sharing. {passage}",
        gated_segments=[],
        founder_materials=[material(passage)],
        idempotency_key="issue10_edit_ambiguous",
    ))
    assert len(results) == 2
    assert not results[1].passed
    assert results[1].reason == UNCLEAR_BOUNDARY_REASON


async def test_claim_gate_assembles_a_quoted_sentence_without_extra_punctuation():
    """Codex review r3: a terminator before a closing quote needs no added period."""
    result = await claim_gate(ClaimGateInput(
        segments=[claim(0), Segment(kind="non_claim", phrase_id="q.current_tool")],
        founder_materials=[material('Our motto is "We ship today." We ship weekly.')],
        idempotency_key="issue10_quoted",
    ))
    assert result.passed, result.failed_sentences
    assert result.draft_text == 'Our motto is "We ship today." What are you using today?'


# ---------- reply plan question spans ----------

def plan(target: str) -> ReplyPlan:
    return ReplyPlan(
        reply_type="answer_question",
        target_point=target,
        mention_product=False,
        mention_product_reason="A synthetic reason.",
    )


@pytest.mark.parametrize(
    "post,question",
    [
        ("Is the U.S.A. market ready?", "Is the U.S.A. market ready?"),
        ("Moving to Rust. Dr. Smith, can you help?", "Dr. Smith, can you help?"),
        ("Any tools for queues, e.g. Kafka or NATS?", "Any tools for queues, e.g. Kafka or NATS?"),
        ("Hiring in the U.K. soon. Who handles payroll?", "Who handles payroll?"),
    ],
)
def test_whole_question_with_an_abbreviation_is_a_valid_target(post, question):
    assert [post[a:b].strip() for a, b in question_spans(post)] == [question]
    assert validate_reply_plan(plan(question), post) == []


@pytest.mark.parametrize(
    "post,statement,question",
    [
        ("We moved to the U.S. We need payroll. Any tips?", "We need payroll.", "Any tips?"),
        # Codex review r1: an ambiguous period splits post text.
        ("Revenue doubled in 2021. Canals or railways?", "Revenue doubled in 2021.", "Canals or railways?"),
        ("We sell in the U.S. Customers pay monthly?", "We sell in the U.S.", "Customers pay monthly?"),
        # Codex review r2: an initial before a capitalized word splits post text.
        ("Version A. Anyone interested?", "Version A.", "Anyone interested?"),
        ("We chose Plan B. Customers need help?", "We chose Plan B.", "Customers need help?"),
        # Codex thermo r4
        ("We launched in 2021. what changed?", "We launched in 2021.", "what changed?"),
        ("We partnered with Acme Inc. 2024 was profitable?", "We partnered with Acme Inc.", "2024 was profitable?"),
    ],
)
def test_statement_before_a_question_is_outside_it(post, statement, question):
    assert [post[a:b].strip() for a, b in question_spans(post)] == [question]
    assert validate_reply_plan(plan(statement), post) != []


def test_emoji_after_a_statement_stays_outside_the_question():
    """Codex thermo r9."""
    post = "We launched!\U0001F680 Any tips?"
    # The emoji lands in the question span; it is not a statement.
    assert [post[a:b].strip(" \U0001F680") for a, b in question_spans(post)] == ["Any tips?"]
    assert validate_reply_plan(plan("We launched!"), post) != []


@pytest.mark.parametrize("post", ["What do you think? \U0001F914", "What do you think?\U0001F914\U0001F914"])
def test_question_target_may_include_a_trailing_emoji(post):
    """Codex review r10."""
    assert validate_reply_plan(plan(post), post) == []


async def test_claim_with_trailing_emoji_assembles_without_extra_punctuation():
    result = await claim_gate(ClaimGateInput(
        segments=[claim(0), Segment(kind="non_claim", phrase_id="q.current_tool")],
        founder_materials=[material("Our product saves time!\U0001F680 Customers pay monthly.")],
        idempotency_key="issue10_emoji",
    ))
    assert result.passed, result.failed_sentences
    assert result.draft_text == "Our product saves time!\U0001F680 What are you using today?"


def test_missing_space_before_a_question_splits_post_text():
    """Codex thermo r8."""
    post = "We grew.Is that good?"
    assert [post[a:b].strip() for a, b in question_spans(post)] == ["Is that good?"]
    assert validate_reply_plan(plan("We grew."), post) != []
    post = "We launched.AI helps?"
    assert [post[a:b].strip() for a, b in question_spans(post)] == ["AI helps?"]
    post = "We use AI.Is that useful?"
    assert [post[a:b].strip() for a, b in question_spans(post)] == ["Is that useful?"]
    post = "We launched\u2026 What do you think?"
    assert [post[a:b].strip() for a, b in question_spans(post)] == ["What do you think?"]
    post = "We use ASP.NET. Is it good?"
    assert [post[a:b].strip() for a, b in question_spans(post)] == ["Is it good?"]
    post = "Anyone using Node.js for queues?"
    assert [post[a:b].strip() for a, b in question_spans(post)] == [post]


def test_score_before_a_question_is_outside_it():
    """Codex thermo r7."""
    post = "99. Is that good?"
    assert [post[a:b].strip() for a, b in question_spans(post)] == ["Is that good?"]


def test_url_at_the_end_of_a_quoted_question():
    """Codex thermo r7 (nit): URL masking stops before closing guillemets."""
    post = "\u00abHave you tried https://example.com?\u00bb Next."
    assert [post[a:b].strip() for a, b in question_spans(post)] == ["\u00abHave you tried https://example.com?\u00bb"]


def test_closing_guillemet_ends_a_statement_before_a_question():
    """Codex thermo r6."""
    post = "\u00abWe launched.\u00bb What do you think?"
    assert [post[a:b].strip() for a, b in question_spans(post)] == ["What do you think?"]
    assert validate_reply_plan(plan("\u00abWe launched.\u00bb"), post) != []


def test_claim_splitter_stays_linear_on_many_initials():
    """Codex thermo r2: the sentence-start check must not rebuild the sentence."""
    def elapsed(n: int) -> float:
        text = "J. " * n + "Smith."
        started = time.perf_counter()
        split_into_sentences(text)
        return time.perf_counter() - started

    elapsed(1000)  # warm up
    small, large = elapsed(2000), elapsed(16000)
    assert large < small * 8 * 4  # 8x the input; allow 4x noise over linear


def test_question_spans_stay_linear_on_many_periods():
    """Codex thermo r1: the word lookup must not rescan the post from the start."""
    def elapsed(n: int) -> float:
        text = "x. " * n + "J. " * n + " " * n + "Dr. " * n + "aa.Aa" * n + "Smith?"
        started = time.perf_counter()
        question_spans(text)
        return time.perf_counter() - started

    elapsed(1000)  # warm up
    small, large = elapsed(2000), elapsed(16000)
    assert large < small * 8 * 4  # 8x the input; allow 4x noise over linear
