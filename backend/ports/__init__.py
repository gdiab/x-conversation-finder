"""Ports (interfaces) for external dependencies."""

from .claim_checker import ClaimCheckerPort
from .reply_agent import ReplyAgentPort
from .x_client import SearchResult, XClientPort

__all__ = ["ClaimCheckerPort", "ReplyAgentPort", "SearchResult", "XClientPort"]
