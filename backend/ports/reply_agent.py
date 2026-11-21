"""
ReplyAgentPort - abstraction for reply generation.

Per ADR-0003: the Agent Kit sits behind this port.
The plain activity adapter is the baseline and default.
"""

from abc import ABC, abstractmethod
from typing import Optional

from backend.models import Evidence, Post


class ReplyAgentPort(ABC):
    """Port for reply scoring and drafting."""
    
    @abstractmethod
    async def research(
        self,
        post: Post,
        topic: str,
        product_brief: str,
        idempotency_key: str,
    ) -> Evidence:
        """
        Research a post and gather evidence.
        
        Returns Evidence with:
        - matched_topic
        - why_it_fits
        - confidence_inputs (raw features)
        - founder_materials
        - computed confidence_score
        """
        pass
    
    @abstractmethod
    async def draft(
        self,
        evidence: Evidence,
        steer: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Evidence:
        """
        Draft a reply based on evidence.
        
        Updates evidence.draft with:
        - text
        - claim_gate result
        - steers_applied
        
        Returns updated evidence.
        """
        pass
