"""Fake brief suggester for tests and the fake-adapter app (issue #42). No API calls.

With a scripted response it returns that response (tests use this to send
quotes that are not in the page). Without one it is deterministic: each page
line of at least four words that the brief does not contain becomes a
suggestion quoting that line, and a brief sentence none of whose words of
four or more letters appear on the page is flagged as not mentioned.
"""

import re
from typing import List, Optional

from backend.ports.brief_suggester import (
    MAX_SUGGESTIONS,
    BriefSuggesterPort,
    RawFlag,
    RawSuggestion,
    SuggestRequest,
    SuggestResponse,
)

_WORD_RE = re.compile(r"[A-Za-z0-9]{4,}")


class FakeBriefSuggester(BriefSuggesterPort):
    def __init__(self, scripted: Optional[SuggestResponse] = None):
        self.scripted = scripted
        self.requests: List[SuggestRequest] = []

    async def suggest(self, request: SuggestRequest) -> SuggestResponse:
        self.requests.append(request)
        if self.scripted is not None:
            return self.scripted
        brief_text = " ".join(b.text for b in request.brief_sentences).casefold()
        suggestions: List[RawSuggestion] = []
        for line in request.page_text.splitlines():
            line = line.strip().lstrip("#-* ").strip()
            if len(line.split()) < 4 or line.casefold() in brief_text:
                continue
            suggestions.append(RawSuggestion(
                sentence=line[:200],
                quote=line[:200],
                why="Fake suggester: a page line the brief does not contain.",
            ))
            if len(suggestions) == MAX_SUGGESTIONS:
                break
        page_words = {w.casefold() for w in _WORD_RE.findall(request.page_text)}
        flags = [
            RawFlag(brief_sentence_id=b.id, kind="not_mentioned", note="Fake suggester: no word of it is on the page.")
            for b in request.brief_sentences
            if not {w.casefold() for w in _WORD_RE.findall(b.text)} & page_words
        ]
        return SuggestResponse(suggestions=suggestions, flags=flags, model_used="fake-suggester")
