"""
Database setup and models for SQLite persistence.

Per B3: Worker and API are separate processes, so in-memory store doesn't work.
"""

from sqlalchemy import create_engine, Column, String, Float, JSON, DateTime, Boolean, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from datetime import datetime, timezone
from typing import Optional
import json

from backend.config import get_settings

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


# Global engine and session factory
_engine = None
_SessionLocal = None


def get_engine():
    """Get or create the database engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {}
        )
        Base.metadata.create_all(_engine)
    return _engine


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
