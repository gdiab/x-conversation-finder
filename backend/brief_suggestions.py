"""Suggest brief additions from the product page (issue #42).

The Start Watch form's "Suggest brief additions" button calls the API, which
runs suggest_brief_additions() in the API process (not a workflow, so nothing
here enters Temporal history):

1. Code checks the URL (https, a site) and reads the page through the
   PageFetcherPort (for a GitHub repository, its README; the SSRF guard is in
   the HTTP adapter).
2. One model call through the BriefSuggesterPort, metered in the daily
   inference ledger (backend.budget.meter_api_inference). Its worst-case cost
   is shown on the form before the click (cost_estimate()).
3. Code keeps a suggestion only if its quote appears word for word in the page
   text (backend.gate.reply_fit.quote_in_text), its sentence is one short line
   and the brief does not already hold it; at most MAX_SUGGESTIONS are kept.
   A flag is kept only if it names an existing brief sentence, and a
   "contradicted" flag only with a quote that is on the page.
4. The result goes back to the form. Nothing is stored: not the page text,
   not the suggestions. The brief changes only when the founder ticks
   suggestions and clicks "Add checked to brief" (in the browser).

The page text is untrusted: it is never logged, and no error message quotes it.
"""

from typing import List, Optional

from backend.adapters.plain_brief_suggester import (
    MARKER_HEX_CHARS,
    build_suggest_messages,
)
from backend.budget import meter_api_inference, prompt_token_bound
from backend.config import get_settings
from backend.gate.brief import render_brief_sentence, split_brief
from backend.gate.product_link import product_url_error
from backend.gate.reply_fit import quote_in_text
from backend.ports.brief_suggester import (
    MAX_SUGGESTIONS,
    BriefSuggesterPort,
    SuggestRequest,
)
from backend.ports.page_fetcher import URL_INVALID, PageFetchError, PageFetcherPort

SUGGEST_METER_CALL = "brief-suggestions"

# The brief as the prompt shows it (its numbered sentences with their
# headings), in UTF-8 bytes. It bounds the cost shown before the click.
MAX_BRIEF_BYTES = 20000
# The product name as the prompt shows it (backend/api.py allows 80 characters)
MAX_PRODUCT_NAME_CHARS = 80

MAX_SENTENCE_CHARS = 300
MAX_QUOTE_CHARS = 400
MAX_WHY_CHARS = 300
MAX_NOTE_CHARS = 300
# A quote shorter than this proves little ("the", "fast")
MIN_QUOTE_WORDS = 3

FLAG_KINDS = ("contradicted", "not_mentioned")


class BriefSuggestionError(Exception):
    """A request the service refuses before or instead of the model call.
    `code` is what the API and UI switch on; the message is for the founder."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


BRIEF_TOO_LONG = "brief_too_long"
MODEL_ANSWER_UNUSABLE = "model_answer_unusable"


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _collapsed_casefold(value: str) -> str:
    return _one_line(value).casefold()


def cost_estimate() -> dict:
    """The worst-case cost of one suggestion call, shown before the click.

    It is sized as the reservation is (prompt_token_bound: UTF-8 bytes of the
    prompt, plus the max output tokens): the prompt with the longest product
    name in 4-byte characters and an empty page and brief, plus the page and
    the shown brief at their caps. The call is never reserved at more.
    """
    settings = get_settings()
    longest_name = "\U0001F600" * MAX_PRODUCT_NAME_CHARS
    empty = SuggestRequest(page_text="", page_source="web_page", brief_sentences=[], product_name=longest_name)
    template = prompt_token_bound(build_suggest_messages(empty, "0" * MARKER_HEX_CHARS))
    input_tokens = template + settings.suggest_max_page_bytes + MAX_BRIEF_BYTES
    output_tokens = settings.suggest_max_output_tokens
    cost = (
        input_tokens * settings.inference_input_token_cost_usd
        + output_tokens * settings.inference_output_token_cost_usd
    )
    return {
        "max_cost_usd": round(cost, 6),
        "max_input_tokens": input_tokens,
        "max_output_tokens": output_tokens,
        "model": settings.openai_model if settings.reply_agent_adapter == "plain" else "fake",
        "max_page_bytes": settings.suggest_max_page_bytes,
        "max_suggestions": MAX_SUGGESTIONS,
    }


def _check_brief(product_brief: str) -> list:
    """The brief's numbered sentences, or BriefSuggestionError if the brief,
    as the prompt shows it, passes MAX_BRIEF_BYTES."""
    if len(product_brief.encode("utf-8")) > MAX_BRIEF_BYTES:
        raise BriefSuggestionError(
            BRIEF_TOO_LONG,
            f"The brief is longer than {MAX_BRIEF_BYTES} bytes, the most the suggestion call takes.",
        )
    sentences = split_brief(product_brief)
    shown = "\n".join(render_brief_sentence(b) for b in sentences)
    if len(shown.encode("utf-8")) > MAX_BRIEF_BYTES:
        raise BriefSuggestionError(
            BRIEF_TOO_LONG,
            f"The brief, with the headings repeated for each sentence, is longer than {MAX_BRIEF_BYTES} "
            "bytes, the most the suggestion call takes.",
        )
    return sentences


async def suggest_brief_additions(
    product_url: str,
    product_brief: str,
    product_name: Optional[str],
    fetcher: PageFetcherPort,
    suggester: BriefSuggesterPort,
) -> dict:
    """Read the page, ask the model once, keep what code can verify.

    Raises PageFetchError (the page could not be read; no model call),
    BriefSuggestionError, or the budget and model errors of the call.
    """
    url = (product_url or "").strip()
    error = product_url_error(url)
    if error:
        raise PageFetchError(URL_INVALID, error + ".")
    product_brief = product_brief or ""
    brief_sentences = _check_brief(product_brief)

    page = await fetcher.fetch(url)
    request = SuggestRequest(
        page_text=page.text,
        page_source=page.source,
        brief_sentences=brief_sentences,
        product_name=_one_line(product_name or "")[:MAX_PRODUCT_NAME_CHARS] or None,
    )
    with meter_api_inference(SUGGEST_METER_CALL) as meter:
        response = await suggester.suggest(request)
    if response.malformed:
        raise BriefSuggestionError(
            MODEL_ANSWER_UNUSABLE,
            "The model's answer was not in the expected form. Nothing was suggested; try again.",
        )

    brief_folded = _collapsed_casefold(product_brief)
    suggestions: List[dict] = []
    seen: set = set()
    dropped_quote = dropped_other = 0
    for raw in response.suggestions:
        sentence = _one_line(raw.sentence)
        quote = _one_line(raw.quote)
        folded = sentence.casefold()
        if not quote_in_text(quote, page.text) or len(quote.split()) < MIN_QUOTE_WORDS or len(quote) > MAX_QUOTE_CHARS:
            dropped_quote += 1
            continue
        if not sentence or len(sentence) > MAX_SENTENCE_CHARS or folded in seen or folded in brief_folded:
            dropped_other += 1
            continue
        if len(suggestions) == MAX_SUGGESTIONS:
            dropped_other += 1
            continue
        seen.add(folded)
        suggestions.append({
            "sentence": sentence,
            "quote": quote,
            "why": _one_line(raw.why)[:MAX_WHY_CHARS],
        })

    by_id = {b.id: b for b in brief_sentences}
    flags: List[dict] = []
    flagged: set = set()
    for raw in response.flags:
        sentence = by_id.get(raw.brief_sentence_id)
        if sentence is None or raw.kind not in FLAG_KINDS or raw.brief_sentence_id in flagged:
            continue
        quote = _one_line(raw.quote)
        if raw.kind == "contradicted" and (not quote_in_text(quote, page.text) or len(quote) > MAX_QUOTE_CHARS):
            continue  # a contradiction code cannot find on the page is not shown
        flagged.add(raw.brief_sentence_id)
        flags.append({
            "brief_sentence_id": sentence.id,
            "brief_sentence": sentence.text,
            "kind": raw.kind,
            "quote": quote if raw.kind == "contradicted" else "",
            "note": _one_line(raw.note)[:MAX_NOTE_CHARS],
        })

    return {
        "source": page.source,
        "page_truncated": page.truncated,
        "suggestions": suggestions,
        "flags": flags,
        "dropped_quote_not_on_page": dropped_quote,
        "dropped_other": dropped_other,
        "cost_usd": round(meter.cost_usd, 6),
        "model": response.model_used,
    }
