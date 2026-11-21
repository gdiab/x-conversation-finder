"""
Tests for budget system.

Per NB3: Atomic reserve-then-commit, cap enforced on first call,
BudgetExceeded exception, per-token pricing, kill switch.
"""

import pytest
from unittest.mock import patch
import tempfile
import os

from backend.budget import (
    reserve_x_budget,
    reserve_inference_budget,
    get_today_budget,
    set_kill_switch,
    BudgetExceeded,
)
from backend.config import Settings


@pytest.fixture(scope="function")
def temp_db():
    """Create a fresh temporary database for each test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    
    yield path
    
    try:
        os.unlink(path)
    except:
        pass


@pytest.fixture(scope="function")
def budget_test_db(temp_db):
    """Setup budget test with proper DB initialization and patching."""
    from contextlib import contextmanager
    
    @contextmanager
    def _setup(**settings_kwargs):
        defaults = {
            "database_url": f"sqlite:///{temp_db}",
            "daily_spend_limit_usd": 100.0,
            "daily_post_read_cap": 1000,
            "x_post_read_cost": 0.005,
            "budget_kill_switch": False,
        }
        defaults.update(settings_kwargs)
        settings = Settings(**defaults)
        
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.budget.get_settings", return_value=settings), \
         patch("backend.database.get_settings", return_value=settings):
            
            from backend.database import init_db
            init_db()
            
            yield settings
    
    return _setup


def with_test_settings(temp_db, **kwargs):
    """Helper to create test settings and patch them."""
    settings = Settings(
        database_url=f"sqlite:///{temp_db}",
        **kwargs
    )
    return patch.multiple(
        "backend.config",
        get_settings=lambda: settings,
    ), patch.multiple(
        "backend.budget",
        get_settings=lambda: settings,
    ), patch.multiple(
        "backend.database",
        get_settings=lambda: settings,
    )


def test_reserve_x_budget_first_call_enforces_cap(temp_db):
    """
    Test that cap is enforced on the very first call (NB3).
    
    QA R15: Fixed test isolation - explicitly clear kill-switch state
    to prevent test order dependency (fails with pytest-randomly seeds 5, 6, 12).
    """
    settings = Settings(
        database_url=f"sqlite:///{temp_db}",
        daily_spend_limit_usd=1.0,
        daily_post_read_cap=100,
        x_post_read_cost=0.5,
        budget_kill_switch=False,
    )
    
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.budget.get_settings", return_value=settings), \
         patch("backend.database.get_settings", return_value=settings):
        
        from backend.database import init_db
        init_db()
        
        with pytest.raises(BudgetExceeded, match="exceed daily spend limit"):
            reserve_x_budget(reads_needed=3, cost_per_read=0.5)


def test_reserve_x_budget_read_cap_enforced(temp_db):
    """Test that read cap is enforced."""
    settings = Settings(
        database_url=f"sqlite:///{temp_db}",
        daily_spend_limit_usd=100.0,
        daily_post_read_cap=10,
        x_post_read_cost=0.005,
        budget_kill_switch=False,
    )
    
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.budget.get_settings", return_value=settings), \
         patch("backend.database.get_settings", return_value=settings):
        
        from backend.database import init_db
        init_db()
        
        reserve_x_budget(5)
        
        with pytest.raises(BudgetExceeded, match="exceed daily read cap"):
            reserve_x_budget(6)


def test_reserve_x_budget_atomic(temp_db):
    """Test that reserve is atomic - failure doesn't change ledger."""
    settings = Settings(

            database_url=f"sqlite:///{temp_db}",
            daily_spend_limit_usd=1.0,
            daily_post_read_cap=100,
            x_post_read_cost=0.5,
            budget_kill_switch=False,
        
    )
    
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.budget.get_settings", return_value=settings), \
         patch("backend.database.get_settings", return_value=settings):
        
        from backend.database import init_db
        init_db()
        
        reserve_x_budget(1, 0.5)
        
        try:
            reserve_x_budget(2, 0.5)
        except BudgetExceeded:
            pass
        
        budget = get_today_budget()
        assert budget["x_reads"] == 1
        assert budget["x_cost_usd"] == 0.5


def test_reserve_x_budget_kill_switch_config(temp_db):
    """Test that config kill switch blocks budget."""
    settings = Settings(

            database_url=f"sqlite:///{temp_db}",
            daily_spend_limit_usd=10.0,
            daily_post_read_cap=100,
            x_post_read_cost=0.005,
            budget_kill_switch=True,
        
    )
    
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.budget.get_settings", return_value=settings), \
         patch("backend.database.get_settings", return_value=settings):
        
        from backend.database import init_db
        init_db()
        
        with pytest.raises(BudgetExceeded, match="kill switch enabled in config"):
            reserve_x_budget(1)


def test_reserve_x_budget_kill_switch_db(temp_db):
    """Test that DB kill switch blocks budget."""
    settings = Settings(

            database_url=f"sqlite:///{temp_db}",
            daily_spend_limit_usd=10.0,
            daily_post_read_cap=100,
            x_post_read_cost=0.005,
            budget_kill_switch=False,
        
    )
    
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.budget.get_settings", return_value=settings), \
         patch("backend.database.get_settings", return_value=settings):
        
        from backend.database import init_db
        init_db()
        
        set_kill_switch(True)
        
        with pytest.raises(BudgetExceeded, match="kill switch enabled in database"):
            reserve_x_budget(1)


def test_reserve_inference_budget_per_token_pricing(temp_db):
    """Test per-token pricing for inference (NB3)."""
    settings = Settings(

            database_url=f"sqlite:///{temp_db}",
            daily_inference_limit_usd=1.0,
            inference_input_token_cost_usd=0.001,
            inference_output_token_cost_usd=0.002,
            budget_kill_switch=False,
        
    )
    
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.budget.get_settings", return_value=settings), \
         patch("backend.database.get_settings", return_value=settings):
        
        from backend.database import init_db
        init_db()
        
        cost, reservation_date = reserve_inference_budget(input_tokens=100, output_tokens=50)
        
        expected_cost = (100 * 0.001) + (50 * 0.002)
        assert abs(cost - expected_cost) < 0.0001
        assert reservation_date is not None  # R5-B1: returns date


def test_reserve_inference_budget_enforces_cap_first_call(temp_db):
    """Test that inference budget cap is enforced on first call."""
    settings = Settings(

            database_url=f"sqlite:///{temp_db}",
            daily_inference_limit_usd=0.01,
            inference_input_token_cost_usd=0.001,
            inference_output_token_cost_usd=0.002,
            budget_kill_switch=False,
        
    )
    
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.budget.get_settings", return_value=settings), \
         patch("backend.database.get_settings", return_value=settings):
        
        from backend.database import init_db
        init_db()
        
        with pytest.raises(BudgetExceeded, match="exceed daily inference limit"):
            reserve_inference_budget(input_tokens=100, output_tokens=100)


def test_reserve_inference_budget_atomic(temp_db):
    """Test that inference reserve is atomic."""
    settings = Settings(

            database_url=f"sqlite:///{temp_db}",
            daily_inference_limit_usd=0.02,
            inference_input_token_cost_usd=0.001,
            inference_output_token_cost_usd=0.002,
            budget_kill_switch=False,
        
    )
    
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.budget.get_settings", return_value=settings), \
         patch("backend.database.get_settings", return_value=settings):
        
        from backend.database import init_db
        init_db()
        
        reserve_inference_budget(input_tokens=5, output_tokens=5)
        
        try:
            reserve_inference_budget(input_tokens=100, output_tokens=100)
        except BudgetExceeded:
            pass
        
        budget = get_today_budget()
        assert budget["inference_calls"] == 1
        assert abs(budget["inference_cost_usd"] - 0.015) < 0.0001


def test_reserve_inference_budget_kill_switch(temp_db):
    """Test that kill switch blocks inference."""
    settings = Settings(

            database_url=f"sqlite:///{temp_db}",
            daily_inference_limit_usd=1.0,
            inference_input_token_cost_usd=0.001,
            inference_output_token_cost_usd=0.002,
            budget_kill_switch=True,
        
    )
    
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.budget.get_settings", return_value=settings), \
         patch("backend.database.get_settings", return_value=settings):
        
        from backend.database import init_db
        init_db()
        
        with pytest.raises(BudgetExceeded, match="kill switch enabled"):
            reserve_inference_budget(input_tokens=10, output_tokens=10)


def test_get_today_budget_includes_kill_switch(temp_db):
    """Test that get_today_budget includes kill switch status."""
    settings = Settings(

            database_url=f"sqlite:///{temp_db}",
            daily_spend_limit_usd=10.0,
            daily_post_read_cap=100,
            daily_inference_limit_usd=5.0,
            budget_kill_switch=False,
        
    )
    
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.budget.get_settings", return_value=settings), \
         patch("backend.database.get_settings", return_value=settings):
        
        from backend.database import init_db
        init_db()
        
        budget = get_today_budget()
        assert "kill_switch" in budget
        assert budget["kill_switch"] is False
        
        set_kill_switch(True)
        
        budget = get_today_budget()
        assert budget["kill_switch"] is True


def test_multiple_reserves_accumulate(temp_db):
    """Test that multiple reserves accumulate correctly."""
    settings = Settings(

            database_url=f"sqlite:///{temp_db}",
            daily_spend_limit_usd=10.0,
            daily_post_read_cap=100,
            x_post_read_cost=0.1,
            budget_kill_switch=False,
        
    )
    
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.budget.get_settings", return_value=settings), \
         patch("backend.database.get_settings", return_value=settings):
        
        from backend.database import init_db
        init_db()
        
        reserve_x_budget(10, 0.1)
        reserve_x_budget(20, 0.1)
        
        budget = get_today_budget()
        assert budget["x_reads"] == 30
        assert abs(budget["x_cost_usd"] - 3.0) < 0.01


def test_concurrent_budget_no_overspend(temp_db):
    """
    Test R3-B3: Truly concurrent budget access proves no overspend.
    
    Multiple threads try to reserve budget concurrently. The atomic 
    BEGIN IMMEDIATE transaction ensures cap is enforced even under concurrency.
    """
    import threading
    import time
    
    settings = Settings(
        database_url=f"sqlite:///{temp_db}",
        daily_spend_limit_usd=1.0,  # Very low limit
        daily_post_read_cap=100,
        x_post_read_cost=0.5,  # Each read costs 0.5, so 2 reads = 1.0 = cap
        budget_kill_switch=False,
    )
    
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.budget.get_settings", return_value=settings), \
         patch("backend.database.get_settings", return_value=settings):
        
        from backend.database import init_db
        init_db()
        
        # Track successes and failures
        results = {"succeeded": 0, "exceeded": 0, "lock": threading.Lock()}
        
        def try_reserve():
            """Attempt to reserve 1 read (0.5 USD)."""
            try:
                reserve_x_budget(1, 0.5)
                with results["lock"]:
                    results["succeeded"] += 1
            except BudgetExceeded:
                with results["lock"]:
                    results["exceeded"] += 1
        
        # Launch 10 threads all trying to reserve at once
        threads = []
        for _ in range(10):
            t = threading.Thread(target=try_reserve)
            threads.append(t)
        
        # Start all threads nearly simultaneously
        for t in threads:
            t.start()
        
        # Wait for all to complete
        for t in threads:
            t.join()
        
        # With cap of 1.0 USD and cost 0.5 per read, only 2 should succeed
        assert results["succeeded"] == 2, f"Expected 2 successes, got {results['succeeded']}"
        assert results["exceeded"] == 8, f"Expected 8 exceeded, got {results['exceeded']}"
        
        # Verify final budget doesn't exceed cap
        budget = get_today_budget()
        assert budget["x_reads"] == 2
        assert budget["x_cost_usd"] <= settings.daily_spend_limit_usd
