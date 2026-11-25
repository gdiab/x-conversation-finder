"""
Database setup and models for SQLite persistence.

Per B3: Worker and API are separate processes, so in-memory store doesn't work.
"""

from sqlalchemy import create_engine, event, Column, String, Float, JSON, DateTime, Boolean, Integer, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from datetime import datetime, timezone
from typing import Optional
import json
import logging

from backend.config import get_settings

logger = logging.getLogger(__name__)

Base = declarative_base()


class ReviewCardDB(Base):
    """SQLite model for ReviewCard."""
    
    __tablename__ = "review_cards"
    
    workflow_id = Column(String, primary_key=True)
    post_data = Column(JSON, nullable=False)
    evidence_data = Column(JSON, nullable=False)
    status = Column(String, nullable=False)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)
    approved_by = Column(String, nullable=True)
    posted_url = Column(String, nullable=True)
    reply_post_id = Column(String, nullable=True)
    skip_reason = Column(String, nullable=True)
    followup_replies = Column(JSON, nullable=True)
    # Edit re-gate fields (ADR-0006 Amendment E + QA R11 B2/C)
    edited_text_gate_results = Column(JSON, nullable=True)
    edited_text_gate_state = Column(String, nullable=True)  # "passed", "failed", or "not_checked"
    post_anyway = Column(Boolean, nullable=True)
    post_anyway_failing_sentences = Column(JSON, nullable=True)
    # QA R13: Monotonic edit sequence to prevent stale card writes
    edit_seq = Column(Integer, nullable=True, default=0)


class StoredPostDB(Base):
    """A post found by a TopicWatch search (issue #23).

    Workflows carry only the post id; the activities read the post from here,
    so its text, author and metrics never enter Temporal history. A removal
    (delete_post_text) replaces the text and records it, as on a card.
    """

    __tablename__ = "stored_posts"

    post_id = Column(String, primary_key=True)
    post_data = Column(JSON, nullable=False)
    stored_at = Column(String, nullable=False)


class ReviewDocDB(Base):
    """A Review's working document (issue #23): its evidence, a draft
    candidate, a check result or a founder's edit or nudge text.

    The activities write and read these by (workflow_id, slot); the workflow
    holds only the slot names, digests and counts. A removal deletes every
    document of the post.
    """

    __tablename__ = "review_docs"

    workflow_id = Column(String, primary_key=True)
    slot = Column(String, primary_key=True)
    post_id = Column(String, nullable=False, index=True)
    data = Column(JSON, nullable=False)
    updated_at = Column(String, nullable=False)
    # The activity that wrote it: its id (Temporal numbers a workflow's
    # activities in scheduling order) and attempt. A late attempt (a
    # timed-out one whose thread finished after its retry) or an earlier
    # step cannot overwrite what a later one wrote. None outside an activity.
    writer_seq = Column(Integer, nullable=True)
    writer_attempt = Column(Integer, nullable=True)


class BudgetLedgerDB(Base):
    """Daily budget tracking."""
    
    __tablename__ = "budget_ledger"
    
    date = Column(String, primary_key=True)
    x_reads = Column(Float, default=0.0)
    x_cost_usd = Column(Float, default=0.0)
    inference_calls = Column(Float, default=0.0)
    inference_cost_usd = Column(Float, default=0.0)
    last_updated = Column(String, nullable=False)


class GlobalSettingsDB(Base):
    """Global settings that persist across days."""
    
    __tablename__ = "global_settings"
    
    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
    last_updated = Column(String, nullable=False)


class BudgetReservationDB(Base):
    """Track open budget reservations for retry idempotency."""
    
    __tablename__ = "budget_reservations"
    
    # Composite key: workflow_id:workflow_run_id:activity_id:date:budget_type
    reservation_key = Column(String, primary_key=True)
    workflow_id = Column(String, nullable=False)
    workflow_run_id = Column(String, nullable=False)
    activity_id = Column(String, nullable=False)
    reservation_date = Column(String, nullable=False)  # UTC date
    budget_type = Column(String, nullable=False)  # 'x_reads' or 'inference'
    reserved_amount = Column(Float, nullable=False)  # reads or tokens
    cost_usd = Column(Float, nullable=False)
    created_at = Column(String, nullable=False)
    reconciled = Column(Integer, default=0)  # 0 = open, 1 = reconciled


class BudgetReconciliationDB(Base):
    """Track reconciliations for idempotency across activity retries."""
    
    __tablename__ = "budget_reconciliations"
    
    # Composite key: workflow_id + activity_id + attempt
    reconciliation_key = Column(String, primary_key=True)
    workflow_id = Column(String, nullable=False)
    activity_id = Column(String, nullable=False)
    attempt = Column(String, nullable=False)
    reservation_date = Column(String, nullable=False)  # UTC date of reservation
    reserved_amount = Column(Float, nullable=False)  # reads or tokens
    actual_amount = Column(Float, nullable=False)  # actual used
    reconciled_at = Column(String, nullable=False)
    budget_type = Column(String, nullable=False)  # 'x_reads' or 'inference'


# Global engine and session factory
_engine = None
_SessionLocal = None


def get_engine():
    """Get or create the database engine (B3: resolved to absolute path)."""
    global _engine
    if _engine is None:
        settings = get_settings()
        resolved_url = settings.get_resolved_database_url()
        logger.info(f"Database URL resolved to: {resolved_url}")
        _engine = create_engine(
            resolved_url,
            connect_args={"check_same_thread": False} if "sqlite" in resolved_url else {}
        )
        if "sqlite" in resolved_url:
            event.listen(_engine, "connect", _sqlite_secure_delete)
        Base.metadata.create_all(_engine)
    return _engine


def _sqlite_secure_delete(dbapi_connection, connection_record) -> None:
    """Issue #23: SQLite overwrites deleted content with zeros (removal duty,
    ADR-0006). secure_delete is a per-connection setting, so every new
    connection of the pool sets it."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA secure_delete=ON")
    finally:
        cursor.close()


# Issue #23: attempts of the checkpoint per removal attempt. A reader holding
# an old snapshot (the API process) can keep the WAL from being reset; the
# attempt waits for SQLite's busy timeout (5 s), and delete_post_text then
# fails retryably, so Temporal retries the removal until it completes.
WAL_CHECKPOINT_ATTEMPTS = 1


def checkpoint_wal() -> bool:
    """Copy the WAL into the database file and truncate the WAL to zero bytes
    (issue #23: after a removal, the old pages must not survive in the -wal
    file). Returns whether the checkpoint completed; a reader that keeps an
    old snapshot can make it incomplete (delete_post_text then fails
    retryably).
    """
    engine = get_engine()
    if "sqlite" not in str(engine.url):
        return True
    for _ in range(WAL_CHECKPOINT_ATTEMPTS):
        with engine.connect() as conn:
            busy, _log, _checkpointed = conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)")).one()
        if not busy:
            return True
    logger.warning("WAL checkpoint after a removal did not complete; a reader holds an old snapshot")
    return False


def get_session_factory():
    """Get or create the session factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=get_engine())
    return _SessionLocal


def get_db() -> Session:
    """Get a database session."""
    SessionLocal = get_session_factory()
    return SessionLocal()


def init_db():
    """Initialize database with WAL mode (B3)."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    
    # Enable WAL mode for SQLite (B3)
    if "sqlite" in str(engine.url):
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.commit()
    
    # QA R13: Run migrations for existing databases
    _migrate_add_edit_seq(engine)


def _migrate_add_edit_seq(engine):
    """
    QA R13: Migration to add edit_seq column to review_cards.
    
    This migration is idempotent - it checks if the column exists before adding it.
    """
    if "sqlite" not in str(engine.url):
        # Only needed for SQLite; other DBs handle this differently
        return
    
    with engine.connect() as conn:
        # Check if edit_seq column exists
        result = conn.execute(text("PRAGMA table_info(review_cards)"))
        columns = [row[1] for row in result]
        
        if 'edit_seq' not in columns:
            logger.info("Adding edit_seq column to review_cards table")
            conn.execute(text("ALTER TABLE review_cards ADD COLUMN edit_seq INTEGER DEFAULT 0"))
            conn.commit()
            logger.info("Migration complete: edit_seq column added")
