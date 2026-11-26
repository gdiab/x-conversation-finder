"""Storage activities for review cards using SQLite."""

from dataclasses import asdict, dataclass
from typing import Optional, Dict, Any, List, Union
from temporalio import activity
from temporalio.exceptions import ApplicationError
import asyncio
from sqlalchemy import text as sql_text
from sqlalchemy.orm.attributes import flag_modified

from backend.models import ReviewCard, Evidence
from backend.models.reply_plan import reply_plan_card_fields
from backend.models.written import (
    DRAFT_MODE_WRITTEN,
    draft_mode_card_fields,
    sentence_check_card_fields,
    written_draft_card_fields,
)

# Placeholder for post text removed under X's removal duty (ADR-0006); the
# Review workflow's get_card query uses the same value.
POST_TEXT_REMOVED = "[removed - post deleted]"

# Issue #9: skip reason for a pending Review whose target post returned 404 on
# the re-fetch at approve time. The frontend shows "Post removed" for it.
# issue #23: delete_post_text could not truncate the WAL yet; retried
WAL_CHECKPOINT_PENDING_ERROR = "WalCheckpointPending"

POST_REMOVED_SKIP_REASON = "Post removed: the target post was deleted or is no longer visible on X"


def _redact_material(material: Dict[str, Any], passage_unverified: bool = False) -> Dict[str, Any]:
    """A founder material without its model-written relevance note and source
    label, and without its passage when that was never checked against the
    brief (issue #9)."""
    redacted = dict(material)
    if passage_unverified and redacted.get("passage"):
        redacted["passage"] = POST_TEXT_REMOVED
    if redacted.get("relevance"):
        redacted["relevance"] = POST_TEXT_REMOVED
    if redacted.get("source") and redacted["source"] != "product_brief":
        redacted["source"] = POST_TEXT_REMOVED
    return redacted


# Issue #9: gate-derived text the Review workflow builds from model output.
# Cards stored before the gate stopped echoing model identifiers can carry a
# post quote in these, so a removal redacts them by provenance (this prefix
# and the [marker] failure rows are code-written; founder text is kept).
GATE_SKIP_REASON_PREFIX = "Not enough evidence:"  # review.py _draft_and_validate
GATE_STEER_PREFIX = "Previous draft failed claim gate."  # review.py _draft_and_validate
GATE_DETAILS_REMOVED = "details removed with the post"
MANUAL_EDIT_STEER = "manual_edit"  # review.py appends it to steers_applied on a founder edit


def redact_skip_reason(reason: Optional[str]) -> Optional[str]:
    """A skip reason without gate details built from model output (issue #9)."""
    if isinstance(reason, str) and reason.startswith(GATE_SKIP_REASON_PREFIX):
        return f"{GATE_SKIP_REASON_PREFIX} {GATE_DETAILS_REMOVED}"
    return reason


def _redact_segment(segment: Dict[str, Any], passages: Dict[str, str]) -> Dict[str, Any]:
    """A stored segment keeping only known identifiers and code-inserted text:
    a listed phrase's text, or a claim sentence found verbatim in its passage."""
    from backend.gate.non_claim_set import UNKNOWN_IDENTIFIER, get_phrase_text, known_phrase_id

    redacted = dict(segment)
    if redacted.get("kind") not in (None, "claim", "non_claim"):
        redacted["kind"] = UNKNOWN_IDENTIFIER
    if redacted.get("phrase_id"):
        redacted["phrase_id"] = known_phrase_id(redacted["phrase_id"])
    source = redacted.get("source_passage_id")
    if source and source not in passages:
        redacted["source_passage_id"] = UNKNOWN_IDENTIFIER
    text = redacted.get("text")
    if text:
        core = text.strip().rstrip(".!?")
        phrase = get_phrase_text(redacted.get("phrase_id") or "") or ""
        passage = passages.get(redacted.get("source_passage_id") or "", "")
        inserted_by_code = (
            (redacted.get("kind") == "non_claim" and phrase and core == phrase.strip().rstrip(".!?"))
            or (redacted.get("kind") == "claim" and core and core in passage)
        )
        if not inserted_by_code:
            redacted["text"] = None
    # On older rejected segments the index is unchecked model output (it
    # could be a number copied from the post): keep it only for a claim whose
    # text is exactly that sentence of its passage.
    if not _index_matches_text(redacted, passages):
        redacted["sentence_index"] = None
    return redacted


def _index_matches_text(segment: Dict[str, Any], passages: Dict[str, str]) -> bool:
    """Whether a claim segment's sentence_index names the sentence it holds."""
    from backend.activities.claim_gate_activity import split_into_sentences

    index, text = segment.get("sentence_index"), segment.get("text")
    passage = passages.get(segment.get("source_passage_id") or "")
    if segment.get("kind") != "claim" or not isinstance(index, int) or isinstance(index, bool):
        return False
    if not text or not passage:
        return False
    sentences = split_into_sentences(passage)
    if not 0 <= index < len(sentences):
        return False
    return sentences[index].strip().rstrip(".!?") == text.strip().rstrip(".!?")


# Issue #12: the values code writes into a sentence check. On a removal,
# anything else found in these fields is replaced.
_KNOWN_TRIGGERS = ("product_name", "first_person_plural", "number", "comparison", "feature_term")
_KNOWN_VERDICTS = ("supported", "unsupported", "not_a_product_claim", "not_checked")


def redact_sentence_check(check: Any) -> Any:
    """A written-mode sentence check without the text a model wrote (issue #12).

    The sentence is the drafting model's free text (or a founder edit of it)
    and can quote the post; the reason is the checker's or code's comment on
    that sentence and can echo it; a trigger's match is a piece of the
    sentence. Kept: the verdict, which triggers fired, and the supporting
    brief sentences, which are the founder's own text looked up by code.
    """
    from backend.gate.non_claim_set import UNKNOWN_IDENTIFIER

    if not isinstance(check, dict):
        return check
    redacted = dict(check)
    if redacted.get("sentence"):
        redacted["sentence"] = POST_TEXT_REMOVED
    if redacted.get("reason"):
        redacted["reason"] = GATE_DETAILS_REMOVED
    if redacted.get("kind") not in (None, "claim", "non_claim"):
        redacted["kind"] = UNKNOWN_IDENTIFIER
    if redacted.get("verdict") not in (None, *_KNOWN_VERDICTS):
        redacted["verdict"] = UNKNOWN_IDENTIFIER
    if isinstance(redacted.get("triggers"), list):
        redacted["triggers"] = [
            {
                "trigger": t.get("trigger") if t.get("trigger") in _KNOWN_TRIGGERS else UNKNOWN_IDENTIFIER,
                "match": "",
            }
            for t in redacted["triggers"]
            if isinstance(t, dict)
        ]
    return redacted


def is_written_evidence(evidence_data: Any) -> bool:
    """Whether a card's evidence dict belongs to a written-mode Review (issue #12)."""
    if not isinstance(evidence_data, dict):
        return False
    draft = evidence_data.get("draft")
    return evidence_data.get("draft_mode") == DRAFT_MODE_WRITTEN or (
        isinstance(draft, dict) and draft.get("mode") == DRAFT_MODE_WRITTEN
    )


def redact_edit_results(results: Any, evidence_data: Any) -> Any:
    """A card's edited-text check results after a removal (issue #12).

    In written mode an edit starts from text a model wrote after reading the
    post, so its sentences are redacted like the draft's. In assembled mode
    the edited sentences are the founder's own words and stay, as before.
    """
    if not is_written_evidence(evidence_data) or not isinstance(results, list):
        return results
    return [redact_sentence_check(r) for r in results]


def redact_failing_sentences(sentences: Any, evidence_data: Any) -> Any:
    """A card's post_anyway failing sentences after a removal; see redact_edit_results."""
    if not is_written_evidence(evidence_data) or not isinstance(sentences, list):
        return sentences
    return [POST_TEXT_REMOVED for _ in sentences]


def _redact_draft(draft: Dict[str, Any], materials: Any) -> Dict[str, Any]:
    """A stored draft without model-derived gate details (issue #9).

    The assembled draft text and segments inserted by code are kept; free
    text or unknown identifiers in segments and gate-generated steers are
    removed, and the claim-gate details are dropped as a whole (older cards
    can hold echoed model values in any of them). Founder nudges and edits
    are the founder's own text and stay.

    A written draft (issue #12) is different: the model wrote its text after
    reading the post, so the text goes, with each sentence check's sentence
    and reason and the drafter's hints. A founder edit of a written draft
    goes too: it starts from the model's text. So do the founder's nudges on
    it, which are written while looking at that text.
    """
    passages = {
        m.get("passage_id"): m.get("passage") or ""
        for m in (materials if isinstance(materials, list) else [])
        if isinstance(m, dict) and m.get("passage_id")
    }
    redacted = dict(draft)
    if isinstance(redacted.get("segments"), list):
        redacted["segments"] = [
            _redact_segment(s, passages) if isinstance(s, dict) else s for s in redacted["segments"]
        ]
    if isinstance(redacted.get("steers_applied"), list):
        redacted["steers_applied"] = [
            f"{GATE_STEER_PREFIX} {GATE_DETAILS_REMOVED}"
            if isinstance(s, str) and s.startswith(GATE_STEER_PREFIX) else s
            for s in redacted["steers_applied"]
        ]
    gate = redacted.get("claim_gate")
    if isinstance(gate, dict):
        redacted["claim_gate"] = {
            "passed": gate.get("passed"),
            "segments": [],
            "failed_sentences": [],
            "reasoning": f"Claim gate {GATE_DETAILS_REMOVED}",
            "claims": [],
            "failed_claims": [],
        }
    if redacted.get("mode") == DRAFT_MODE_WRITTEN:
        if redacted.get("text"):
            redacted["text"] = POST_TEXT_REMOVED
        if isinstance(redacted.get("steers_applied"), list):
            # A nudge on a written draft is written while looking at model text
            # that can quote the post ("drop the part about ..."), so it goes
            # too. The "manual_edit" marker and the gate's own redacted steer
            # are code-written and stay.
            redacted["steers_applied"] = [
                s if s == MANUAL_EDIT_STEER or (isinstance(s, str) and s.startswith(GATE_STEER_PREFIX))
                else POST_TEXT_REMOVED
                for s in redacted["steers_applied"]
            ]
        if isinstance(redacted.get("sentence_checks"), list):
            redacted["sentence_checks"] = [redact_sentence_check(c) for c in redacted["sentence_checks"]]
        if isinstance(redacted.get("written"), dict):
            redacted["written"] = {**redacted["written"], "drafter_hints": []}
    return redacted


def redact_post_text_copies(evidence_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a copy of a card's evidence dict with every copy of the post text removed.

    Used for the stored card (delete_post_text) and for the Review workflow's
    get_card query, so both views redact the same fields. Every free-text
    field a model wrote after reading the post can quote it:
    - why_it_fits, and each founder material's relevance note;
    - issue #9: matched_topic and each founder material's source label, which
      the research model also writes freely (the default source
      "product_brief" is kept);
    - issue #4: reply_fit.brief_problem_span and reply_fit.rejected_span;
    - issue #5: reply_plan.target_point, reply_plan.mention_product_reason,
      and reply_plan.reply_type when it is not one of the known values;
    - issue #9: the draft's model-derived gate details (_redact_draft) and a
      gate-failure skip reason (redact_skip_reason);
    - issue #12: a written draft's text, its sentence checks' sentences and
      reasons, and the drafter's hints (_redact_draft). The card's edited-text
      results live outside the evidence: redact_removed_card covers them.
    Pure and deterministic (the workflow calls it from a query).
    """
    from backend.models.reply_plan import REPLY_TYPES

    evidence_data = dict(evidence_data or {})
    for key in ("why_it_fits", "matched_topic"):
        if evidence_data.get(key):
            evidence_data[key] = POST_TEXT_REMOVED
    # Issue #4 checks each founder passage against the brief and records
    # reply_fit. A card without reply_fit predates that check, so its
    # passages are model output too (they could copy the post) and go, along
    # with the draft text assembled from them.
    passages_unverified = not evidence_data.get("reply_fit")
    materials = evidence_data.get("founder_materials")
    if isinstance(materials, list):
        evidence_data["founder_materials"] = [
            _redact_material(m, passages_unverified) if isinstance(m, dict) else m for m in materials
        ]
        materials = evidence_data["founder_materials"]
    reply_fit = evidence_data.get("reply_fit")
    if isinstance(reply_fit, dict):
        evidence_data["reply_fit"] = {
            **reply_fit,
            **{
                key: POST_TEXT_REMOVED
                for key in ("brief_problem_span", "rejected_span")
                if reply_fit.get(key)
            },
        }
    reply_plan = evidence_data.get("reply_plan")
    if isinstance(reply_plan, dict):
        redacted = dict(reply_plan)
        for key in ("target_point", "mention_product_reason"):
            if redacted.get(key):
                redacted[key] = POST_TEXT_REMOVED
        if redacted.get("reply_type") and redacted["reply_type"] not in REPLY_TYPES:
            redacted["reply_type"] = POST_TEXT_REMOVED
        evidence_data["reply_plan"] = redacted
    if isinstance(evidence_data.get("draft"), dict):
        draft = _redact_draft(evidence_data["draft"], evidence_data.get("founder_materials"))
        if passages_unverified and draft.get("text"):
            draft["text"] = POST_TEXT_REMOVED
        evidence_data["draft"] = draft
    if evidence_data.get("skip_reason"):
        evidence_data["skip_reason"] = redact_skip_reason(evidence_data["skip_reason"])
    return evidence_data


def redact_removed_card(card: Dict[str, Any]) -> Dict[str, Any]:
    """A card dict (the get_card query's, or a stored card's) after a removal.

    The one place that lists every card field a removal touches: the post
    text, the evidence copies (redact_post_text_copies), the skip reason and,
    in written mode (issue #12), the edited-text results and the post_anyway
    failing sentences. Pure and deterministic (the workflow calls it from a
    query).
    """
    evidence = card.get("evidence")
    redacted = {
        **card,
        "post": {**(card.get("post") or {}), "text": POST_TEXT_REMOVED, "text_removed": True},
        "evidence": redact_post_text_copies(evidence),
        "skip_reason": redact_skip_reason(card.get("skip_reason")),
    }
    for key, redact in (
        ("edited_text_gate_results", redact_edit_results),
        ("post_anyway_failing_sentences", redact_failing_sentences),
    ):
        if key in card:
            redacted[key] = redact(card[key], evidence)
    return redacted
from backend.database import get_db, ReviewCardDB
from backend.review_docs import text_free_activity as _text_free_activity


@dataclass
class CardRef:
    """A card for store_card to compose from the database (issue #23).

    The workflow names the card's state; store_card reads the stored post and
    the evidence in evidence_slot, so neither enters the Review's history.
    """

    post_id: str
    evidence_slot: str
    status: str
    created_at: str
    updated_at: str
    # A reason without post text (a rule, the threshold, the plan's code-written
    # errors, the founder's own skip reason, "post removed")
    skip_reason: Optional[str] = None
    # Build the reason from the stored failures of the draft in this slot
    # (the gate-failure skip: "Not enough evidence: ...")
    skip_reason_from_slot: str = ""
    # Also write the reason on the evidence (the research and plan skips did)
    evidence_skip_reason: bool = False
    # Drop the draft (a removal recorded while drafting: it can quote the post)
    drop_draft: bool = False
    # Make evidence_slot the Review's current evidence (the accepted draft)
    make_current: bool = False


@dataclass
class StoreCardInput:
    """Input for store_card activity."""
    
    # None with `ref` set (issue #23)
    card: Optional[ReviewCard]
    idempotency_key: str
    ref: Optional[CardRef] = None


@dataclass
class UpdateCardInput:
    """
    B1: Input for update_card activity with typed fields to fix bool payload decoding.
    
    Replaces dict-based updates with explicit typed fields so Temporal can properly
    serialize/deserialize all values, including bools.
    
    QA R13: Added edit_seq to prevent stale card writes.
    """
    workflow_id: str
    idempotency_key: str
    # Typed update fields
    status: Optional[str] = None
    approved_by: Optional[str] = None
    posted_url: Optional[str] = None
    reply_post_id: Optional[str] = None
    skip_reason: Optional[str] = None
    followup_replies: Optional[List[Dict[str, Any]]] = None
    evidence: Optional[Evidence] = None
    edited_text_gate_results: Optional[List[Dict[str, Any]]] = None
    edited_text_gate_state: Optional[str] = None  # QA R11 B2/C: "passed", "failed", or "not_checked"
    post_anyway: Optional[bool] = None
    post_anyway_failing_sentences: Optional[List[str]] = None
    edit_seq: Optional[int] = None  # QA R13: Monotonic edit sequence
    # issue #5: a gated nudge replaced the draft; drop the old edit's gate results
    clear_edited_text_gate: Optional[bool] = None
    # --- Issue #23: text out of history. The fields below name Review
    # documents (backend/review_docs.py) instead of carrying their text. ---
    post_id: str = ""
    # Write the draft of the evidence stored in this slot to the card, and
    # make it the Review's current evidence (a nudged redraft)
    evidence_slot: str = ""
    # A founder edit: its text (slot), and its check results: the assembled
    # re-gate's (edit_results_slot) or the written check's (edit_check_slot).
    # Neither for a check that could not run ("not_checked"). The edit
    # becomes the current evidence's draft text, as the workflow did.
    edit_text_slot: str = ""
    edit_results_slot: str = ""
    edit_check_slot: str = ""
    # post_anyway (approve): record the failing sentences of the stored check
    # results the founder acknowledged, by slot (the assembled re-gate's, or
    # the written check's); an approve after a "not_checked" edit names none
    post_anyway_results_slot: str = ""
    post_anyway_check_slot: str = ""
    # A write that has no document of its own (check_followup's replies):
    # dropped when a later attempt already wrote it (review_docs.claim_write)
    writer_slot: str = ""


@dataclass
class GetCardInput:
    """Input for get_card activity."""
    
    workflow_id: str


@dataclass
class DeletePostTextInput:
    """Input for delete_post_text activity (B8 - 404 handling)."""
    
    workflow_id: str
    idempotency_key: str
    # issue #9: the removed post's id; when set, every stored card of this
    # post (e.g. under another account) loses its copy of the text too
    post_id: Optional[str] = None


def _card_to_db(card: ReviewCard) -> ReviewCardDB:
    """Convert ReviewCard to database model."""
    # Handle status - it might be an enum or a string due to serialization
    if isinstance(card.status, str):
        status_str = card.status
    elif hasattr(card.status, 'value'):
        status_str = card.status.value
    else:
        status_str = str(card.status)
    
    return ReviewCardDB(
        workflow_id=card.workflow_id,
        post_data={
            "id": card.post.id,
            "text": card.post.text,
            "author": {
                "id": card.post.author.id,
                "username": card.post.author.username,
                "name": card.post.author.name,
                "followers_count": card.post.author.followers_count,
                "verified": card.post.author.verified,
            },
            "created_at": card.post.created_at,
            "like_count": card.post.like_count,
            "reply_count": card.post.reply_count,
            "repost_count": card.post.repost_count,
            "quote_count": card.post.quote_count,
            "conversation_id": card.post.conversation_id,
            "in_reply_to_user_id": card.post.in_reply_to_user_id,
            "reply_settings": card.post.reply_settings,
            "url": card.post.url,
        },
        evidence_data={
            "post_id": card.evidence.post_id,
            "matched_topic": card.evidence.matched_topic,
            "why_it_fits": card.evidence.why_it_fits,
            "confidence_score": card.evidence.confidence_score,
            "confidence_inputs": card.evidence.confidence_inputs,
            "founder_materials": [
                {
                    "source": m.source,
                    "passage": m.passage,
                    "relevance": m.relevance,
                    "passage_id": m.passage_id,
                }
                for m in card.evidence.founder_materials
            ],
            "draft": {
                "text": card.evidence.draft.text,
                "segments": [
                    {
                        "kind": s.kind,
                        "text": s.text,
                        "source_passage_id": s.source_passage_id,
                        "sentence_index": s.sentence_index,
                        "phrase_id": s.phrase_id,  # issue #9: removal keeps valid phrase segments
                    }
                    for s in card.evidence.draft.segments
                ],
                "claim_gate": {
                    "passed": card.evidence.draft.claim_gate.passed,
                    "segments": [
                        {
                            "kind": s.kind,
                            "text": s.text,
                            "source_passage_id": s.source_passage_id,
                            "sentence_index": s.sentence_index,
                            "phrase_id": s.phrase_id,  # issue #9: removal keeps valid phrase segments
                        }
                        for s in card.evidence.draft.claim_gate.segments
                    ],
                    "failed_sentences": [
                        {
                            "sentence": sentence,
                            "reason": reason,
                        }
                        for sentence, reason in card.evidence.draft.claim_gate.failed_sentences
                    ],
                    "reasoning": card.evidence.draft.claim_gate.reasoning,
                    # Legacy fields for backwards compatibility
                    "claims": [
                        {
                            "text": c.text,
                            "cited_passage_ids": c.cited_passage_ids,
                            "quoted_spans": c.quoted_spans,
                        }
                        for c in card.evidence.draft.claim_gate.claims
                    ],
                    "failed_claims": [
                        {
                            "claim": {
                                "text": c.text,
                                "cited_passage_ids": c.cited_passage_ids,
                                "quoted_spans": c.quoted_spans,
                            },
                            "reason": reason,
                        }
                        for c, reason in card.evidence.draft.claim_gate.failed_claims
                    ],
                },
                "steers_applied": card.evidence.draft.steers_applied,
                "token_count": card.evidence.draft.token_count,
                "model_used": card.evidence.draft.model_used,
                "input_tokens": card.evidence.draft.input_tokens,
                "output_tokens": card.evidence.draft.output_tokens,
                "plan_conformance_failures": list(card.evidence.draft.plan_conformance_failures),  # issue #5
                **written_draft_card_fields(card.evidence.draft),  # issue #12
            } if card.evidence.draft else None,
            "skip_reason": card.evidence.skip_reason,
            # Issue #4: should-we-reply features and checks
            "reply_fit": asdict(card.evidence.reply_fit) if card.evidence.reply_fit else None,
            **reply_plan_card_fields(card.evidence),  # issue #5
            **draft_mode_card_fields(card.evidence),  # issue #12
        },
        status=status_str,
        created_at=card.created_at,
        updated_at=card.updated_at,
        approved_by=card.approved_by,
        posted_url=card.posted_url,
        reply_post_id=card.reply_post_id,
        skip_reason=card.skip_reason,
        followup_replies=card.followup_replies if card.followup_replies else None,
        # Edit re-gate fields (ADR-0006 Amendment E)
        edited_text_gate_results=[
            sentence_check_card_fields(r) for r in card.edited_text_gate_results
        ] if card.edited_text_gate_results else None,
        post_anyway=card.post_anyway,
        post_anyway_failing_sentences=card.post_anyway_failing_sentences,
    )


def _post_removal_recorded(db, post_id: str) -> bool:
    """Whether any stored card of this post records a removal (issue #9)."""
    return db.query(ReviewCardDB.workflow_id).filter(
        ReviewCardDB.post_data["id"].as_string() == post_id,
        ReviewCardDB.post_data["text_removed"].as_boolean() == True,  # noqa: E712
    ).first() is not None


def post_removal_recorded(post_id: str) -> bool:
    """Whether the post's removal is recorded: on its stored post, or on any
    stored card of it (this Review's own card, or the card of another
    account's Review of the same post). Asked before each model call that is
    sent the post or text written from it, and before approve's X re-fetch
    (issues #12, #22)."""
    from backend import review_docs

    return review_docs.post_removed(post_id)


def card_post_text_removed(workflow_id: str) -> bool:
    """Whether this workflow's stored card records a removal (issue #9)."""
    card = _get_card_sync(GetCardInput(workflow_id=workflow_id))
    return bool(card and card["post"].get("text_removed"))


def followup_reply_fields(post) -> Dict[str, Any]:
    """A follow-up reply as the card stores it."""
    return {
        "id": post.id,
        "text": post.text,
        "author": post.author.username,
        "url": post.url,
    }


def _compose_card(workflow_id: str, ref: CardRef) -> ReviewCard:
    """The card a CardRef names (issue #23), read from the database.

    After a removal the evidence documents are gone: the card then holds a
    minimal evidence record, and the stored post's text is already replaced.
    """
    from backend import review_docs
    from backend.gate.failures import DraftFailures, gate_skip_reason
    from backend.models import Post

    db = get_db()
    try:
        post_data = review_docs.stored_post_data(db, ref.post_id)
    finally:
        db.close()
    if post_data is None:
        raise ApplicationError(
            f"Post {ref.post_id} is not stored", type=review_docs.DOC_MISSING_ERROR_TYPE, non_retryable=True
        )
    post_data.pop("text_removed", None)
    post = review_docs.from_json(post_data, Post)
    evidence = review_docs.find_doc(workflow_id, ref.post_id, ref.evidence_slot, Evidence)
    if evidence is None:
        if not review_docs.post_removed(ref.post_id):
            raise ApplicationError(
                f"Review document {ref.evidence_slot!r} of {workflow_id} is missing",
                type=review_docs.DOC_MISSING_ERROR_TYPE,
                non_retryable=True,
            )
        evidence = Evidence(
            post_id=ref.post_id,
            matched_topic="",
            why_it_fits="",
            confidence_score=0.0,
            confidence_inputs={},
            founder_materials=[],
        )
    if ref.drop_draft:
        evidence.draft = None
    skip_reason = ref.skip_reason
    if ref.skip_reason_from_slot:
        failures = review_docs.find_doc(
            workflow_id, ref.post_id, review_docs.failures_slot(ref.skip_reason_from_slot), DraftFailures
        )
        skip_reason = gate_skip_reason(failures.rows if failures else [])
    if ref.evidence_skip_reason:
        evidence.skip_reason = skip_reason
    return ReviewCard(
        workflow_id=workflow_id,
        post=post,
        evidence=evidence,
        status=ref.status,
        created_at=ref.created_at,
        updated_at=ref.updated_at,
        skip_reason=skip_reason if ref.status == "skipped" else None,
    )


def _store_card_sync(input: StoreCardInput, make_current: bool = False) -> bool:
    """Synchronous DB work for store_card (B3).

    make_current (issue #23): also make the card's evidence the Review's
    current evidence document, in the same transaction. A late attempt (its
    retry already stored the card, and an edit or nudge has since written
    the current evidence) writes nothing.
    """
    from backend import review_docs

    with review_docs.text_safe_db_errors():
        return _store_card_in_transaction(input, make_current)


def _store_card_in_transaction(input: StoreCardInput, make_current: bool) -> bool:
    from backend import review_docs

    db = get_db()
    try:
        # issue #9: hold SQLite's write lock from the removal check to the
        # commit (as backend/budget.py does), so a removal cannot interleave
        # with this write, e.g. a timed-out attempt's thread still running
        db.execute(sql_text("BEGIN IMMEDIATE"))
        # issue #23: a run replaced by a later run of the workflow id writes nothing
        if review_docs.run_superseded(db, input.card.workflow_id):
            db.commit()
            return True
        if make_current:
            post_id = input.card.post.id
            if review_docs.doc_written_later(db, input.card.workflow_id, review_docs.EVIDENCE_SLOT):
                db.commit()
                return True
            review_docs.save_doc_in(db, input.card.workflow_id, post_id, review_docs.EVIDENCE_SLOT, input.card.evidence)
        card_db = _card_to_db(input.card)
        
        # issue #9: a removal already recorded for this post (by another
        # account's Review, or this card's own earlier removal) also applies to
        # this write, so a late or retried store cannot put the text back.
        # issue #22: a removal recorded on the stored post counts too
        if review_docs._post_removed(db, card_db.post_data["id"]):
            _remove_card_post_text(card_db)
            # A late store (a timed-out attempt finishing after the removal)
            # must not revert the removed card's later status either
            existing = db.get(ReviewCardDB, card_db.workflow_id)
            if existing is not None and (existing.post_data or {}).get("text_removed"):
                card_db.status = existing.status
                card_db.skip_reason = existing.skip_reason
        
        # Use INSERT...ON CONFLICT (B3)
        # SQLAlchemy's merge() provides upsert-like behavior
        db.merge(card_db)
        db.commit()
        return True
    finally:
        db.close()


@activity.defn(name="store_card")
@_text_free_activity(lambda input: input.card is None)
async def store_card(input: StoreCardInput) -> bool:
    """
    Store a review card in SQLite.
    
    Idempotent: upsert by workflow_id using merge (INSERT...ON CONFLICT).
    B3: Runs sync DB work via asyncio.to_thread.
    """
    workflow_id = input.card.workflow_id if input.card else activity.info().workflow_id
    activity.logger.info(f"Storing card {workflow_id} (key: {input.idempotency_key})")
    
    from backend.review_docs import text_safe_db_errors

    make_current = False
    with text_safe_db_errors():  # issue #23: a failed write's parameters hold the text
        if input.card is None:
            # issue #23: compose here, where activity.info() is available
            card = await asyncio.to_thread(_compose_card, workflow_id, input.ref)
            make_current = input.ref.make_current and input.ref.evidence_slot != "evidence"
            input = StoreCardInput(card=card, idempotency_key=input.idempotency_key)
    # B3: Run sync DB work in thread pool
    result = await asyncio.to_thread(_store_card_sync, input, make_current)
    
    activity.logger.info(f"Stored card {workflow_id}")
    return result


def _update_card_sync(input: UpdateCardInput) -> bool:
    """update_card's DB work; database errors without data (issue #23: the
    follow-up replies check_followup writes go through here too)."""
    from backend import review_docs

    with review_docs.text_safe_db_errors():
        return _update_card_in_transaction(input)


def _update_card_in_transaction(input: UpdateCardInput) -> bool:
    """
    Synchronous DB work for update_card. B1: Works with typed fields instead of dict.
    
    QA R13: Checks edit_seq and ignores writes with lower seq than stored.
    Issue #9: on a card that records a removal, the written evidence is
    redacted again, so a late write (an in-flight redraft, or another
    Review's stale evidence) cannot restore copies of the post.
    """
    db = get_db()
    try:
        db.execute(sql_text("BEGIN IMMEDIATE"))  # issue #9, see _store_card_sync
        card_db = db.query(ReviewCardDB).filter(
            ReviewCardDB.workflow_id == input.workflow_id
        ).first()
        
        if not card_db:
            return False
        
        # issue #23: the text-free fields read Review documents in this
        # transaction, so a stale edit (below) writes neither the card nor them
        evidence_value = input.evidence
        workflow_id = input.workflow_id
        # issue #23: a run replaced by a later run of the workflow id writes nothing
        from backend import review_docs as _docs

        if _docs.run_superseded(db, workflow_id):
            return True
        if input.writer_slot:
            from backend import review_docs

            post_id = (card_db.post_data or {}).get("id") or ""
            if not review_docs.claim_write(db, input.workflow_id, post_id, input.writer_slot):
                return True
        # issue #23: a write naming Review documents is dropped as a whole
        # when it comes from a late attempt: a retry of an edit write that
        # already committed (same edit_seq: the first one wrote it all), or a
        # write the current evidence has moved past (a later edit or nudge)
        if input.edit_text_slot and input.edit_seq is not None and input.edit_seq == (card_db.edit_seq or 0):
            return True
        if (input.evidence_slot or input.edit_text_slot):
            from backend import review_docs

            if review_docs.doc_written_later(db, input.workflow_id, review_docs.EVIDENCE_SLOT):
                return True
        # QA R13: Check edit_seq - ignore stale writes
        if input.edit_seq is not None:
            stored_edit_seq = card_db.edit_seq or 0
            if input.edit_seq < stored_edit_seq:
                # Stale write - ignore it
                return True  # Return True to indicate operation was "successful" (idempotent)
            # Update the stored edit_seq
            card_db.edit_seq = input.edit_seq
        
        # B1: Apply updates from typed fields
        if input.status is not None:
            card_db.status = input.status
        if input.approved_by is not None:
            card_db.approved_by = input.approved_by
        if input.posted_url is not None:
            card_db.posted_url = input.posted_url
        if input.reply_post_id is not None:
            card_db.reply_post_id = input.reply_post_id
        if input.skip_reason is not None:
            card_db.skip_reason = input.skip_reason
        if input.followup_replies is not None:
            card_db.followup_replies = input.followup_replies
            flag_modified(card_db, "followup_replies")  # QA R13: flag_modified for JSON
        if input.clear_edited_text_gate:
            card_db.edited_text_gate_results = None
            card_db.edited_text_gate_state = None
        if input.edited_text_gate_results is not None:
            card_db.edited_text_gate_results = input.edited_text_gate_results
            flag_modified(card_db, "edited_text_gate_results")  # QA R13: flag_modified for JSON
        if input.edited_text_gate_state is not None:
            card_db.edited_text_gate_state = input.edited_text_gate_state
        if input.post_anyway is not None:
            # B1: Direct assignment of bool - Temporal now handles it correctly
            card_db.post_anyway = input.post_anyway
        if input.post_anyway_failing_sentences is not None:
            card_db.post_anyway_failing_sentences = input.post_anyway_failing_sentences
            flag_modified(card_db, "post_anyway_failing_sentences")  # QA R13: flag_modified for JSON
        
        # issue #23: documents named instead of carried
        if input.evidence_slot or input.edit_text_slot:
            evidence_value = _apply_text_free_update(db, card_db, input)
        if input.post_anyway_results_slot or input.post_anyway_check_slot:
            card_db.post_anyway_failing_sentences = stored_failing_sentences_in(
                db, input.workflow_id, input.post_id,
                results_slot=input.post_anyway_results_slot, check_slot=input.post_anyway_check_slot,
            )
            flag_modified(card_db, "post_anyway_failing_sentences")
        
        # B2: Update evidence_data JSON with flag_modified
        if evidence_value is not None:
            value = evidence_value
            evidence_data = card_db.evidence_data.copy()  # Make a new dict
            if hasattr(value, 'draft') and value.draft:
                evidence_data["draft"] = {
                    "text": value.draft.text,
                    "segments": [
                        {
                            "kind": s.kind,
                            "text": s.text,
                            "source_passage_id": s.source_passage_id,
                            "sentence_index": s.sentence_index,
                            "phrase_id": s.phrase_id,  # issue #9: removal keeps valid phrase segments
                        }
                        for s in value.draft.segments
                    ],
                    "claim_gate": {
                        "passed": value.draft.claim_gate.passed,
                        "segments": [
                            {
                                "kind": s.kind,
                                "text": s.text,
                                "source_passage_id": s.source_passage_id,
                                "sentence_index": s.sentence_index,
                                "phrase_id": s.phrase_id,  # issue #9: removal keeps valid phrase segments
                            }
                            for s in value.draft.claim_gate.segments
                        ],
                        "failed_sentences": [
                            {
                                "sentence": sentence,
                                "reason": reason,
                            }
                            for sentence, reason in value.draft.claim_gate.failed_sentences
                        ],
                        "reasoning": value.draft.claim_gate.reasoning,
                        # Legacy fields
                        "claims": [
                            {
                                "text": c.text,
                                "cited_passage_ids": c.cited_passage_ids,
                                "quoted_spans": c.quoted_spans,
                            }
                            for c in value.draft.claim_gate.claims
                        ],
                        "failed_claims": [
                            {
                                "claim": {
                                    "text": c.text,
                                    "cited_passage_ids": c.cited_passage_ids,
                                    "quoted_spans": c.quoted_spans,
                                },
                                "reason": reason,
                            }
                            for c, reason in value.draft.claim_gate.failed_claims
                        ],
                    },
                    "steers_applied": value.draft.steers_applied,
                    "token_count": value.draft.token_count,
                    "model_used": value.draft.model_used,
                    "input_tokens": value.draft.input_tokens,
                    "output_tokens": value.draft.output_tokens,
                    "plan_conformance_failures": list(value.draft.plan_conformance_failures),  # issue #5
                    **written_draft_card_fields(value.draft),  # issue #12
                }
            card_db.evidence_data = evidence_data
            flag_modified(card_db, "evidence_data")
        
        if (card_db.post_data or {}).get("text_removed"):
            _redact_removed_card_db(card_db)
        
        db.commit()
        return True
    finally:
        db.close()


@activity.defn(name="update_card")
@_text_free_activity(lambda input: bool(input.post_id or input.writer_slot))
async def update_card(input: UpdateCardInput) -> bool:
    """
    Update a review card.
    
    Idempotent: updates are applied by field.
    B3: Runs sync DB work via asyncio.to_thread.
    """
    activity.logger.info(
        f"Updating card {input.workflow_id} (key: {input.idempotency_key})"
    )
    
    from backend.review_docs import text_safe_db_errors

    # B3: Run sync DB work in thread pool (issue #23: errors without data)
    with text_safe_db_errors():
        result = await asyncio.to_thread(_update_card_sync, input)
    
    if not result:
        activity.logger.error(f"Card {input.workflow_id} not found")
    else:
        activity.logger.info(f"Updated card {input.workflow_id}")
    
    return result


def stored_failing_sentences_in(db, workflow_id: str, post_id: str, results_slot: str = "", check_slot: str = "") -> List[str]:
    """The failing sentences of one stored edit check (issue #23): the
    assembled re-gate's results, or the written check's."""
    from backend import review_docs
    from backend.models.written import WrittenCheckResult
    from backend.activities.claim_gate_activity import SentenceGateResult

    if results_slot:
        results = review_docs.load_doc_in(db, workflow_id, post_id, results_slot, List[SentenceGateResult])
    elif check_slot:
        results = review_docs.load_doc_in(db, workflow_id, post_id, check_slot, WrittenCheckResult).sentences
    else:
        return []
    return [r.sentence for r in results if not r.passed]


def _apply_text_free_update(db, card_db: ReviewCardDB, input: UpdateCardInput) -> Evidence:
    """The document-backed part of update_card (issue #23). Returns the
    evidence whose draft goes on the card.

    - evidence_slot: a nudged redraft becomes the current evidence;
    - edit_text_slot: the founder's edit becomes the current draft's text
      ("manual_edit" steer appended), and its check results go on the card.
    The caller has already dropped stale and late writes (edit_seq, and
    review_docs.doc_written_later), so the current evidence follows the card.
    """
    from backend import review_docs
    from backend.models.written import WrittenCheckResult
    from backend.activities.claim_gate_activity import SentenceGateResult

    post_id = input.post_id
    workflow_id = input.workflow_id
    current = review_docs.EVIDENCE_SLOT
    if input.evidence_slot:
        evidence = review_docs.load_doc_in(db, workflow_id, post_id, input.evidence_slot, Evidence)
        review_docs.save_doc_in(db, workflow_id, post_id, current, evidence)
        return evidence
    text = review_docs.load_doc_in(db, workflow_id, post_id, input.edit_text_slot, str)
    evidence = review_docs.load_doc_in(db, workflow_id, post_id, current, Evidence)
    evidence.draft.text = text
    evidence.draft.steers_applied.append(MANUAL_EDIT_STEER)
    review_docs.save_doc_in(db, workflow_id, post_id, current, evidence)
    results: List[Any] = []
    if input.edit_results_slot:
        results = review_docs.load_doc_in(
            db, workflow_id, post_id, input.edit_results_slot, List[SentenceGateResult]
        )
    elif input.edit_check_slot:
        results = list(
            review_docs.load_doc_in(db, workflow_id, post_id, input.edit_check_slot, WrittenCheckResult).sentences
        )
    card_db.edited_text_gate_results = [sentence_check_card_fields(r) for r in results]
    flag_modified(card_db, "edited_text_gate_results")
    return evidence


def _get_card_sync(input: GetCardInput) -> Optional[Dict]:
    """Synchronous DB work for get_card (B3)."""
    db = get_db()
    try:
        card_db = db.query(ReviewCardDB).filter(
            ReviewCardDB.workflow_id == input.workflow_id
        ).first()
        
        if not card_db:
            return None
        
        # Return as dict
        return {
            "workflow_id": card_db.workflow_id,
            "post": card_db.post_data,
            "evidence": card_db.evidence_data,
            "status": card_db.status,
            "created_at": card_db.created_at,
            "updated_at": card_db.updated_at,
            "approved_by": card_db.approved_by,
            "posted_url": card_db.posted_url,
            "reply_post_id": card_db.reply_post_id,
            "skip_reason": card_db.skip_reason,
            "followup_replies": card_db.followup_replies if card_db.followup_replies else None,
        }
    finally:
        db.close()


@activity.defn(name="get_card")
async def get_card(input: GetCardInput) -> Optional[Dict]:
    """
    Get a review card by workflow ID.
    
    B3: Runs sync DB work via asyncio.to_thread.
    """
    activity.logger.info(f"Getting card {input.workflow_id}")
    
    # B3: Run sync DB work in thread pool
    result = await asyncio.to_thread(_get_card_sync, input)
    
    if result:
        activity.logger.info(f"Found card {input.workflow_id}")
    else:
        activity.logger.info(f"Card {input.workflow_id} not found")
    
    return result


def _redact_removed_card_db(card_db: ReviewCardDB) -> None:
    """Redact every copy of the post text on a stored card but the post's own
    (redact_removed_card lists the fields). Used when the removal is recorded
    and on every later write to the card."""
    redacted = redact_removed_card({
        "post": card_db.post_data,
        "evidence": card_db.evidence_data,
        "skip_reason": card_db.skip_reason,
        "edited_text_gate_results": card_db.edited_text_gate_results,
        "post_anyway_failing_sentences": card_db.post_anyway_failing_sentences,
    })
    card_db.evidence_data = redacted["evidence"]
    flag_modified(card_db, "evidence_data")
    card_db.skip_reason = redacted["skip_reason"]
    for key in ("edited_text_gate_results", "post_anyway_failing_sentences"):
        if redacted[key] != getattr(card_db, key):
            setattr(card_db, key, redacted[key])
            flag_modified(card_db, key)


def _remove_card_post_text(card_db: ReviewCardDB) -> None:
    """Replace a stored card's post text and every copy of it on the card."""
    post_data = dict(card_db.post_data)
    post_data["text"] = POST_TEXT_REMOVED
    # issue #9: record the removal on the card (the UI shows "Post removed")
    post_data["text_removed"] = True
    card_db.post_data = post_data
    flag_modified(card_db, "post_data")

    _redact_removed_card_db(card_db)


def _delete_post_text_sync(input: DeletePostTextInput) -> bool:
    """delete_post_text's DB work; database errors without data (issue #23)."""
    from backend import review_docs

    with review_docs.text_safe_db_errors():
        return _delete_post_text_in_transaction(input)


def _delete_post_text_in_transaction(input: DeletePostTextInput) -> bool:
    """Synchronous DB work for delete_post_text (B8).

    Returns whether the card for input.workflow_id exists. With post_id set
    (issue #9), every other stored card of that post is redacted as well.
    """
    db = get_db()
    try:
        db.execute(sql_text("BEGIN IMMEDIATE"))  # issue #9, see _store_card_sync
        card_db = db.query(ReviewCardDB).filter(
            ReviewCardDB.workflow_id == input.workflow_id
        ).first()
        
        cards = [card_db] if card_db else []
        if input.post_id:
            cards += [
                other
                for other in db.query(ReviewCardDB).filter(
                    ReviewCardDB.post_data["id"].as_string() == input.post_id,
                    ReviewCardDB.workflow_id != input.workflow_id,
                )
            ]
        
        # Delete post text on 404 per X API compliance, keep structure
        for card in cards:
            _remove_card_post_text(card)
        
        # issue #23: the stored post and the Reviews' working documents
        from backend.review_docs import remove_post_docs

        post_id = input.post_id or ((card_db.post_data or {}).get("id") if card_db else None)
        if post_id:
            remove_post_docs(db, post_id, [c.workflow_id for c in cards])
        
        db.commit()
    finally:
        db.close()
    # issue #23: secure_delete zeroes the freed space; the checkpoint copies
    # the WAL into the database file and truncates it, so the old pages do
    # not survive in the -wal file
    # A reader holding an old snapshot can keep the checkpoint from
    # completing: the removal (already committed, and idempotent) fails with
    # a retryable error, and Temporal retries it until the checkpoint is done.
    from backend.database import checkpoint_wal

    if not checkpoint_wal():
        raise ApplicationError(
            "Post text removed; the WAL checkpoint is pending (a reader holds an old snapshot)",
            type=WAL_CHECKPOINT_PENDING_ERROR,
        )
    return card_db is not None


def apply_stored_removal(workflow_id: str, card: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Redact a card read from elsewhere (the Review's get_card query) when the
    stored card records a removal (issue #9).

    A removal can happen outside the workflow (the eval re-fetch, or another
    account's Review of the same post); the workflow's own state still holds
    the text, so readers of the query must apply the stored removal.
    """
    if not card or not isinstance(card.get("post"), dict) or card["post"].get("text_removed"):
        return card
    stored = _get_card_sync(GetCardInput(workflow_id=workflow_id))
    if not stored or not stored["post"].get("text_removed"):
        return card
    return redact_removed_card(card)


@activity.defn(name="delete_post_text")
async def delete_post_text(input: DeletePostTextInput) -> bool:
    """
    Delete post text from card after 404 (B8 - X API compliance).
    
    Keeps the card structure but removes the post text.
    """
    activity.logger.info(
        f"Deleting post text for {input.workflow_id} (key: {input.idempotency_key})"
    )
    
    # B3: Run sync DB work in thread pool
    from backend.review_docs import text_safe_db_errors

    with text_safe_db_errors():
        result = await asyncio.to_thread(_delete_post_text_sync, input)
    
    if result:
        activity.logger.info(f"Deleted post text for {input.workflow_id}")
    else:
        activity.logger.warning(f"Card {input.workflow_id} not found")
    
    return result


def _card_dict(card: ReviewCardDB) -> Dict[str, Any]:
    """A stored card as the API returns it."""
    return {
        "workflow_id": card.workflow_id,
        "post": card.post_data,
        "evidence": card.evidence_data,
        "status": card.status,
        "created_at": card.created_at,
        "updated_at": card.updated_at,
        "approved_by": card.approved_by,
        "posted_url": card.posted_url,
        "reply_post_id": card.reply_post_id,
        "skip_reason": card.skip_reason,
        "followup_replies": card.followup_replies if card.followup_replies else None,
        # B2: Include edit re-gate fields
        "edited_text_gate_results": card.edited_text_gate_results if card.edited_text_gate_results else None,
        # issue #5: the plan panel uses this to tell an edited draft apart
        "edited_text_gate_state": card.edited_text_gate_state,
        "post_anyway": card.post_anyway,
        "post_anyway_failing_sentences": card.post_anyway_failing_sentences if card.post_anyway_failing_sentences else None,
    }


def get_all_cards() -> List[Dict]:
    """Get all cards (for API access). B2: Include edited_text_gate_results and post_anyway."""
    db = get_db()
    try:
        return [_card_dict(card) for card in db.query(ReviewCardDB).all()]
    finally:
        db.close()


def get_stored_card(workflow_id: str) -> Optional[Dict[str, Any]]:
    """One stored card as the API returns it, or None."""
    db = get_db()
    try:
        card = db.get(ReviewCardDB, workflow_id)
        return _card_dict(card) if card is not None else None
    finally:
        db.close()


# issue #23: the Review state a text-free get_card query reports that can be
# ahead of the stored row (the workflow writes the row after deciding)
_QUERY_STATE_FIELDS = ("status", "posted_url", "reply_post_id", "post_anyway")


def text_free_card(workflow_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """The card of a Review that keeps text out of its history (issue #23).

    Its get_card query reports only state; the content is on the stored
    card. The query's status and approval fields win (they can be ahead of
    the row by one activity); the text and the check results come from the
    row, so they always belong together. While the card is not stored yet,
    the query's in-progress state is returned as it is.
    """
    state = {k: v for k, v in state.items() if k not in ("text_free", "post_id", "draft_sha256")}
    if state.get("in_progress"):
        return state
    card = get_stored_card(workflow_id)
    if card is None:
        return {"workflow_id": workflow_id, "status": state.get("status"), "in_progress": True,
                "created_at": state.get("created_at"), "evidence": None}
    for key in _QUERY_STATE_FIELDS:
        if state.get(key) is not None:
            card[key] = state[key]
    if card["status"] == "skipped" and not card.get("skip_reason"):
        card["skip_reason"] = state.get("skip_reason")
    return card
