"""Ports (interfaces) for external dependencies."""

from .reply_agent import ReplyAgentPort
from .x_client import SearchResult, XClientPort

__all__ = ["ReplyAgentPort", "SearchResult", "XClientPort"]
