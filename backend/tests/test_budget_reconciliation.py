"""
Tests for budget reconciliation (R4-B2).

Verify that reserved budgets are reconciled to actual usage.
"""

import pytest
from unittest.mock import patch
import tempfile
from pathlib import Path

from backend.config import Settings
from backend.budget import (
    reserve_x_budget,
    reconcile_x_reads,
    reserve_inference_budget,
    reconcile_inference_tokens,
    get_today_budget,
)


@pytest.fixture
def temp_db_reconcile():
    """Create a temporary database for reconciliation tests."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    
    settings = Settings(
        database_url=f"sqlite:///{db_path.absolute()}",
        daily_spend_limit_usd=100.0,
        daily_post_read_cap=1000,
        daily_inference_limit_usd=50.0,
        x_post_read_cost=0.005,
        inference_input_token_cost_usd=0.00000015,
        inference_output_token_cost_usd=0.0000006,
    )
    
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.budget.get_settings", return_value=settings), \
         patch("backend.database.get_settings", return_value=settings):
        
        # Clear engine cache
        import backend.database
        backend.database._engine = None
        backend.database._SessionLocal = None
        
        from backend.database import init_db
        init_db()
        
        yield settings
    
    db_path.unlink(missing_ok=True)


def test_reconcile_x_reads_refunds_unused(temp_db_reconcile):
    """R4-B2: Reconcile refunds unused X reads."""
    # Reserve 10, use 5
    cost_reserved = reserve_x_budget(10, 0.005)
    assert cost_reserved == 0.05
    
    budget = get_today_budget()
    assert budget["x_reads"] == 10
    assert budget["x_cost_usd"] == 0.05
    
    # Reconcile to actual
    cost_actual = reconcile_x_reads(10, 5, 0.005)
    assert cost_actual == 0.025
    
    # Check ledger was updated
    budget = get_today_budget()
    assert budget["x_reads"] == 5, "Should refund 5 unused reads"
    assert budget["x_cost_usd"] == 0.025, "Should refund $0.025"


def test_reconcile_x_reads_no_refund_when_equal(temp_db_reconcile):
    """R4-B2: No refund when actual equals reserved."""
    reserve_x_budget(10, 0.005)
    
    cost_actual = reconcile_x_reads(10, 10, 0.005)
    assert cost_actual == 0.05
    
    budget = get_today_budget()
    assert budget["x_reads"] == 10
    assert budget["x_cost_usd"] == 0.05


def test_reconcile_inference_tokens_refunds_unused(temp_db_reconcile):
    """R4-B2: Reconcile refunds unused inference tokens."""
    # Reserve 1000 input / 500 output
    cost_reserved = reserve_inference_budget(1000, 500)
    expected_reserved = (1000 * 0.00000015) + (500 * 0.0000006)
    assert abs(cost_reserved - expected_reserved) < 1e-9
    
    budget = get_today_budget()
    assert budget["inference_calls"] == 1
    assert abs(budget["inference_cost_usd"] - expected_reserved) < 1e-9
    
    # Reconcile to actual (600 input / 300 output)
    cost_actual = reconcile_inference_tokens(1000, 500, 600, 300)
    expected_actual = (600 * 0.00000015) + (300 * 0.0000006)
    assert abs(cost_actual - expected_actual) < 1e-9
    
    # Check ledger was updated
    budget = get_today_budget()
    assert budget["inference_calls"] == 1  # Calls don't change
    assert abs(budget["inference_cost_usd"] - expected_actual) < 1e-9, "Should refund unused tokens"


def test_reconcile_inference_tokens_no_refund_when_more(temp_db_reconcile):
    """R4-B2: No refund when actual exceeds reserved."""
    reserve_inference_budget(100, 50)
    
    # Actual exceeds reserved
    cost_actual = reconcile_inference_tokens(100, 50, 200, 100)
    expected = (200 * 0.00000015) + (100 * 0.0000006)
    assert abs(cost_actual - expected) < 1e-9
    
    # Ledger should still show original reservation (no negative refund)
    budget = get_today_budget()
    original = (100 * 0.00000015) + (50 * 0.0000006)
    assert abs(budget["inference_cost_usd"] - original) < 1e-9


def test_multiple_reconciliations_accumulate(temp_db_reconcile):
    """R4-B2: Multiple reconciliations accumulate correctly."""
    # First operation: reserve 10, use 7
    reserve_x_budget(10, 0.005)
    reconcile_x_reads(10, 7, 0.005)
    
    # Second operation: reserve 10, use 6
    reserve_x_budget(10, 0.005)
    reconcile_x_reads(10, 6, 0.005)
    
    budget = get_today_budget()
    assert budget["x_reads"] == 13, "Should have 7 + 6 = 13 reads"
    assert budget["x_cost_usd"] == 0.065, "Should have $0.035 + $0.030 = $0.065"
