"""BriefSuggesterPort - propose brief sentences from the product page (issue #42).

One model call: given the founder's brief (as numbered sentences) and the
page text, the model proposes at most MAX_SUGGESTIONS sentences the brief
lacks, each with the exact page quote that supports it and why it helps
replies, and flags brief sentences the page contradicts or does not mention.

The answer is a proposal. Code (backend/brief_suggestions.py) keeps a
suggestion only if its quote appears verbatim in the page text, keeps a flag
only if it names a brief sentence that exists, and nothing reaches the brief
until the founder ticks it in the form.

Budget (issue #8): an adapter that makes a paid call reserves it through
backend.budget.current_inference_meter(); the API opens the meter
(backend.budget.meter_api_inference).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from backend.models.written import BriefSentence

MAX_SUGGESTIONS = 8


@dataclass(frozen=True)
class SuggestRequest:
    """Everything the model sees: the page text (untrusted), the brief
    sentences and the product name."""

    page_text: str
    page_source: str  # "github_readme" or "web_page"
    brief_sentences: List[BriefSentence]
    product_name: Optional[str] = None


@dataclass(frozen=True)
class RawSuggestion:
    """One proposed sentence as the model wrote it; not checked yet."""

    sentence: str
    quote: str
    why: str


@dataclass(frozen=True)
class RawFlag:
    """A brief sentence the model says the page contradicts or does not
    mention; not checked yet. kind: "contradicted" or "not_mentioned"."""

    brief_sentence_id: str
    kind: str
    note: str = ""
    quote: str = ""


@dataclass(frozen=True)
class SuggestResponse:
    suggestions: List[RawSuggestion] = field(default_factory=list)
    flags: List[RawFlag] = field(default_factory=list)
    model_used: str = ""
    # True when the answer was not the JSON shape asked for (nothing usable)
    malformed: bool = False


class BriefSuggesterPort(ABC):
    """Port for the brief-suggestion model call."""

    @abstractmethod
    async def suggest(self, request: SuggestRequest) -> SuggestResponse:
        """Return the model's proposals. Must not raise on malformed model
        output: return what was parsed (malformed=True when nothing was).
        Output cut off at the max output tokens may raise (issue #8)."""
