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
            
            # Clear engine cache
            import backend.database
            backend.database._engine = None
            backend.database._SessionLocal = None
            
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
    """Test that cap is enforced on the very first call (NB3)."""
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
        
        # Clear engine cache
        import backend.database
        backend.database._engine = None
        backend.database._SessionLocal = None
        
        from backend.database import init_db
        init_db()
        
        reserve_x_budget(5)
        
        with pytest.raises(BudgetExceeded, match="exceed daily read cap"):
            reserve_x_budget(6)


def test_reserve_x_budget_atomic(temp_db):
    """Test that reserve is atomic - failure doesn't change ledger."""
    with patch("backend.config.get_settings") as mock_settings:
        settings = Settings(
            database_url=f"sqlite:///{temp_db}",
            daily_spend_limit_usd=1.0,
            daily_post_read_cap=100,
            x_post_read_cost=0.5,
            budget_kill_switch=False,
        )
        mock_settings.return_value = settings
        
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
    with patch("backend.config.get_settings") as mock_settings:
        settings = Settings(
            database_url=f"sqlite:///{temp_db}",
            daily_spend_limit_usd=10.0,
            daily_post_read_cap=100,
            x_post_read_cost=0.005,
            budget_kill_switch=True,
        )
        mock_settings.return_value = settings
        
        with pytest.raises(BudgetExceeded, match="kill switch enabled in config"):
            reserve_x_budget(1)


def test_reserve_x_budget_kill_switch_db(temp_db):
    """Test that DB kill switch blocks budget."""
    with patch("backend.config.get_settings") as mock_settings:
        settings = Settings(
            database_url=f"sqlite:///{temp_db}",
            daily_spend_limit_usd=10.0,
            daily_post_read_cap=100,
            x_post_read_cost=0.005,
            budget_kill_switch=False,
        )
        mock_settings.return_value = settings
        
        set_kill_switch(True)
        
        with pytest.raises(BudgetExceeded, match="kill switch enabled in database"):
            reserve_x_budget(1)


def test_reserve_inference_budget_per_token_pricing(temp_db):
    """Test per-token pricing for inference (NB3)."""
    with patch("backend.config.get_settings") as mock_settings:
        settings = Settings(
            database_url=f"sqlite:///{temp_db}",
            daily_inference_limit_usd=1.0,
            inference_input_token_cost_usd=0.001,
            inference_output_token_cost_usd=0.002,
            budget_kill_switch=False,
        )
        mock_settings.return_value = settings
        
        cost = reserve_inference_budget(input_tokens=100, output_tokens=50)
        
        expected_cost = (100 * 0.001) + (50 * 0.002)
        assert abs(cost - expected_cost) < 0.0001


def test_reserve_inference_budget_enforces_cap_first_call(temp_db):
    """Test that inference budget cap is enforced on first call."""
    with patch("backend.config.get_settings") as mock_settings:
        settings = Settings(
            database_url=f"sqlite:///{temp_db}",
            daily_inference_limit_usd=0.01,
            inference_input_token_cost_usd=0.001,
            inference_output_token_cost_usd=0.002,
            budget_kill_switch=False,
        )
        mock_settings.return_value = settings
        
        with pytest.raises(BudgetExceeded, match="exceed daily inference limit"):
            reserve_inference_budget(input_tokens=100, output_tokens=100)


def test_reserve_inference_budget_atomic(temp_db):
    """Test that inference reserve is atomic."""
    with patch("backend.config.get_settings") as mock_settings:
        settings = Settings(
            database_url=f"sqlite:///{temp_db}",
            daily_inference_limit_usd=0.02,
            inference_input_token_cost_usd=0.001,
            inference_output_token_cost_usd=0.002,
            budget_kill_switch=False,
        )
        mock_settings.return_value = settings
        
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
    with patch("backend.config.get_settings") as mock_settings:
        settings = Settings(
            database_url=f"sqlite:///{temp_db}",
            daily_inference_limit_usd=1.0,
            inference_input_token_cost_usd=0.001,
            inference_output_token_cost_usd=0.002,
            budget_kill_switch=True,
        )
        mock_settings.return_value = settings
        
        with pytest.raises(BudgetExceeded, match="kill switch enabled"):
            reserve_inference_budget(input_tokens=10, output_tokens=10)


def test_get_today_budget_includes_kill_switch(temp_db):
    """Test that get_today_budget includes kill switch status."""
    with patch("backend.config.get_settings") as mock_settings:
        settings = Settings(
            database_url=f"sqlite:///{temp_db}",
            daily_spend_limit_usd=10.0,
            daily_post_read_cap=100,
            daily_inference_limit_usd=5.0,
            budget_kill_switch=False,
        )
        mock_settings.return_value = settings
        
        budget = get_today_budget()
        assert "kill_switch" in budget
        assert budget["kill_switch"] is False
        
        set_kill_switch(True)
        
        budget = get_today_budget()
        assert budget["kill_switch"] is True


def test_multiple_reserves_accumulate(temp_db):
    """Test that multiple reserves accumulate correctly."""
    with patch("backend.config.get_settings") as mock_settings:
        settings = Settings(
            database_url=f"sqlite:///{temp_db}",
            daily_spend_limit_usd=10.0,
            daily_post_read_cap=100,
            x_post_read_cost=0.1,
            budget_kill_switch=False,
        )
        mock_settings.return_value = settings
        
        reserve_x_budget(10, 0.1)
        reserve_x_budget(20, 0.1)
        
        budget = get_today_budget()
        assert budget["x_reads"] == 30
        assert abs(budget["x_cost_usd"] - 3.0) < 0.01
