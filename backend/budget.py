"""
Budget tracking and enforcement.

Per B6 and ADR-0005: Daily spend ledger with hard caps.
"""

from datetime import datetime, timezone
from backend.database import get_db, BudgetLedgerDB
from backend.config import get_settings


def get_today_utc() -> str:
    """Get today's date in UTC as YYYY-MM-DD."""
    return datetime.now(timezone.utc).date().isoformat()


def check_x_budget(reads_needed: int = 1) -> bool:
    """
    Check if we can afford X reads within daily budget.
    
    Returns True if within budget, False if cap exceeded.
    """
    settings = get_settings()
    today = get_today_utc()
    
    db = get_db()
    try:
        ledger = db.query(BudgetLedgerDB).filter(BudgetLedgerDB.date == today).first()
        
        if not ledger:
            # First call today, within budget
            return True
        
        # Check if adding these reads would exceed the daily cap
        projected_reads = ledger.x_reads + reads_needed
        projected_cost = projected_reads * settings.x_post_read_cost
        
        if projected_cost >= settings.daily_spend_limit_usd:
            return False
        
        # Also check post count cap
        if projected_reads >= settings.daily_post_read_cap:
            return False
        
        return True
    finally:
        db.close()


def record_x_reads(reads: int, cost_usd: float):
    """Record X API reads in the daily ledger."""
    today = get_today_utc()
    
    db = get_db()
    try:
        ledger = db.query(BudgetLedgerDB).filter(BudgetLedgerDB.date == today).first()
        
        if not ledger:
            ledger = BudgetLedgerDB(
                date=today,
                x_reads=reads,
                x_cost_usd=cost_usd,
                inference_calls=0,
                inference_cost_usd=0.0,
                last_updated=datetime.now(timezone.utc).isoformat(),
            )
            db.add(ledger)
        else:
            ledger.x_reads += reads
            ledger.x_cost_usd += cost_usd
            ledger.last_updated = datetime.now(timezone.utc).isoformat()
        
        db.commit()
    finally:
        db.close()


def check_inference_budget() -> bool:
    """
    Check if we can afford an inference call within daily budget.
    
    Returns True if within budget, False if cap exceeded.
    """
    settings = get_settings()
    today = get_today_utc()
    
    db = get_db()
    try:
        ledger = db.query(BudgetLedgerDB).filter(BudgetLedgerDB.date == today).first()
        
        if not ledger:
            return True
        
        if ledger.inference_cost_usd >= settings.daily_inference_limit_usd:
            return False
        
        return True
    finally:
        db.close()


def record_inference(cost_usd: float, tokens: int = 0):
    """Record LLM inference call in the daily ledger."""
    today = get_today_utc()
    
    db = get_db()
    try:
        ledger = db.query(BudgetLedgerDB).filter(BudgetLedgerDB.date == today).first()
        
        if not ledger:
            ledger = BudgetLedgerDB(
                date=today,
                x_reads=0,
                x_cost_usd=0.0,
                inference_calls=1,
                inference_cost_usd=cost_usd,
                last_updated=datetime.now(timezone.utc).isoformat(),
            )
            db.add(ledger)
        else:
            ledger.inference_calls += 1
            ledger.inference_cost_usd += cost_usd
            ledger.last_updated = datetime.now(timezone.utc).isoformat()
        
        db.commit()
    finally:
        db.close()


def get_today_budget() -> dict:
    """Get today's budget usage for the spend meter."""
    settings = get_settings()
    today = get_today_utc()
    
    db = get_db()
    try:
        ledger = db.query(BudgetLedgerDB).filter(BudgetLedgerDB.date == today).first()
        
        if not ledger:
            return {
                "date": today,
                "x_reads": 0,
                "x_cost_usd": 0.0,
                "x_limit_usd": settings.daily_spend_limit_usd,
                "x_cap_reads": settings.daily_post_read_cap,
                "inference_calls": 0,
                "inference_cost_usd": 0.0,
                "inference_limit_usd": settings.daily_inference_limit_usd,
            }
        
        return {
            "date": today,
            "x_reads": ledger.x_reads,
            "x_cost_usd": ledger.x_cost_usd,
            "x_limit_usd": settings.daily_spend_limit_usd,
            "x_cap_reads": settings.daily_post_read_cap,
            "inference_calls": ledger.inference_calls,
            "inference_cost_usd": ledger.inference_cost_usd,
            "inference_limit_usd": settings.daily_inference_limit_usd,
        }
    finally:
        db.close()
