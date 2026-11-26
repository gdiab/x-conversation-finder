"""The watch's product URL at the end of a draft (issue #41).

The Start Watch form's "Product URL" is stored on the watch and passed to its
Reviews. Code, never the model, appends it to a draft that passed its check
and names the product, so the copied reply pastes into X ready to post:

- *Which drafts.* A passing draft whose reply plan says to mention the
  product, or that has a sentence checked as a product claim (a written
  draft's "claim" sentence, an assembled draft's claim segment). Other drafts
  get no link.
- *The check is unchanged.* The written check and the claim gate run on the
  sentences without the link, so the URL never fires the number, name or
  feature triggers.
- *Length.* X counts any link as 23 characters (t.co). With the link and the
  space before it, the sentences get at most 280 - 24 = 256 characters.
- *Edits.* A founder edit that ends with the product URL is checked without
  it, under the 256-character budget; an edit without it is checked as
  before. The link is never required.

Everything here is pure and runs inside activities or the API, never in
workflow code.
"""

from typing import Optional, Tuple
from urllib.parse import urlsplit

# X shortens every link to a t.co URL of this many characters, whatever its length
X_LINK_CHARS = 23
# The link is appended after one space
PRODUCT_LINK_CHARS = X_LINK_CHARS + 1
MAX_PRODUCT_URL_CHARS = 2048


def product_url_error(url: str) -> Optional[str]:
    """Why `url` cannot be a watch's product URL, or None.

    An https URL with a host, without spaces, so it reads as one link on X
    and as one trailing word in an edited draft.
    """
    if not url:
        return "Product URL is required"
    if len(url) > MAX_PRODUCT_URL_CHARS:
        return f"Product URL must be at most {MAX_PRODUCT_URL_CHARS} characters"
    if any(ch.isspace() for ch in url):
        return "Product URL must not contain spaces"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "Product URL is not a valid URL"
    if parts.scheme.lower() != "https" or not url.lower().startswith("https://"):
        return "Product URL must start with https://"
    try:
        host = parts.hostname
    except ValueError:
        host = None
    if not host or "." not in host.strip("."):
        return "Product URL must name a site, such as https://example.com"
    return None


def append_product_link(body: str, url: str) -> str:
    """The draft as it is posted: the checked sentences, one space, the URL.

    A body that already holds the URL as a word of its own (a brief
    sentence quoting it, inserted verbatim or paraphrased) gets no second
    copy. A longer URL that starts with it ("<url>/pricing") is another link
    and does not count."""
    if holds_product_link(body, url):
        return body
    return f"{body.rstrip()} {url}"


# Punctuation that can close a sentence or a bracket right after a link
_AFTER_LINK = ".,;:!?)]}\"'\u201d\u2019"


def holds_product_link(body: str, url: str) -> bool:
    """Whether a word of `body` is exactly `url`, or `url` between opening
    and closing or sentence punctuation only."""
    for word in body.split():
        word = word.lstrip("([{\"'\u201c\u2018")
        if word == url:
            return True
        if word.startswith(url) and word[len(url):] and all(ch in _AFTER_LINK for ch in word[len(url):]):
            return True
    return False


def linked_max_chars(text: str, url: str, body: str, max_chars: int) -> int:
    """The length left for `body` (the text without its trailing product link,
    see strip_product_link) under X's `max_chars`: the link counts as
    X_LINK_CHARS, and the whitespace before it as typed (a founder's edit can
    put a line break or two spaces there)."""
    separator = len(text.rstrip()) - len(url) - len(body)
    return max_chars - X_LINK_CHARS - max(separator, 0)


def strip_product_link(text: str, url: Optional[str]) -> Tuple[str, bool]:
    """Split a trailing product URL off a text: (the text without it, whether it was there).

    The URL must be the text's last word, exactly as the watch has it, with
    whitespace (or nothing) before it. A URL anywhere else, or a changed one
    (an added period, another path), is part of the text and is checked as
    such. Without a product URL the text is returned unchanged.
    """
    if not url or text is None:
        return text, False
    trimmed = text.rstrip()
    if not trimmed.endswith(url):
        return text, False
    body = trimmed[: -len(url)]
    if body and not body[-1].isspace():
        return text, False
    return body.rstrip(), True


def writer_max_chars(max_chars: int, product_url: Optional[str], mention_product: Optional[bool]) -> int:
    """The length the writer is asked for: the link's share comes off when
    the plan says to mention the product, the drafts that get the link."""
    if product_url and mention_product is True:
        return max_chars - PRODUCT_LINK_CHARS
    return max_chars
