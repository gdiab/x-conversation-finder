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
from typing import Tuple
from backend.database import get_db, BudgetLedgerDB, GlobalSettingsDB, BudgetReconciliationDB
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


def reserve_x_budget(reads_needed: int = 1, cost_per_read: float = None) -> Tuple[float, str]:
    """
    Atomically reserve X API budget.
    
    Uses BEGIN IMMEDIATE transaction for atomic reserve-then-commit.
    Cap enforced on first call too.
    
    Returns (cost in USD, reservation date YYYY-MM-DD).
    
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
        return cost, today
        
    except BudgetExceeded:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise
    finally:
        db.close()


def reserve_inference_budget(input_tokens: int, output_tokens: int) -> Tuple[float, str]:
    """
    Atomically reserve inference budget.
    
    Uses per-token pricing from config (NB3).
    Uses BEGIN IMMEDIATE transaction for atomic reserve-then-commit.
    Cap enforced on first call too.
    
    Returns (cost in USD, reservation date YYYY-MM-DD).
    
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
        return cost, today
        
    except BudgetExceeded:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise
    finally:
        db.close()


def reconcile_x_reads(
    reserved_reads: int,
    actual_reads: int,
    reservation_date: str,
    workflow_id: str,
    workflow_run_id: str,
    activity_id: str,
    cost_per_read: float = None
) -> float:
    """
    Reconcile X budget after actual results known (R5-B1: two-way with idempotency).
    
    - Charges delta when actual > reserved
    - Refunds delta when actual < reserved  
    - Applies to reservation's own UTC day, not today
    - Idempotent per workflow run and activity (workflow_id, workflow_run_id, activity_id)
    - Never takes a day below zero
    
    Returns the actual cost charged.
    """
    settings = get_settings()
    
    if cost_per_read is None:
        cost_per_read = settings.x_post_read_cost
    
    actual_cost = actual_reads * cost_per_read
    reconciliation_key = f"{workflow_id}:{workflow_run_id}:{activity_id}:x_reads"
    
    db = get_db()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
        
        # Check if already reconciled (idempotency)
        existing = db.query(BudgetReconciliationDB).filter(
            BudgetReconciliationDB.reconciliation_key == reconciliation_key
        ).first()
        
        if existing:
            # Already reconciled, return the actual cost
            return actual_cost
        
        # Get ledger for reservation date (not today)
        ledger = db.query(BudgetLedgerDB).filter(
            BudgetLedgerDB.date == reservation_date
        ).first()
        
        if not ledger:
            # Should not happen (reservation should have created it)
            ledger = BudgetLedgerDB(
                date=reservation_date,
                x_reads=0.0,
                x_cost_usd=0.0,
                inference_calls=0,
                inference_cost_usd=0.0,
                last_updated=datetime.now(timezone.utc).isoformat(),
            )
            db.add(ledger)
        
        # Calculate delta
        delta_reads = actual_reads - reserved_reads
        delta_cost = delta_reads * cost_per_read
        
        # Apply delta (charge or refund)
        new_reads = ledger.x_reads + delta_reads
        new_cost = ledger.x_cost_usd + delta_cost
        
        # Clamp at zero (never go negative)
        ledger.x_reads = max(0.0, new_reads)
        ledger.x_cost_usd = max(0.0, new_cost)
        ledger.last_updated = datetime.now(timezone.utc).isoformat()
        
        # Record reconciliation for idempotency
        reconciliation = BudgetReconciliationDB(
            reconciliation_key=reconciliation_key,
            workflow_id=workflow_id,
            activity_id=activity_id,
            attempt=workflow_run_id,  # Store run_id in attempt field for now
            reservation_date=reservation_date,
            reserved_amount=float(reserved_reads),
            actual_amount=float(actual_reads),
            reconciled_at=datetime.now(timezone.utc).isoformat(),
            budget_type='x_reads',
        )
        db.add(reconciliation)
        
        db.commit()
        return actual_cost
        
    except Exception as e:
        db.rollback()
        raise
    finally:
        db.close()


def reconcile_inference_tokens(
    reserved_input: int,
    reserved_output: int,
    actual_input: int,
    actual_output: int,
    reservation_date: str,
    workflow_id: str,
    workflow_run_id: str,
    activity_id: str
) -> float:
    """
    Reconcile inference budget after actual usage known (R5-B1: two-way with idempotency).
    
    - Charges delta when actual > reserved
    - Refunds delta when actual < reserved
    - Applies to reservation's own UTC day, not today
    - Idempotent per workflow run and activity (workflow_id, workflow_run_id, activity_id)
    - Never takes a day below zero
    
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
    
    reconciliation_key = f"{workflow_id}:{workflow_run_id}:{activity_id}:inference"
    
    db = get_db()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
        
        # Check if already reconciled (idempotency)
        existing = db.query(BudgetReconciliationDB).filter(
            BudgetReconciliationDB.reconciliation_key == reconciliation_key
        ).first()
        
        if existing:
            # Already reconciled, return the actual cost
            return actual_cost
        
        # Get ledger for reservation date (not today)
        ledger = db.query(BudgetLedgerDB).filter(
            BudgetLedgerDB.date == reservation_date
        ).first()
        
        if not ledger:
            # Should not happen (reservation should have created it)
            ledger = BudgetLedgerDB(
                date=reservation_date,
                x_reads=0.0,
                x_cost_usd=0.0,
                inference_calls=0,
                inference_cost_usd=0.0,
                last_updated=datetime.now(timezone.utc).isoformat(),
            )
            db.add(ledger)
        
        # Calculate delta
        delta_cost = actual_cost - reserved_cost
        
        # Apply delta (charge or refund)
        new_cost = ledger.inference_cost_usd + delta_cost
        
        # Clamp at zero (never go negative)
        ledger.inference_cost_usd = max(0.0, new_cost)
        ledger.last_updated = datetime.now(timezone.utc).isoformat()
        
        # Record reconciliation for idempotency
        total_reserved = reserved_input + reserved_output
        total_actual = actual_input + actual_output
        reconciliation = BudgetReconciliationDB(
            reconciliation_key=reconciliation_key,
            workflow_id=workflow_id,
            activity_id=activity_id,
            attempt=workflow_run_id,  # Store run_id in attempt field for now
            reservation_date=reservation_date,
            reserved_amount=float(total_reserved),
            actual_amount=float(total_actual),
            reconciled_at=datetime.now(timezone.utc).isoformat(),
            budget_type='inference',
        )
        db.add(reconciliation)
        
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
