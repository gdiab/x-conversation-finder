"""Tests for config validation."""

import pytest
from pydantic import ValidationError
from backend.config import Settings


def test_poll_interval_fake_adapter_allows_zero():
    """Fake adapter can use 0-hour poll interval for tests."""
    settings = Settings(
        x_client_adapter='fake',
        watch_poll_interval_hours=0
    )
    assert settings.watch_poll_interval_hours == 0


def test_poll_interval_real_adapter_rejects_zero():
    """Real adapter must use at least 1-hour poll interval."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            x_client_adapter='real',
            watch_poll_interval_hours=0
        )
    assert "must be at least 1" in str(exc_info.value)


def test_poll_interval_real_adapter_accepts_one():
    """Real adapter accepts 1+ hour poll interval."""
    settings = Settings(
        x_client_adapter='real',
        watch_poll_interval_hours=1
    )
    assert settings.watch_poll_interval_hours == 1


def test_poll_interval_no_adapter_specified_uses_default_fake():
    """When adapter not specified, it defaults to 'fake' and allows 0 hours."""
    settings = Settings(
        watch_poll_interval_hours=0
    )
    # Default adapter is 'fake' per settings definition
    assert settings.x_client_adapter == 'fake'
    assert settings.watch_poll_interval_hours == 0
