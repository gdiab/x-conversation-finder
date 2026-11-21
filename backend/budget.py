"""
Budget tracking and enforcement.

Per NB3 and R3-B3: Atomic reserve-then-commit in one SQLite transaction (BEGIN IMMEDIATE).
- Cap enforced on first call too
- Raise non-retryable BudgetExceeded ApplicationError
- Per-token inference pricing from config
- Kill switch (env/config flag + DB flag checked before every X/LLM call)
- lookup_post over budget must raise, never report not-found
"""

from datetime import datetime, timezone
from sqlalchemy import text
from temporalio.exceptions import ApplicationError
from backend.database import get_db, BudgetLedgerDB, GlobalSettingsDB
from backend.config import get_settings


class BudgetExceeded(ApplicationError):
    """
    Raised when budget is exceeded.
    
    Non-retryable exception per NB3 and R3-B3.
    """
    def __init__(self, message: str):
        super().__init__(message, non_retryable=True, type='BudgetExceeded')


def get_today_utc() -> str:
    """Get today's date in UTC as YYYY-MM-DD."""
    return datetime.now(timezone.utc).date().isoformat()


def _check_kill_switch(db):
    """
    Check kill switch (env/config flag + DB flag).
    
    Raises BudgetExceeded if either kill switch is active.
    Per S-2: DB kill switch persists across days until explicitly cleared.
    """
    settings = get_settings()
    
    if settings.budget_kill_switch:
        raise BudgetExceeded("Budget kill switch enabled in config")
    
    # Check global kill switch (persists until cleared, not per-day)
    global_setting = db.query(GlobalSettingsDB).filter(
        GlobalSettingsDB.key == "kill_switch"
    ).first()
    
    if global_setting and global_setting.value == "true":
        raise BudgetExceeded("Budget kill switch enabled in database")


def reserve_x_budget(reads_needed: int = 1, cost_per_read: float = None) -> float:
    """
    Atomically reserve X API budget.
    
    Uses BEGIN IMMEDIATE transaction for atomic reserve-then-commit.
    Cap enforced on first call too.
    
    Returns the cost in USD.
    
    Raises BudgetExceeded if:
    - Kill switch is active (env/config or DB)
    - Adding reads would exceed daily_spend_limit_usd
    - Adding reads would exceed daily_post_read_cap
    """
    settings = get_settings()
    today = get_today_utc()
    
    if cost_per_read is None:
        cost_per_read = settings.x_post_read_cost
    
    cost = reads_needed * cost_per_read
    
    db = get_db()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
        
        _check_kill_switch(db)
        
        ledger = db.query(BudgetLedgerDB).filter(BudgetLedgerDB.date == today).first()
        
        current_reads = ledger.x_reads if ledger else 0
        current_cost = ledger.x_cost_usd if ledger else 0.0
        
        projected_reads = current_reads + reads_needed
        projected_cost = current_cost + cost
        
        if projected_cost > settings.daily_spend_limit_usd:
            raise BudgetExceeded(
                f"Would exceed daily spend limit: "
                f"${projected_cost:.2f} > ${settings.daily_spend_limit_usd:.2f}"
            )
        
        if projected_reads > settings.daily_post_read_cap:
            raise BudgetExceeded(
                f"Would exceed daily read cap: "
                f"{projected_reads} > {settings.daily_post_read_cap}"
            )
        
        if not ledger:
            ledger = BudgetLedgerDB(
                date=today,
                x_reads=reads_needed,
                x_cost_usd=cost,
                inference_calls=0,
                inference_cost_usd=0.0,
                last_updated=datetime.now(timezone.utc).isoformat(),
            )
            db.add(ledger)
        else:
            ledger.x_reads = projected_reads
            ledger.x_cost_usd = projected_cost
            ledger.last_updated = datetime.now(timezone.utc).isoformat()
        
        db.commit()
        return cost
        
    except BudgetExceeded:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise
    finally:
        db.close()


def reserve_inference_budget(input_tokens: int, output_tokens: int) -> float:
    """
    Atomically reserve inference budget.
    
    Uses per-token pricing from config (NB3).
    Uses BEGIN IMMEDIATE transaction for atomic reserve-then-commit.
    Cap enforced on first call too.
    
    Returns the cost in USD.
    
    Raises BudgetExceeded if:
    - Kill switch is active (env/config or DB)
    - Adding cost would exceed daily_inference_limit_usd
    """
    settings = get_settings()
    today = get_today_utc()
    
    cost = (
        input_tokens * settings.inference_input_token_cost_usd +
        output_tokens * settings.inference_output_token_cost_usd
    )
    
    db = get_db()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
        
        _check_kill_switch(db)
        
        ledger = db.query(BudgetLedgerDB).filter(BudgetLedgerDB.date == today).first()
        
        current_cost = ledger.inference_cost_usd if ledger else 0.0
        current_calls = ledger.inference_calls if ledger else 0
        
        projected_cost = current_cost + cost
        
        if projected_cost > settings.daily_inference_limit_usd:
            raise BudgetExceeded(
                f"Would exceed daily inference limit: "
                f"${projected_cost:.4f} > ${settings.daily_inference_limit_usd:.2f}"
            )
        
        if not ledger:
            ledger = BudgetLedgerDB(
                date=today,
                x_reads=0,
                x_cost_usd=0.0,
                inference_calls=1,
                inference_cost_usd=cost,
                last_updated=datetime.now(timezone.utc).isoformat(),
            )
            db.add(ledger)
        else:
            ledger.inference_calls = current_calls + 1
            ledger.inference_cost_usd = projected_cost
            ledger.last_updated = datetime.now(timezone.utc).isoformat()
        
        db.commit()
        return cost
        
    except BudgetExceeded:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise
    finally:
        db.close()


def reconcile_x_reads(reserved_reads: int, actual_reads: int, cost_per_read: float = None) -> float:
    """
    Reconcile X budget after actual results known (R4-B2).
    
    Refunds the difference if actual < reserved.
    Returns the actual cost charged.
    """
    if actual_reads >= reserved_reads:
        # No reconciliation needed
        return actual_reads * (cost_per_read or get_settings().x_post_read_cost)
    
    settings = get_settings()
    today = get_today_utc()
    
    if cost_per_read is None:
        cost_per_read = settings.x_post_read_cost
    
    refund_reads = reserved_reads - actual_reads
    refund_cost = refund_reads * cost_per_read
    
    db = get_db()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
        
        ledger = db.query(BudgetLedgerDB).filter(BudgetLedgerDB.date == today).first()
        
        if ledger:
            ledger.x_reads -= refund_reads
            ledger.x_cost_usd -= refund_cost
            ledger.last_updated = datetime.now(timezone.utc).isoformat()
            db.commit()
        
        return actual_reads * cost_per_read
        
    except Exception as e:
        db.rollback()
        raise
    finally:
        db.close()


def reconcile_inference_tokens(reserved_input: int, reserved_output: int, 
                               actual_input: int, actual_output: int) -> float:
    """
    Reconcile inference budget after actual usage known (R4-B2).
    
    Refunds the difference if actual < reserved.
    Returns the actual cost charged.
    """
    settings = get_settings()
    
    reserved_cost = (
        reserved_input * settings.inference_input_token_cost_usd +
        reserved_output * settings.inference_output_token_cost_usd
    )
    
    actual_cost = (
        actual_input * settings.inference_input_token_cost_usd +
        actual_output * settings.inference_output_token_cost_usd
    )
    
    if actual_cost >= reserved_cost:
        # No reconciliation needed
        return actual_cost
    
    refund_cost = reserved_cost - actual_cost
    today = get_today_utc()
    
    db = get_db()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
        
        ledger = db.query(BudgetLedgerDB).filter(BudgetLedgerDB.date == today).first()
        
        if ledger:
            ledger.inference_cost_usd -= refund_cost
            ledger.last_updated = datetime.now(timezone.utc).isoformat()
            db.commit()
        
        return actual_cost
        
    except Exception as e:
        db.rollback()
        raise
    finally:
        db.close()


def get_today_budget() -> dict:
    """Get today's budget usage for the spend meter."""
    settings = get_settings()
    today = get_today_utc()
    
    db = get_db()
    try:
        ledger = db.query(BudgetLedgerDB).filter(BudgetLedgerDB.date == today).first()
        
        # Check global kill switch (persists across days)
        global_setting = db.query(GlobalSettingsDB).filter(
            GlobalSettingsDB.key == "kill_switch"
        ).first()
        db_kill_switch = (global_setting.value == "true") if global_setting else False
        
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
                "kill_switch": settings.budget_kill_switch or db_kill_switch,
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
            "kill_switch": settings.budget_kill_switch or db_kill_switch,
        }
    finally:
        db.close()


def set_kill_switch(enabled: bool):
    """
    Set the global kill switch in the database.
    
    Per S-2: Kill switch persists across days until explicitly cleared.
    This is in addition to the env/config kill switch.
    """
    db = get_db()
    try:
        global_setting = db.query(GlobalSettingsDB).filter(
            GlobalSettingsDB.key == "kill_switch"
        ).first()
        
        if not global_setting:
            global_setting = GlobalSettingsDB(
                key="kill_switch",
                value="true" if enabled else "false",
                last_updated=datetime.now(timezone.utc).isoformat(),
            )
            db.add(global_setting)
        else:
            global_setting.value = "true" if enabled else "false"
            global_setting.last_updated = datetime.now(timezone.utc).isoformat()
        
        db.commit()
    finally:
        db.close()
