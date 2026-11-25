"""Adapters for external dependencies."""

from .fake_reply_agent import FakeReplyAgent
from .plain_reply_agent import PlainReplyAgent
from .fake_claim_checker import FakeClaimChecker
from .plain_claim_checker import PlainClaimChecker
from .fake_x_client import FakeXClient
from .http_x_client import HttpXClient

__all__ = [
    "FakeReplyAgent",
    "PlainReplyAgent",
    "FakeClaimChecker",
    "PlainClaimChecker",
    "FakeXClient",
    "HttpXClient",
]
