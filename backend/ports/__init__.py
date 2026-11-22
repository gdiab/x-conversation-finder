"""Ports (interfaces) for external dependencies."""

from .reply_agent import ReplyAgentPort
from .x_client import XClientPort

__all__ = ["ReplyAgentPort", "XClientPort"]
