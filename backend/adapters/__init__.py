"""Adapters for external dependencies."""

from .fake_reply_agent import FakeReplyAgent
from .plain_reply_agent import PlainReplyAgent
from .fake_x_client import FakeXClient

__all__ = ["FakeReplyAgent", "PlainReplyAgent", "FakeXClient"]
