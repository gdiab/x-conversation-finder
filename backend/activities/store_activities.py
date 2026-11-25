"""Storage activities for review cards using SQLite."""

from dataclasses import asdict, dataclass
from typing import Optional, Dict, Any, List, Union
from temporalio import activity
import asyncio
from sqlalchemy.orm.attributes import flag_modified

from backend.models import ReviewCard, Evidence
from backend.models.reply_plan import reply_plan_card_fields

# Placeholder for post text removed under X's removal duty (ADR-0006); the
# Review workflow's get_card query uses the same value.
POST_TEXT_REMOVED = "[removed - post deleted]"

# Issue #9: skip reason for a pending Review whose target post returned 404 on
# the re-fetch at approve time. The frontend shows "Post removed" for it.
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
    return redacted


def _redact_draft(draft: Dict[str, Any], materials: Any) -> Dict[str, Any]:
    """A stored draft without model-derived gate details (issue #9).

    The assembled draft text and segments inserted by code are kept; free
    text or unknown identifiers in segments and gate-generated steers are
    removed, and the claim-gate details are dropped as a whole (older cards
    can hold echoed model values in any of them). Founder nudges and edits
    are the founder's own text and stay.
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
      gate-failure skip reason (redact_skip_reason).
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
from backend.database import get_db, ReviewCardDB


@dataclass
class StoreCardInput:
    """Input for store_card activity."""
    
    card: ReviewCard
    idempotency_key: str


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
            } if card.evidence.draft else None,
            "skip_reason": card.evidence.skip_reason,
            # Issue #4: should-we-reply features and checks
            "reply_fit": asdict(card.evidence.reply_fit) if card.evidence.reply_fit else None,
            **reply_plan_card_fields(card.evidence),  # issue #5
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
            {
                "sentence": r.sentence,
                "passed": r.passed,
                "kind": r.kind,
                "reason": r.reason,
                "kept_segment": r.kept_segment,
                "source_passage_id": r.source_passage_id,
                "sentence_index": r.sentence_index,
            }
            for r in card.edited_text_gate_results
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


def card_post_text_removed(workflow_id: str) -> bool:
    """Whether this workflow's stored card records a removal (issue #9)."""
    card = _get_card_sync(GetCardInput(workflow_id=workflow_id))
    return bool(card and card["post"].get("text_removed"))


def _store_card_sync(input: StoreCardInput) -> bool:
    """Synchronous DB work for store_card (B3)."""
    db = get_db()
    try:
        card_db = _card_to_db(input.card)
        
        # issue #9: a removal already recorded for this post (by another
        # account's Review, or this card's own earlier removal) also applies to
        # this write, so a late or retried store cannot put the text back
        if _post_removal_recorded(db, card_db.post_data["id"]):
            _remove_card_post_text(card_db)
        
        # Use INSERT...ON CONFLICT (B3)
        # SQLAlchemy's merge() provides upsert-like behavior
        db.merge(card_db)
        db.commit()
        return True
    finally:
        db.close()


@activity.defn(name="store_card")
async def store_card(input: StoreCardInput) -> bool:
    """
    Store a review card in SQLite.
    
    Idempotent: upsert by workflow_id using merge (INSERT...ON CONFLICT).
    B3: Runs sync DB work via asyncio.to_thread.
    """
    activity.logger.info(
        f"Storing card {input.card.workflow_id} (key: {input.idempotency_key})"
    )
    
    # B3: Run sync DB work in thread pool
    result = await asyncio.to_thread(_store_card_sync, input)
    
    activity.logger.info(f"Stored card {input.card.workflow_id}")
    return result


def _update_card_sync(input: UpdateCardInput) -> bool:
    """
    Synchronous DB work for update_card. B1: Works with typed fields instead of dict.
    
    QA R13: Checks edit_seq and ignores writes with lower seq than stored.
    """
    db = get_db()
    try:
        card_db = db.query(ReviewCardDB).filter(
            ReviewCardDB.workflow_id == input.workflow_id
        ).first()
        
        if not card_db:
            return False
        
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
        
        # B2: Update evidence_data JSON with flag_modified
        if input.evidence is not None:
            value = input.evidence
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
                }
            card_db.evidence_data = evidence_data
            flag_modified(card_db, "evidence_data")
        
        db.commit()
        return True
    finally:
        db.close()


@activity.defn(name="update_card")
async def update_card(input: UpdateCardInput) -> bool:
    """
    Update a review card.
    
    Idempotent: updates are applied by field.
    B3: Runs sync DB work via asyncio.to_thread.
    """
    activity.logger.info(
        f"Updating card {input.workflow_id} (key: {input.idempotency_key})"
    )
    
    # B3: Run sync DB work in thread pool
    result = await asyncio.to_thread(_update_card_sync, input)
    
    if not result:
        activity.logger.error(f"Card {input.workflow_id} not found")
    else:
        activity.logger.info(f"Updated card {input.workflow_id}")
    
    return result


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


def _remove_card_post_text(card_db: ReviewCardDB) -> None:
    """Replace a stored card's post text and every evidence copy of it."""
    post_data = dict(card_db.post_data)
    post_data["text"] = POST_TEXT_REMOVED
    # issue #9: record the removal on the card (the UI shows "Post removed")
    post_data["text_removed"] = True
    card_db.post_data = post_data
    flag_modified(card_db, "post_data")

    card_db.evidence_data = redact_post_text_copies(card_db.evidence_data)
    flag_modified(card_db, "evidence_data")
    card_db.skip_reason = redact_skip_reason(card_db.skip_reason)


def _delete_post_text_sync(input: DeletePostTextInput) -> bool:
    """Synchronous DB work for delete_post_text (B8).

    Returns whether the card for input.workflow_id exists. With post_id set
    (issue #9), every other stored card of that post is redacted as well.
    """
    db = get_db()
    try:
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
        
        db.commit()
        return card_db is not None
    finally:
        db.close()


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
    return {
        **card,
        "post": {**card["post"], "text": POST_TEXT_REMOVED, "text_removed": True},
        "evidence": redact_post_text_copies(card.get("evidence")),
        "skip_reason": redact_skip_reason(card.get("skip_reason")),
    }


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
    result = await asyncio.to_thread(_delete_post_text_sync, input)
    
    if result:
        activity.logger.info(f"Deleted post text for {input.workflow_id}")
    else:
        activity.logger.warning(f"Card {input.workflow_id} not found")
    
    return result


def get_all_cards() -> List[Dict]:
    """Get all cards (for API access). B2: Include edited_text_gate_results and post_anyway."""
    db = get_db()
    try:
        cards = db.query(ReviewCardDB).all()
        
        return [
            {
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
            for card in cards
        ]
    finally:
        db.close()
