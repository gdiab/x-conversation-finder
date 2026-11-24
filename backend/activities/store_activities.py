"""Storage activities for review cards using SQLite."""

from dataclasses import asdict, dataclass
from typing import Optional, Dict, Any, List, Union
from temporalio import activity
import asyncio
from sqlalchemy.orm.attributes import flag_modified

from backend.models import ReviewCard, Evidence
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


@dataclass
class GetCardInput:
    """Input for get_card activity."""
    
    workflow_id: str


@dataclass
class DeletePostTextInput:
    """Input for delete_post_text activity (B8 - 404 handling)."""
    
    workflow_id: str
    idempotency_key: str


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
            } if card.evidence.draft else None,
            "skip_reason": card.evidence.skip_reason,
            # Issue #4: should-we-reply features and checks
            "reply_fit": asdict(card.evidence.reply_fit) if card.evidence.reply_fit else None,
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


def _store_card_sync(input: StoreCardInput) -> bool:
    """Synchronous DB work for store_card (B3)."""
    db = get_db()
    try:
        card_db = _card_to_db(input.card)
        
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


def _delete_post_text_sync(input: DeletePostTextInput) -> bool:
    """Synchronous DB work for delete_post_text (B8)."""
    db = get_db()
    try:
        card_db = db.query(ReviewCardDB).filter(
            ReviewCardDB.workflow_id == input.workflow_id
        ).first()
        
        if not card_db:
            return False
        
        # Delete post text on 404 per X API compliance, keep structure
        post_data = dict(card_db.post_data)
        post_data["text"] = "[removed - post deleted]"
        card_db.post_data = post_data
        flag_modified(card_db, "post_data")

        # Evidence fields that quote the post go too: why_it_fits quotes it,
        # and the issue #4 problem span is a verbatim copy of part of it.
        evidence_data = dict(card_db.evidence_data or {})
        if evidence_data.get("why_it_fits"):
            evidence_data["why_it_fits"] = "[removed - post deleted]"
        reply_fit = evidence_data.get("reply_fit")
        if isinstance(reply_fit, dict) and reply_fit.get("brief_problem_span"):
            evidence_data["reply_fit"] = {**reply_fit, "brief_problem_span": "[removed - post deleted]"}
        card_db.evidence_data = evidence_data
        flag_modified(card_db, "evidence_data")
        
        db.commit()
        return True
    finally:
        db.close()


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
                "post_anyway": card.post_anyway,
                "post_anyway_failing_sentences": card.post_anyway_failing_sentences if card.post_anyway_failing_sentences else None,
            }
            for card in cards
        ]
    finally:
        db.close()
