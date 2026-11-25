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
    AMBIGUOUS_BOUNDARY_REASON,
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
    AMBIGUOUS,
    CONTINUE,
    END,
    SPLITTER_VERSION,
    abbreviation_kind,
    has_ambiguous_boundary,
    period_boundary,
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
# them. A change to any expected list here changes the indices that drafts
# reference: bump SPLITTER_VERSION and read ADR-0006 (2026-11-21 amendment,
# item 2) before updating one. The third value says whether every sentence
# in the split is selectable as far as the boundary rules go.

GOLDEN_SPLITS = [
    # Initialisms inside a sentence (the issue's example).
    ("Is the U.S.A. market ready?", ["Is the U.S.A. market ready?"]),
    ("We sell in the U.S. market today.", ["We sell in the U.S. market today."]),
    ("Our U.K. team ships on Fridays.", ["Our U.K. team ships on Fridays."]),
    ("Standups start at 9 a.m. sharp.", ["Standups start at 9 a.m. sharp."]),
    # An initialism before a clear sentence start splits; before another
    # capitalized word it is ambiguous: no split, and not selectable.
    ("We sell in the U.S. We ship weekly.", ["We sell in the U.S.", "We ship weekly."]),
    ("We ship in the U.S. Customers pay monthly.", ["We ship in the U.S. Customers pay monthly."]),
    ("Is it live in the U.S.? Yes.", ["Is it live in the U.S.?", "Yes."]),
    ("We operate in the U.S.A.", ["We operate in the U.S.A."]),
    # e.g. and i.e. mid-sentence never split, even before a capitalized word.
    ("Use a queue, e.g. Postgres or Redis.", ["Use a queue, e.g. Postgres or Redis."]),
    ("Pick one store, i.e. Postgres.", ["Pick one store, i.e. Postgres."]),
    ("Foo vs. It is shipped.", ["Foo vs. It is shipped."]),
    ("We use e.g. We ship weekly.", ["We use e.g. We ship weekly."]),
    # Titles never split (QA R13).
    ("Ask Dr. Smith about it.", ["Ask Dr. Smith about it."]),
    # etc. and St. can end a sentence.
    ("We support Slack, Teams, etc. We add more monthly.", ["We support Slack, Teams, etc.", "We add more monthly."]),
    ("We support Slack, Teams, etc. and more.", ["We support Slack, Teams, etc. and more."]),
    ("We support Slack, Teams, etc. Pricing is flat.", ["We support Slack, Teams, etc. Pricing is flat."]),
    ("Our office is on Main St. We host meetups.", ["Our office is on Main St.", "We host meetups."]),
    ("We met at St. Louis Station.", ["We met at St. Louis Station."]),  # "Main St. Louis ..." reads both ways
    # Single-letter initials before a capitalized word are ambiguous (name
    # initial or sentence end); before a lowercase word they continue.
    ("Built by J. Smith last year.", ["Built by J. Smith last year."]),
    ("Built by J. A. Smith last year.", ["Built by J. A. Smith last year."]),
    ("We chose Plan B. Customers need help.", ["We chose Plan B. Customers need help."]),
    ("Pick option B. or C.", ["Pick option B. or C."]),
    ("Built by J. The artist Smith in 2021.", ["Built by J. The artist Smith in 2021."]),
    ("We offer Plan B. It costs less.", ["We offer Plan B. It costs less."]),
    # Ph.D. (QA R11 test) splits before a clear sentence start.
    ("Founded by Jo Lee, Ph.D. The company is small.", ["Founded by Jo Lee, Ph.D.", "The company is small."]),
    # Decimals and numbers (S-5).
    (
        "Our product has 99.99% uptime. It handles 1.5 million requests per second. Great performance.",
        ["Our product has 99.99% uptime.", "It handles 1.5 million requests per second.", "Great performance."],
    ),
    ("Uptime was 99.9. We fixed that.", ["Uptime was 99.9.", "We fixed that."]),
    ("We launched in 2021. We now serve teams.", ["We launched in 2021.", "We now serve teams."]),
    ("We launched in 2021. Customers followed.", ["We launched in 2021. Customers followed."]),
    # List markers stay with their item.
    ("1. The product supports SSO.", ["1. The product supports SSO."]),
    ("Steps: 1. Install the CLI.", ["Steps: 1. Install the CLI."]),
    ("We have 12. The rest are pending.", ["We have 12. The rest are pending."]),
    # Company suffixes (QA R14) are unchanged.
    ("We are Acme Inc. Our product supports SSO.", ["We are Acme Inc.", "Our product supports SSO."]),
    ("We are Acme Inc. and others.", ["We are Acme Inc. and others."]),
    ("We are Acme Corp. Pricing is flat.", ["We are Acme Corp.", "Pricing is flat."]),
    # Terminators before closing quotes and brackets.
    ('We said "ship it." Customers agreed.', ['We said "ship it."', "Customers agreed."]),
    ("We ship in the U.S.\u201d Customers pay monthly.", ["We ship in the U.S.\u201d Customers pay monthly."]),
    ("Fast setup (really!) Clear pricing.", ["Fast setup (really!)", "Clear pricing."]),
    # Ordinary sentence ends.
    ("Fast setup. Clear pricing! Any questions?", ["Fast setup.", "Clear pricing!", "Any questions?"]),
    ("Wait... Then it worked.", ["Wait...", "Then it worked."]),
]

# Sentences from GOLDEN_SPLITS with a period inside that might end a sentence.
AMBIGUOUS_SENTENCES = [
    "We ship in the U.S. Customers pay monthly.",
    "We use e.g. We ship weekly.",
    "Foo vs. It is shipped.",
    "We support Slack, Teams, etc. Pricing is flat.",
    "We met at St. Louis Station.",
    "Built by J. Smith last year.",
    "Built by J. A. Smith last year.",
    "Built by J. The artist Smith in 2021.",
    "We offer Plan B. It costs less.",
    "We chose Plan B. Customers need help.",
    "We ship in the U.S.\u201d Customers pay monthly.",
    "We launched in 2021. Customers followed.",
    "Steps: 1. Install the CLI.",
    "We have 12. The rest are pending.",
]


@pytest.mark.parametrize("text,expected", GOLDEN_SPLITS)
def test_golden_splits(text, expected):
    assert split_into_sentences(text) == expected


@pytest.mark.parametrize("text,expected", GOLDEN_SPLITS)
def test_only_listed_sentences_have_an_ambiguous_boundary(text, expected):
    for sentence in expected:
        assert has_ambiguous_boundary(sentence) == (sentence in AMBIGUOUS_SENTENCES), sentence


def test_split_is_deterministic():
    text = " ".join(t for t, _ in GOLDEN_SPLITS)
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
        ("(U.S.)", "terminal"),
        ("J.", "initial"),
        ("Dr.", "prefix"),
        ("e.g.", "prefix"),
        ("Inc.", "company"),
        ("1.", "list_marker"),
        ("12.", "list_marker"),
        ("2021.", "number"),
        ("$5.", "number"),
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


@pytest.mark.parametrize(
    "word,next_word,expected",
    [
        ("U.S.", None, END),
        ("Dr.", None, END),
        ("done.", "Customers", END),
        ("U.S.", "We", END),
        ("U.S.", "It's", END),
        ("U.S.", "It’s", END),
        ("U.S.", "market", CONTINUE),
        ("U.S.", "Customers", AMBIGUOUS),
        ("U.S.", "$5", AMBIGUOUS),
        ("2021.", "We", END),
        ("2021.", "Customers", AMBIGUOUS),
        ("Dr.", "Smith", CONTINUE),
        ("Dr.", "We", AMBIGUOUS),
        ("approx.", "10", CONTINUE),
        ("J.", "Smith", AMBIGUOUS),  # "J. Smith" or "Plan B. Customers"
        ("J.", "smith", CONTINUE),
        ("J.", "The", AMBIGUOUS),
        ('U.S."', "Customers", AMBIGUOUS),
        ("Inc.", "Customers", END),
        ("Inc.", "and", CONTINUE),
        ("Inc.", "3", CONTINUE),
        ("12.", "The", AMBIGUOUS),
    ],
)
def test_period_boundary(word, next_word, expected):
    assert period_boundary(word, next_word) == expected


def test_list_marker_at_sentence_start_continues():
    assert period_boundary("1.", "The", sentence_start=True) == CONTINUE
    assert period_boundary("1.", "The", sentence_start=False) == AMBIGUOUS


def test_every_fixed_phrase_starts_a_new_sentence():
    """A claim ending in a number, then any fixed phrase, splits in the assembled draft."""
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
        "1. The product supports SSO.",
        "Use a queue, e.g. Postgres or Redis.",
    ]:
        assert claim_selectability_error(sentence) == "", sentence


@pytest.mark.parametrize("sentence", AMBIGUOUS_SENTENCES)
def test_sentence_with_an_ambiguous_boundary_is_not_selectable(sentence):
    assert claim_selectability_error(sentence) == AMBIGUOUS_BOUNDARY_REASON


def test_ambiguous_reason_does_not_trigger_the_abbreviation_explanation():
    """ReviewCard.tsx shows any reason containing 'abbreviation' as ends-with-an-abbreviation."""
    assert "abbreviation" not in AMBIGUOUS_BOUNDARY_REASON.lower()


def test_insert_rejects_a_sentence_ending_in_an_initialism():
    materials = {"passage_0": material("We sell in the U.S. We ship weekly.")}
    ok, _, error = insert_claim_sentence(claim(0), materials)
    assert not ok
    assert "abbreviation" in error.lower()
    ok, text, _ = insert_claim_sentence(claim(1), materials)
    assert ok and text == "We ship weekly"


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
    ],
)
def test_insert_rejects_a_sentence_with_an_ambiguous_boundary(passage):
    materials = {"passage_0": material(passage)}
    assert len(split_into_sentences(passage)) == 1
    ok, _, error = insert_claim_sentence(claim(0), materials)
    assert not ok
    assert error == AMBIGUOUS_BOUNDARY_REASON


def test_list_marker_is_never_a_claim_by_itself():
    """Codex review r1: "1." must not split off and be quoted as the claim "1"."""
    materials = {"passage_0": material("1. The product supports SSO. 2. The API is fast.")}
    sentences = split_into_sentences(materials["passage_0"].passage)
    assert sentences == ["1. The product supports SSO.", "2. The API is fast."]
    ok, text, _ = insert_claim_sentence(claim(0), materials)
    assert ok and text == "1. The product supports SSO"


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
    assert result.failed_sentences[0][1] == AMBIGUOUS_BOUNDARY_REASON


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
    ["We ship in the U.S. Customers pay monthly.", "Built by J. The artist Smith in 2021."],
)
async def test_edit_regate_rejects_a_passage_sentence_with_an_ambiguous_boundary(passage):
    """Two founder sentences cannot pass the edit path as one claim."""
    results = await verify_edited_text(EditedTextGateInput(
        edited_text=f"Thanks for sharing. {passage}",
        gated_segments=[],
        founder_materials=[material(passage)],
        idempotency_key="issue10_edit_ambiguous",
    ))
    assert len(results) == 2
    assert not results[1].passed
    assert results[1].reason == AMBIGUOUS_BOUNDARY_REASON


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
    ],
)
def test_statement_before_a_question_is_outside_it(post, statement, question):
    assert [post[a:b].strip() for a, b in question_spans(post)] == [question]
    assert validate_reply_plan(plan(statement), post) != []


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
        text = "x. " * n + "J. " * n + "Any tips?"
        started = time.perf_counter()
        question_spans(text)
        return time.perf_counter() - started

    elapsed(1000)  # warm up
    small, large = elapsed(2000), elapsed(16000)
    assert large < small * 8 * 4  # 8x the input; allow 4x noise over linear
