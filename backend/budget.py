"""
Budget tracking and enforcement.

Per NB3 and R3-B3: Atomic reserve-then-commit in one SQLite transaction (BEGIN IMMEDIATE).
- Cap enforced on first call too
- Raise non-retryable BudgetExceeded ApplicationError
- Per-token inference pricing from config
- Kill switch (env/config flag + DB flag checked before every X/LLM call)
- lookup_post over budget must raise, never report not-found

Issue #8: every paid attempt is counted.
- Reservations and reconciliations are keyed by Temporal activity attempt, so a
  retry after a lost completion makes a new reservation instead of reusing the
  previous attempt's. Inference keys also carry a call index, one per paid call.
- An open reservation from an attempt that sent its request stays counted at the
  reserved amount: the call may have been billed.
- An X attempt that X refused with a 4xx status (including 429) is released
  (reconciled to zero): X bills per post returned, and the X endpoint is fixed
  in code, so a 4xx comes from X itself. Inference errors are never released:
  openai_base_url can point at a gateway whose status says nothing about
  upstream billing. 5xx, timeouts and crashes stay counted.
- Inference reservations are sized from an upper bound on the prompt's tokens
  plus the call's max output tokens (prompt_token_bound, InferenceMeter), not
  from fixed estimates.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from sqlalchemy import text
from temporalio import activity
from temporalio.exceptions import ApplicationError
from typing import Iterator, List, Optional, Sequence, Tuple
from backend.database import get_db, BudgetLedgerDB, GlobalSettingsDB, BudgetReconciliationDB, BudgetReservationDB
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


def _resolve_attempt(attempt: Optional[int]) -> int:
    """The attempt to key a reservation by.

    None means the current Temporal activity attempt when called inside an
    activity, else 1. Callers that pass workflow_id/workflow_run_id/activity_id
    from activity.info() therefore get per-attempt keys without passing it.
    """
    if attempt is not None:
        return attempt
    if activity.in_activity():
        return activity.info().attempt
    return 1


class ReservationClosed(ApplicationError):
    """A reservation that was already reconciled (or released) was asked to
    authorize another request. Each paid request needs its own reservation."""

    def __init__(self, reservation_key: str):
        super().__init__(
            f"Reservation {reservation_key} is closed; a new paid request needs a new "
            "attempt or call index",
            non_retryable=True,
            type="ReservationClosed",
        )


def _refuse_closed_reservation(existing) -> None:
    """Reuse is only for the same request reserving again while still open.
    A closed reservation must not authorize a new paid request uncounted."""
    if existing.reconciled:
        raise ReservationClosed(existing.reservation_key)


def _reservation_key(
    workflow_id: str,
    workflow_run_id: str,
    activity_id: str,
    attempt: int,
    reservation_date: str,
    budget_type: str,
    call_index: Optional[int] = None,
) -> str:
    call = f":c{call_index}" if call_index is not None else ""
    return f"{workflow_id}:{workflow_run_id}:{activity_id}:a{attempt}{call}:{reservation_date}:{budget_type}"



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


def reserve_x_budget(
    reads_needed: int = 1,
    cost_per_read: float = None,
    workflow_id: Optional[str] = None,
    workflow_run_id: Optional[str] = None,
    activity_id: Optional[str] = None,
    attempt: Optional[int] = None,
) -> Tuple[float, str]:
    """
    Atomically reserve X API budget.
    
    Uses BEGIN IMMEDIATE transaction for atomic reserve-then-commit.
    Cap enforced on first call too.
    
    Note: The read cap is soft by at most one call. A reservation that would exceed the cap
    is rejected, but the cap may be exceeded by up to (max_results - 1) reads if the last
    allowed reservation is fully used. For example, with a cap of 100 and max_results=10,
    the last allowed call might reserve reads 91-100, so actual usage could reach 100.
    Reconciliation adjusts down to actual usage but never prevents a reservation that
    would stay within the cap.
    
    If workflow_id, workflow_run_id, and activity_id are provided, the reservation is
    keyed by them plus the activity attempt (issue #8). The same attempt reserving
    again reuses its reservation; a retry (a new attempt) makes a new one, because
    the previous attempt may have made the paid call before its completion was lost.
    `attempt` defaults to the current activity attempt (see _resolve_attempt).
    Pass the same `attempt` to reconcile_x_reads / release_x_reservation.
    
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
    keyed = bool(workflow_id and workflow_run_id and activity_id)
    if keyed:
        reservation_key = _reservation_key(
            workflow_id, workflow_run_id, activity_id, _resolve_attempt(attempt), today, "x_reads"
        )
    
    db = get_db()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
        
        _check_kill_switch(db)
        
        # Same attempt reserving again: reuse its reservation
        if keyed:
            existing = db.query(BudgetReservationDB).filter(
                BudgetReservationDB.reservation_key == reservation_key
            ).first()
            
            if existing:
                _refuse_closed_reservation(existing)
                db.commit()
                return existing.cost_usd, existing.reservation_date
        
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
        
        # Log if we're close to or over the soft cap after reconciliation would occur
        if current_reads > settings.daily_post_read_cap:
            import logging
            logger = logging.getLogger(__name__)
            overage = current_reads - settings.daily_post_read_cap
            logger.warning(
                f"Read cap soft overage: {current_reads:.0f} reads exceeds cap of "
                f"{settings.daily_post_read_cap} by {overage:.0f} (soft cap allows up to one call past limit)"
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
        
        # Record the reservation (idempotent per attempt)
        if keyed:
            reservation = BudgetReservationDB(
                reservation_key=reservation_key,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                activity_id=activity_id,
                reservation_date=today,
                budget_type='x_reads',
                reserved_amount=reads_needed,
                cost_usd=cost,
                created_at=datetime.now(timezone.utc).isoformat(),
                reconciled=0,
            )
            db.add(reservation)
        
        db.commit()
        return cost, today
        
    except (BudgetExceeded, ReservationClosed):
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise
    finally:
        db.close()


def reserve_inference_budget(
    input_tokens: int,
    output_tokens: int,
    workflow_id: Optional[str] = None,
    workflow_run_id: Optional[str] = None,
    activity_id: Optional[str] = None,
    attempt: Optional[int] = None,
    call_index: int = 1,
) -> Tuple[float, str]:
    """
    Atomically reserve inference budget.
    
    Uses per-token pricing from config (NB3).
    Uses BEGIN IMMEDIATE transaction for atomic reserve-then-commit.
    Cap enforced on first call too.
    
    input_tokens and output_tokens are the call's upper bound: the prompt estimate
    and the max output tokens sent with the request (InferenceMeter does this).
    
    If workflow_id, workflow_run_id, and activity_id are provided, the reservation is
    keyed by them plus the activity attempt and call_index (issue #8): one
    reservation per paid call per attempt. The same key reserving again reuses it.
    
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
    total_tokens = input_tokens + output_tokens
    keyed = bool(workflow_id and workflow_run_id and activity_id)
    if keyed:
        reservation_key = _reservation_key(
            workflow_id, workflow_run_id, activity_id, _resolve_attempt(attempt), today,
            "inference", call_index,
        )
    
    db = get_db()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
        
        _check_kill_switch(db)
        
        # Same attempt and call reserving again: reuse its reservation
        if keyed:
            existing = db.query(BudgetReservationDB).filter(
                BudgetReservationDB.reservation_key == reservation_key
            ).first()
            
            if existing:
                _refuse_closed_reservation(existing)
                db.commit()
                return existing.cost_usd, existing.reservation_date
        
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
        
        # Record the reservation (idempotent per attempt and call)
        if keyed:
            reservation = BudgetReservationDB(
                reservation_key=reservation_key,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                activity_id=activity_id,
                reservation_date=today,
                budget_type='inference',
                reserved_amount=total_tokens,
                cost_usd=cost,
                created_at=datetime.now(timezone.utc).isoformat(),
                reconciled=0,
            )
            db.add(reservation)
        
        db.commit()
        return cost, today
        
    except (BudgetExceeded, ReservationClosed):
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
    cost_per_read: float = None,
    attempt: Optional[int] = None,
) -> float:
    """
    Reconcile X budget after actual results known (R5-B1: two-way with idempotency).
    
    - Charges delta when actual > reserved
    - Refunds delta when actual < reserved  
    - Applies to reservation's own UTC day, not today
    - Idempotent per workflow run, activity and attempt (issue #8); `attempt`
      defaults to the current activity attempt, as in reserve_x_budget
    - Never takes a day below zero
    
    Returns the actual cost charged.
    """
    settings = get_settings()
    
    if cost_per_read is None:
        cost_per_read = settings.x_post_read_cost
    
    attempt = _resolve_attempt(attempt)
    actual_cost = actual_reads * cost_per_read
    # One identity for the reservation and its reconciliation (incl. UTC date)
    reconciliation_key = _reservation_key(
        workflow_id, workflow_run_id, activity_id, attempt, reservation_date, "x_reads"
    )
    
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
        if new_reads < 0:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(
                f"Zero floor clamped over-refund: would refund {abs(new_reads):.1f} reads "
                f"from {ledger.x_reads:.1f}, clamping to 0 "
                f"(workflow_id={workflow_id}, activity_id={activity_id})"
            )
        if new_cost < 0:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(
                f"Zero floor clamped over-refund: would refund ${abs(new_cost):.4f} "
                f"from ${ledger.x_cost_usd:.4f}, clamping to $0.00 "
                f"(workflow_id={workflow_id}, activity_id={activity_id})"
            )
        
        ledger.x_reads = max(0.0, new_reads)
        ledger.x_cost_usd = max(0.0, new_cost)
        ledger.last_updated = datetime.now(timezone.utc).isoformat()
        
        # Record reconciliation for idempotency
        reconciliation = BudgetReconciliationDB(
            reconciliation_key=reconciliation_key,
            workflow_id=workflow_id,
            activity_id=activity_id,
            attempt=str(attempt),
            reservation_date=reservation_date,
            reserved_amount=float(reserved_reads),
            actual_amount=float(actual_reads),
            reconciled_at=datetime.now(timezone.utc).isoformat(),
            budget_type='x_reads',
        )
        db.add(reconciliation)
        
        # Mark the reservation as reconciled
        reservation_key_str = reconciliation_key
        reservation = db.query(BudgetReservationDB).filter(
            BudgetReservationDB.reservation_key == reservation_key_str
        ).first()
        if reservation:
            reservation.reconciled = 1
        
        db.commit()
        return actual_cost
        
    except Exception as e:
        db.rollback()
        raise
    finally:
        db.close()


def release_x_reservation(
    reserved_reads: int,
    reservation_date: str,
    workflow_id: str,
    workflow_run_id: str,
    activity_id: str,
    cost_per_read: float = None,
    attempt: Optional[int] = None,
) -> float:
    """Release an X reservation whose request returned no posts (issue #8).

    For an attempt X refused with a 4xx status. X bills per post returned. Reconciles the
    reservation to zero reads. Do not call it for 5xx, timeouts or connection
    errors, where the request may have been served; those stay counted.
    """
    return reconcile_x_reads(
        reserved_reads=reserved_reads,
        actual_reads=0,
        reservation_date=reservation_date,
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        activity_id=activity_id,
        cost_per_read=cost_per_read,
        attempt=attempt,
    )


def reconcile_inference_tokens(
    reserved_input: int,
    reserved_output: int,
    actual_input: int,
    actual_output: int,
    reservation_date: str,
    workflow_id: str,
    workflow_run_id: str,
    activity_id: str,
    attempt: Optional[int] = None,
    call_index: int = 1,
) -> float:
    """
    Reconcile inference budget after actual usage known (R5-B1: two-way with idempotency).
    
    - Charges delta when actual > reserved
    - Refunds delta when actual < reserved
    - Applies to reservation's own UTC day, not today
    - Idempotent per workflow run, activity, attempt and call (issue #8);
      `attempt` defaults to the current activity attempt
    - Never takes a day below zero
    
    Returns the actual cost charged.
    """
    return _close_inference_reservation(
        reserved_input, reserved_output, actual_input, actual_output, reservation_date,
        workflow_id, workflow_run_id, activity_id, attempt, call_index,
    )


def _close_inference_reservation(
    reserved_input: int,
    reserved_output: int,
    actual_input: int,
    actual_output: int,
    reservation_date: str,
    workflow_id: str,
    workflow_run_id: str,
    activity_id: str,
    attempt: Optional[int],
    call_index: int,
) -> float:
    settings = get_settings()
    attempt = _resolve_attempt(attempt)
    
    reserved_cost = (
        reserved_input * settings.inference_input_token_cost_usd +
        reserved_output * settings.inference_output_token_cost_usd
    )
    
    actual_cost = (
        actual_input * settings.inference_input_token_cost_usd +
        actual_output * settings.inference_output_token_cost_usd
    )
    
    # One identity for the reservation and its reconciliation (incl. UTC date)
    reconciliation_key = _reservation_key(
        workflow_id, workflow_run_id, activity_id, attempt, reservation_date,
        "inference", call_index,
    )
    
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
        if new_cost < 0:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(
                f"Zero floor clamped over-refund: would refund ${abs(new_cost):.6f} "
                f"from ${ledger.inference_cost_usd:.6f}, clamping to $0.00 "
                f"(workflow_id={workflow_id}, activity_id={activity_id})"
            )
        
        ledger.inference_cost_usd = max(0.0, new_cost)
        ledger.last_updated = datetime.now(timezone.utc).isoformat()
        
        # Record reconciliation for idempotency
        total_reserved = reserved_input + reserved_output
        total_actual = actual_input + actual_output
        reconciliation = BudgetReconciliationDB(
            reconciliation_key=reconciliation_key,
            workflow_id=workflow_id,
            activity_id=activity_id,
            attempt=str(attempt),
            reservation_date=reservation_date,
            reserved_amount=float(total_reserved),
            actual_amount=float(total_actual),
            reconciled_at=datetime.now(timezone.utc).isoformat(),
            budget_type='inference',
        )
        db.add(reconciliation)
        
        # Mark the reservation as reconciled
        reservation_key_str = reconciliation_key
        reservation = db.query(BudgetReservationDB).filter(
            BudgetReservationDB.reservation_key == reservation_key_str
        ).first()
        if reservation:
            reservation.reconciled = 1
        
        db.commit()
        return actual_cost
        
    except Exception as e:
        db.rollback()
        raise
    finally:
        db.close()


# --- issue #8: per-attempt inference metering, sized from the prompt ---

# Prompt token bound. The GPT tokenizers are byte-level BPE: every token covers
# at least one UTF-8 byte, so a text never has more tokens than bytes, whatever
# it contains (code, punctuation, emoji, adversarial post text). English runs
# about 4 bytes per token, so the bound is about 4x the real count; input is the
# cheap side and reconciliation refunds the difference right after the call.
# Chat format overhead (OpenAI cookbook): tokens per message (role and
# delimiters, the role name counted in full) and the assistant reply priming.
TOKENS_PER_MESSAGE = 4
TOKENS_PER_REPLY = 3


def prompt_token_bound(messages: Sequence[dict]) -> int:
    """Upper bound on the input tokens of a chat request: its UTF-8 bytes plus
    the chat format overhead. A reservation sized from it covers the call, so a
    call admitted under the daily cap cannot cost more input than reserved."""
    total = TOKENS_PER_REPLY
    for message in messages:
        for value in message.values():
            text_value = value if isinstance(value, str) else str(value)
            total += len(text_value.encode("utf-8"))
        total += TOKENS_PER_MESSAGE
    return total


@dataclass
class InferenceCall:
    """One paid inference call's reservation, and how it was closed.

    status: "open" (reserved; stays counted at the reserved amount, also when
    the request failed), "settled" (reconciled to the provider's usage) or
    "kept" (no usable usage reported, reservation kept).
    """

    meter: "InferenceMeter"
    call_index: int
    reserved_input: int
    reserved_output: int
    reserved_cost: float
    reservation_date: str
    status: str = "open"
    cost_usd: float = 0.0

    def settle(self, actual_input: Optional[int], actual_output: Optional[int]) -> float:
        """Reconcile to the provider's reported usage. Missing or partial usage
        (either side None or not an int, or 0/0) keeps the full reservation as
        spent (R5-B1). Returns the cost counted."""
        if self.status != "open":
            return self.cost_usd
        valid = all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 0
            for v in (actual_input, actual_output)
        )
        if not valid or (actual_input == 0 and actual_output == 0):
            self.status = "kept"
            self.cost_usd = self.reserved_cost
            return self.cost_usd
        m = self.meter
        self.cost_usd = reconcile_inference_tokens(
            reserved_input=self.reserved_input,
            reserved_output=self.reserved_output,
            actual_input=actual_input,
            actual_output=actual_output,
            reservation_date=self.reservation_date,
            workflow_id=m.workflow_id,
            workflow_run_id=m.workflow_run_id,
            activity_id=m.activity_id,
            attempt=m.attempt,
            call_index=self.call_index,
        )
        self.status = "settled"
        return self.cost_usd


@dataclass
class InferenceMeter:
    """Meters the paid inference calls one activity attempt makes (issue #8).

    The adapter that makes a paid call reserves through the current meter
    right before sending the request, with the prompt estimate and the max
    output tokens it sends, then settles the call. Each call gets
    its own reservation key (attempt + call index), so a retried attempt or a
    second call in one attempt is counted, never folded into an earlier one.
    A call that fails keeps its reservation counted.
    """

    workflow_id: str
    workflow_run_id: str
    activity_id: str
    attempt: int
    calls: List[InferenceCall] = field(default_factory=list)

    def reserve(self, input_tokens: int, max_output_tokens: int) -> InferenceCall:
        """Reserve one call. Raises BudgetExceeded (kill switch or daily cap)."""
        call_index = len(self.calls) + 1
        cost, reservation_date = reserve_inference_budget(
            input_tokens=input_tokens,
            output_tokens=max_output_tokens,
            workflow_id=self.workflow_id,
            workflow_run_id=self.workflow_run_id,
            activity_id=self.activity_id,
            attempt=self.attempt,
            call_index=call_index,
        )
        call = InferenceCall(
            meter=self,
            call_index=call_index,
            reserved_input=input_tokens,
            reserved_output=max_output_tokens,
            reserved_cost=cost,
            reservation_date=reservation_date,
            cost_usd=cost,
        )
        self.calls.append(call)
        return call

    @property
    def cost_usd(self) -> float:
        """Cost counted so far: actual for settled calls, reserved for open or kept."""
        return sum(c.cost_usd for c in self.calls)

    def summary(self) -> str:
        if not self.calls:
            return "no paid inference calls"
        statuses = ", ".join(c.status for c in self.calls)
        return f"{len(self.calls)} inference call(s) [{statuses}], cost ${self.cost_usd:.6f}"


_current_meter: ContextVar[Optional[InferenceMeter]] = ContextVar("inference_meter", default=None)


def current_inference_meter() -> Optional[InferenceMeter]:
    """The meter of the activity attempt running in this context, or None.

    None outside metered activities (adapter unit tests, offline scripts):
    the call is then not budgeted, as before issue #8.
    """
    return _current_meter.get()


@contextmanager
def meter_inference() -> Iterator[InferenceMeter]:
    """Meter the paid inference calls made inside this block for the current
    activity attempt. Must run inside a Temporal activity, once per attempt
    block: call indices restart in each meter, so meters do not nest (a second
    sequential meter in one attempt would collide on call 1 and is refused by
    ReservationClosed once the first call is closed)."""
    if _current_meter.get() is not None:
        raise RuntimeError("meter_inference() does not nest")
    info = activity.info()
    meter = InferenceMeter(
        workflow_id=info.workflow_id,
        workflow_run_id=info.workflow_run_id,
        activity_id=info.activity_id,
        attempt=info.attempt,
    )
    token = _current_meter.set(meter)
    try:
        yield meter
    finally:
        _current_meter.reset(token)


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
