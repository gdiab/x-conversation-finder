"""
Sentence splitting after abbreviations (issue #10, ADR-0006 claim gate contract).

The claim gate's splitter (founder passages, edited drafts) and the reply
plan's question-span splitter (post text) share one rule for periods:
backend.gate.sentence_boundary. All text here is synthetic.
"""

import pytest

from backend.activities.claim_gate_activity import (
    ClaimGateInput,
    EditedTextGateInput,
    claim_gate,
    ends_with_abbreviation,
    insert_claim_sentence,
    split_into_sentences,
    verify_edited_text,
)
from backend.adapters.plain_reply_agent import format_materials_for_prompt
from backend.gate.sentence_boundary import abbreviation_kind, period_ends_sentence
from backend.models import FounderMaterial, ReplyPlan, Segment
from backend.planning import question_spans, validate_reply_plan


def material(passage: str, passage_id: str = "passage_0") -> FounderMaterial:
    return FounderMaterial(source="brief.md", passage=passage, relevance="relevant", passage_id=passage_id)


# ---------- golden splits ----------
#
# Sentence indices are positions in these splits, and claim segments store
# them. A change to any expected list here changes the indices that drafts
# reference; see ADR-0006 (2026-11-21 amendment, item 2) before updating one.

GOLDEN_SPLITS = [
    # Initialisms inside a sentence (the issue's example).
    ("Is the U.S.A. market ready?", ["Is the U.S.A. market ready?"]),
    ("We sell in the U.S. market today.", ["We sell in the U.S. market today."]),
    ("Our U.K. team ships on Fridays.", ["Our U.K. team ships on Fridays."]),
    ("Standups start at 9 a.m. sharp.", ["Standups start at 9 a.m. sharp."]),
    # An initialism that ends a sentence: split only before a clear sentence start.
    ("We sell in the U.S. We ship weekly.", ["We sell in the U.S.", "We ship weekly."]),
    ("We sell in the U.S. Customers love it.", ["We sell in the U.S. Customers love it."]),
    ("Is it live in the U.S.? Yes.", ["Is it live in the U.S.?", "Yes."]),
    ("We operate in the U.S.A.", ["We operate in the U.S.A."]),
    # e.g. and i.e. mid-sentence never split, even before a capitalized word.
    ("Use a queue, e.g. Postgres or Redis.", ["Use a queue, e.g. Postgres or Redis."]),
    ("Pick one store, i.e. The primary database.", ["Pick one store, i.e. The primary database."]),
    ("Foo vs. It is shipped.", ["Foo vs. It is shipped."]),
    # Titles never split (QA R13).
    ("Ask Dr. Smith about it.", ["Ask Dr. Smith about it."]),
    ("Ask Dr. We about it.", ["Ask Dr. We about it."]),
    # etc. and St. can end a sentence.
    ("We support Slack, Teams, etc. We add more monthly.", ["We support Slack, Teams, etc.", "We add more monthly."]),
    ("We support Slack, Teams, etc. and more.", ["We support Slack, Teams, etc. and more."]),
    ("Our office is on Main St. We host meetups.", ["Our office is on Main St.", "We host meetups."]),
    ("We met at St. Louis Station.", ["We met at St. Louis Station."]),
    # Single-letter initials.
    ("Built by J. Smith last year.", ["Built by J. Smith last year."]),
    ("Built by J. A. Smith last year.", ["Built by J. A. Smith last year."]),
    ("We offer Plan B. It costs less.", ["We offer Plan B.", "It costs less."]),
    # Ph.D. (QA R11 test) splits before a clear sentence start.
    ("Founded by Jo Lee, Ph.D. The company is small.", ["Founded by Jo Lee, Ph.D.", "The company is small."]),
    # Decimals and numbers (S-5).
    (
        "Our product has 99.99% uptime. It handles 1.5 million requests per second. Great performance.",
        ["Our product has 99.99% uptime.", "It handles 1.5 million requests per second.", "Great performance."],
    ),
    ("Uptime was 99.9. We fixed that.", ["Uptime was 99.9.", "We fixed that."]),
    ("We launched in 2021. We now serve teams.", ["We launched in 2021.", "We now serve teams."]),
    ("Steps: 1. Install the CLI.", ["Steps: 1. Install the CLI."]),
    # Company suffixes (QA R14) are unchanged.
    ("We are Acme Inc. Our product supports SSO.", ["We are Acme Inc.", "Our product supports SSO."]),
    ("We are Acme Inc. and others.", ["We are Acme Inc. and others."]),
    ("We are Acme Corp. Pricing is flat.", ["We are Acme Corp.", "Pricing is flat."]),
    # Ordinary sentence ends.
    ("Fast setup. Clear pricing! Any questions?", ["Fast setup.", "Clear pricing!", "Any questions?"]),
    ("Wait... Then it worked.", ["Wait...", "Then it worked."]),
]


@pytest.mark.parametrize("text,expected", GOLDEN_SPLITS)
def test_golden_splits(text, expected):
    assert split_into_sentences(text) == expected


def test_split_is_deterministic():
    text = " ".join(t for t, _ in GOLDEN_SPLITS)
    first = split_into_sentences(text)
    assert all(split_into_sentences(text) == first for _ in range(5))


def test_workflow_code_never_splits_sentences():
    """Split changes are replay-safe only while the splitters run in activities.

    Replay reuses recorded activity results, so a new split rule cannot change
    a replayed Review. If workflow code called a splitter, it would. The
    recorded-history replay tests are in test_reply_plan_workflow.py.
    """
    from pathlib import Path

    splitter_names = (
        "split_into_sentences", "ends_with_abbreviation", "insert_claim_sentence",
        "question_spans", "validate_reply_plan", "sentence_boundary", "period_ends_sentence",
    )
    workflows = Path(__file__).resolve().parents[1] / "workflows"
    for path in workflows.glob("*.py"):
        source = path.read_text()
        for name in splitter_names:
            assert name not in source, f"{path.name} references {name}"


def test_sentence_start_after_quote_or_newline():
    assert split_into_sentences('We sell in the U.S. "We ship weekly."') == ["We sell in the U.S.", '"We ship weekly."']
    assert split_into_sentences("We sell in the U.S.\nWe ship weekly.") == ["We sell in the U.S.", "We ship weekly."]
    assert split_into_sentences("We sell in the U.S.\nmarket today.") == ["We sell in the U.S.\nmarket today."]


# ---------- the shared rule ----------

@pytest.mark.parametrize(
    "word,kind",
    [
        ("U.S.A.", "terminal"),
        ("u.s.", "terminal"),
        ("J.", "terminal"),
        ("etc.", "terminal"),
        ("Ph.D.", "terminal"),
        ("(U.S.)", "terminal"),
        ("Dr.", "prefix"),
        ("e.g.", "prefix"),
        ("Inc.", "company"),
        ("2021.", "number"),
        ("99.9.", "number"),
        ("done.", None),
        ("j.", None),  # single lowercase letter: an ordinary word
        ("Zinc.", None),
        ("wait...", None),
        ("U.S.?", None),
    ],
)
def test_abbreviation_kind(word, kind):
    assert abbreviation_kind(word) == kind


def test_period_ends_sentence_at_end_of_text():
    assert period_ends_sentence("U.S.", None)
    assert period_ends_sentence("Dr.", None)


def test_next_word_must_be_a_capitalized_starter():
    assert period_ends_sentence("U.S.", "We")
    assert period_ends_sentence("U.S.", "It's")
    assert period_ends_sentence("U.S.", "It’s")
    assert not period_ends_sentence("U.S.", "we")  # lowercase
    assert not period_ends_sentence("U.S.", "Market")  # not a starter
    assert not period_ends_sentence("J.", "I.")  # another initial
    assert not period_ends_sentence("J.", "Will")  # a name, deliberately not a starter


# ---------- selectability ----------

def test_sentence_ending_in_an_initialism_is_not_selectable():
    assert ends_with_abbreviation("We operate in the U.S.A.")
    assert ends_with_abbreviation("We sell in the U.S.")
    assert ends_with_abbreviation("We support Slack, Teams, etc.")
    assert ends_with_abbreviation("We offer Plan B.")
    assert ends_with_abbreviation("We are Acme Inc.")
    assert ends_with_abbreviation("We are Acme inc.")


def test_sentence_with_a_mid_sentence_abbreviation_is_selectable():
    assert not ends_with_abbreviation("Is the U.S.A. market ready?")
    assert not ends_with_abbreviation("We sell in the U.S. market today.")
    assert not ends_with_abbreviation("Is it live in the U.S.?")
    assert not ends_with_abbreviation("We launched in 2021.")


def test_insert_rejects_a_sentence_ending_in_an_initialism():
    materials = {"passage_0": material("We sell in the U.S. We ship weekly.")}
    ok, _, error = insert_claim_sentence(
        Segment(kind="claim", source_passage_id="passage_0", sentence_index=0), materials
    )
    assert not ok
    assert "abbreviation" in error.lower()
    ok, text, _ = insert_claim_sentence(
        Segment(kind="claim", source_passage_id="passage_0", sentence_index=1), materials
    )
    assert ok and text == "We ship weekly"


def test_insert_keeps_a_mid_sentence_initialism_whole():
    """Before issue #10 the splitter cut this to "We sell in the U.S" (a partial span)."""
    materials = {"passage_0": material("We sell in the U.S.A. market today. Our API is fast.")}
    ok, text, error = insert_claim_sentence(
        Segment(kind="claim", source_passage_id="passage_0", sentence_index=0), materials
    )
    assert ok, error
    assert text == "We sell in the U.S.A. market today"


def test_prompt_numbers_match_the_new_split():
    listing = format_materials_for_prompt([material("We sell in the U.S.A. market today. Our API is fast.")])
    assert "  0: We sell in the U.S.A. market today." in listing
    assert "  1: Our API is fast." in listing


# ---------- claim gate and edit re-gate ----------

async def test_claim_gate_passes_a_claim_with_a_mid_sentence_initialism():
    result = await claim_gate(ClaimGateInput(
        segments=[
            Segment(kind="non_claim", phrase_id="thanks.for_sharing"),
            Segment(kind="claim", source_passage_id="passage_0", sentence_index=0),
            Segment(kind="non_claim", phrase_id="q.current_tool"),
        ],
        founder_materials=[material("We sell in the U.S.A. market today. Our API is fast.")],
        idempotency_key="issue10_gate",
    ))
    assert result.passed, result.failed_sentences
    assert result.draft_text == (
        "Thanks for sharing. We sell in the U.S.A. market today. What are you using today?"
    )


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


def test_sentence_before_a_question_is_still_outside_it():
    post = "We moved to the U.S. We need payroll. Any tips?"
    assert [post[a:b].strip() for a, b in question_spans(post)] == ["Any tips?"]
    assert validate_reply_plan(plan("We need payroll."), post) != []
