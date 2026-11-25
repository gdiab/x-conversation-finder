"""Models for written drafts (issue #12; ADR-0006 amendment 2026-11-24).

In written mode the model writes the reply as free text and code checks its
product claims against the product brief. These dataclasses carry that check
between activities, the Review workflow and the stored card.

Activity payloads use plain str values, not str-Enums (a str-Enum decodes as a
char list at the Temporal activity boundary). Every field added to a payload
has a default, so payloads recorded before a field existed still decode.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

DRAFT_MODE_ASSEMBLED = "assembled"
DRAFT_MODE_WRITTEN = "written"
DRAFT_MODES = (DRAFT_MODE_ASSEMBLED, DRAFT_MODE_WRITTEN)

# Sentence verdicts (SentenceCheck.verdict), decided by code:
VERDICT_SUPPORTED = "supported"  # a product claim; the checker cited brief sentences and code confirmed them
VERDICT_UNSUPPORTED = "unsupported"  # a product claim that failed the check
VERDICT_NOT_PRODUCT = "not_a_product_claim"  # no trigger fired and the checker saw no product statement
VERDICT_NOT_CHECKED = "not_checked"  # the checker was not asked (the draft had already failed in code)


@dataclass
class BriefSentence:
    """One sentence of the product brief, under the id the checker cites."""

    id: str  # "b1", "b2", ...: position in backend.gate.brief.split_brief
    text: str
    # The heading, or the lead-in line of a list ("Key features"), the sentence
    # stands under. A list item or a sentence under "Not supported yet" means
    # something else without it, so the checker and the card show both.
    context: str = ""


@dataclass
class TriggerHit:
    """A deterministic trigger that put a sentence in the checked set."""

    # product_name | first_person_plural | number | comparison | feature_term
    trigger: str
    match: str  # the matched text as it stands in the sentence


@dataclass
class SentenceCheck:
    """The check of one sentence of a written draft or a founder edit.

    This is the card's per-sentence record (amendment item 6): product claim
    or not, why it was checked, the verdict, and the supporting brief
    sentences. `sentence`, `passed`, `kind`, `reason` and `kept_segment` are
    the fields the assembled edit re-gate's results have too, so the approve
    validator and the card read both shapes the same way.
    """

    sentence: str
    passed: bool
    kind: str  # "claim" (checked as a product claim) or "non_claim"
    reason: str = ""  # why it failed; empty if passed
    kept_segment: bool = False  # assembled re-gate only; always False here
    triggers: List[TriggerHit] = field(default_factory=list)
    checker_flagged: bool = False  # the checker classified it as about the product
    verdict: str = VERDICT_NOT_CHECKED
    supporting: List[BriefSentence] = field(default_factory=list)  # cited brief sentences that exist


@dataclass
class DrafterHint:
    """The drafting model's own claim that brief sentences support one of its
    sentences. A hint for the checker, never a verdict (amendment item 1)."""

    sentence: str
    brief_sentence_ids: List[str] = field(default_factory=list)


@dataclass
class WrittenDraftInfo:
    """How a written draft was produced and checked (stored on the draft)."""

    product_name: str = ""
    product_aliases: List[str] = field(default_factory=list)
    feature_terms: List[str] = field(default_factory=list)  # read from the brief
    brief_sentence_count: int = 0
    splitter_version: Optional[int] = None  # the split the sentences were numbered under
    trigger_rules_version: str = ""
    drafter_hints: List[DrafterHint] = field(default_factory=list)
    checker_called: bool = False  # False when the draft failed in code before the paid call
    checker_model: str = ""
    checker_input_tokens: int = 0
    checker_output_tokens: int = 0


@dataclass
class ClaimVerdict:
    """What the checker returned for one sentence, after strict parsing.

    Anything the checker did not state clearly stays None or empty, and code
    reads that as "not supported" (it fails closed).
    """

    index: int  # 1-based position of the sentence in the request
    about_product: Optional[bool] = None
    verdict: Optional[str] = None  # "supported" | "unsupported" | None
    brief_sentence_ids: List[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class ClaimCheckRequest:
    """Everything the checker sees: the sentences, the brief sentences and the
    product name. No post, no plan, no drafting prompt (amendment item 3)."""

    sentences: List[str]
    brief_sentences: List[BriefSentence]
    product_name: str
    product_aliases: List[str] = field(default_factory=list)
    # Per sentence: True when a deterministic trigger fired, so the checker
    # must return a supported/unsupported verdict for it.
    must_verify: List[bool] = field(default_factory=list)
    # Per sentence: brief sentence ids the drafting model suggested (existing
    # ids only). A hint, never a verdict.
    suggested_ids: List[List[str]] = field(default_factory=list)


@dataclass
class ClaimCheckResponse:
    """The checker's verdicts and the call's provenance."""

    verdicts: List[ClaimVerdict] = field(default_factory=list)
    model_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class WrittenCheckResult:
    """Result of checking one written text (a model draft or a founder edit)."""

    passed: bool
    sentences: List[SentenceCheck] = field(default_factory=list)
    # (sentence or [marker], reason) pairs, the shape ClaimGateResult uses
    failures: List[List[str]] = field(default_factory=list)
    reasoning: str = ""
    # Reasons the text does not follow the reply plan's product decision
    plan_failures: List[str] = field(default_factory=list)
    info: WrittenDraftInfo = field(default_factory=WrittenDraftInfo)


def sentence_check_card_fields(check: Any) -> Dict[str, Any]:
    """Card/DB dict for one per-sentence result.

    One helper for the get_card query (ReviewCard.to_dict) and the stored card
    (store_activities), so the two views cannot drift. Accepts a SentenceCheck
    or an assembled re-gate result (which has no written-mode fields).
    """
    if isinstance(check, dict):
        return dict(check)
    if isinstance(check, SentenceCheck):
        return asdict(check)
    return {
        "sentence": check.sentence,
        "passed": check.passed,
        "kind": check.kind,
        "reason": check.reason,
        "kept_segment": check.kept_segment,
        "source_passage_id": getattr(check, "source_passage_id", None),
        "sentence_index": getattr(check, "sentence_index", None),
        "phrase_id": getattr(check, "phrase_id", None),
    }


def written_draft_card_fields(draft: Any) -> Dict[str, Any]:
    """Card/DB fields a draft carries for written mode, merged into its dict."""
    info = getattr(draft, "written", None)
    return {
        "mode": getattr(draft, "mode", None) or DRAFT_MODE_ASSEMBLED,
        "sentence_checks": [
            sentence_check_card_fields(c) for c in (getattr(draft, "sentence_checks", None) or [])
        ],
        "written": asdict(info) if info is not None else None,
    }


def draft_mode_card_fields(evidence: Any) -> Dict[str, Any]:
    """Card/DB fields for the Review's draft mode, merged into the evidence dict."""
    return {
        "draft_mode": getattr(evidence, "draft_mode", None),
        "draft_mode_note": getattr(evidence, "draft_mode_note", None),
    }
