"""PageFetcherPort - read the text of the watch's product page (issue #42).

The Start Watch form's "Suggest brief additions" button reads the page at the
Product URL: for a github.com/<owner>/<repo> URL its README through the GitHub
REST API, for any other https URL the page's readable text. The text is
untrusted data. It is used for one model call in the API process and for the
quote check, and is never stored, logged or sent to Temporal.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class FetchedPage:
    """The readable text of a page.

    source: "github_readme" or "web_page".
    truncated: the text was cut at the size cap; suggestions see only the
    first part.
    """

    url: str
    source: str
    text: str
    truncated: bool = False


class PageFetchError(Exception):
    """A page could not be read. `code` is what the API and the UI switch on;
    the message is shown to the founder and never holds page text."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# Codes (see backend/api.py, BRIEF_SUGGESTION_ERRORS)
URL_INVALID = "url_invalid"  # not an https URL to a site
URL_REFUSED = "url_refused"  # resolves to a private, loopback or other non-public address
PAGE_NOT_FOUND = "page_not_found"  # 404, or a repository without a README
RATE_LIMITED = "rate_limited"  # GitHub's unauthenticated rate limit
FETCH_FAILED = "fetch_failed"  # network error, timeout, other HTTP status
UNSUPPORTED_CONTENT = "unsupported_content"  # not HTML or text, or no readable text


class PageFetcherPort(ABC):
    """Port for reading a product page."""

    @abstractmethod
    async def fetch(self, url: str) -> FetchedPage:
        """Return the page's readable text, or raise PageFetchError."""
