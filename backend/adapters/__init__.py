"""Adapters for external dependencies."""

from .fake_reply_agent import FakeReplyAgent
from .plain_reply_agent import PlainReplyAgent
from .fake_x_client import FakeXClient
from .http_x_client import HttpXClient

__all__ = ["FakeReplyAgent", "PlainReplyAgent", "FakeXClient", "HttpXClient"]
