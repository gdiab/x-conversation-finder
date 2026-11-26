"""Read the product page over HTTPS, with an SSRF guard (issue #42).

- A github.com/<owner>/<repo> URL is read as the repository's README through
  the GitHub REST API (GET /repos/{owner}/{repo}/readme, unauthenticated, as
  raw Markdown). Any other https URL is read as a web page, and its readable
  text is extracted from the HTML.
- Only https on the standard port, without user info in the URL. Each
  request, redirects included, resolves the host first and refuses it when
  any address it resolves to is not a public one (private, loopback,
  link-local, multicast, reserved, unspecified; IPv4-mapped IPv6 is checked as
  its IPv4 address). The request then connects to the checked address, with
  the host name as TLS SNI and Host header, so the certificate is checked for
  the host and a second DNS answer cannot point the connection elsewhere.
- Proxies from the environment are ignored (trust_env=False): a proxy would
  connect to the host by name, past the guard.
- Timeouts per request and for the whole read, at most MAX_REDIRECTS
  redirects, at most MAX_RAW_BYTES read from the network, and the extracted
  text cut to max_text_bytes (UTF-8).

The text is untrusted. Nothing here logs or stores it, and no error message
quotes it.
"""

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlsplit

import httpx

from backend.ports.page_fetcher import (
    FETCH_FAILED,
    PAGE_NOT_FOUND,
    RATE_LIMITED,
    UNSUPPORTED_CONTENT,
    URL_INVALID,
    URL_REFUSED,
    FetchedPage,
    PageFetchError,
    PageFetcherPort,
)

MAX_REDIRECTS = 3
# Bytes read from the network per response; the rest is not downloaded
MAX_RAW_BYTES = 2_000_000
REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# The whole read, redirects and DNS included
TOTAL_TIMEOUT_SEC = 25.0
DNS_TIMEOUT_SEC = 5.0
USER_AGENT = "x-conversation-finder-brief-suggestions"

GITHUB_API = "https://api.github.com"
_GITHUB_HOSTS = ("github.com", "www.github.com")
_GITHUB_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_TEXT_TYPES = ("text/plain", "text/markdown", "text/x-markdown")

Resolver = Callable[[str, int], Awaitable[List[str]]]


async def system_resolver(host: str, port: int) -> List[str]:
    """Every address the host resolves to, through the system resolver."""
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(
        loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout=DNS_TIMEOUT_SEC
    )
    return [info[4][0] for info in infos]


def is_public_address(address: str) -> bool:
    """Whether an IP address is one a public web server can have."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # ::ffff:127.0.0.1 reaches 127.0.0.1; 2002::/16 (6to4) and the
        # NAT64 prefix embed an IPv4 address too
        if ip.ipv4_mapped is not None:
            return is_public_address(str(ip.ipv4_mapped))
        if ip.sixtofour is not None:
            return is_public_address(str(ip.sixtofour))
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            return is_public_address(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
    return ip.is_global and not ip.is_multicast


def check_fetch_url(url: str) -> Tuple[str, str]:
    """(host, path with query) of a URL the fetcher may request, or raise
    PageFetchError(URL_INVALID). The address check comes after, per request."""
    if not isinstance(url, str) or not url or any(ch.isspace() for ch in url):
        raise PageFetchError(URL_INVALID, "Enter the product URL as one https:// link.")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise PageFetchError(URL_INVALID, "The product URL is not a valid URL.")
    if parts.scheme.lower() != "https":
        raise PageFetchError(URL_INVALID, "Only https:// URLs can be read.")
    if not host:
        raise PageFetchError(URL_INVALID, "The product URL must name a site, such as https://example.com.")
    if parts.username is not None or parts.password is not None:
        raise PageFetchError(URL_INVALID, "The product URL must not contain a user name or password.")
    if port not in (None, 443):
        raise PageFetchError(URL_INVALID, "Only the standard https port (443) can be read.")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return host.rstrip(".").lower(), path


def github_repo(url: str) -> Optional[Tuple[str, str]]:
    """(owner, repo) for https://github.com/<owner>/<repo> (a trailing slash
    or ".git" allowed), else None: other GitHub pages are read as web pages."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or host not in _GITHUB_HOSTS or parts.query:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) != 2:
        return None
    owner, repo = segments
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not _GITHUB_OWNER_RE.match(owner) or not _GITHUB_REPO_RE.match(repo) or repo in (".", ".."):
        return None
    return owner, repo


class _TextExtractor(HTMLParser):
    """The readable text of an HTML page: no scripts, styles or head, a line
    break at each block element."""

    _SKIP = frozenset({"script", "style", "noscript", "template", "svg", "head", "iframe", "canvas", "object"})
    _BLOCK = frozenset({
        "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th",
        "table", "section", "article", "header", "footer", "main", "nav", "aside", "blockquote",
        "pre", "dd", "dt", "dl", "hr", "figcaption", "summary", "details",
    })

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self._parts).split("\n"))
        return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def cut_to_bytes(text: str, max_bytes: int) -> Tuple[str, bool]:
    """The text cut to at most max_bytes of UTF-8 at a character boundary,
    and whether it was cut."""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text, False
    return data[:max_bytes].decode("utf-8", errors="ignore"), True


class HttpPageFetcher(PageFetcherPort):
    """Reads a product page over HTTPS through the SSRF guard."""

    def __init__(
        self,
        max_text_bytes: int,
        resolver: Optional[Resolver] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        """resolver and transport: tests inject a fake resolver and an
        httpx.MockTransport, so no test touches the network."""
        self._max_text_bytes = max_text_bytes
        self._resolver = resolver or system_resolver
        self._transport = transport

    async def fetch(self, url: str) -> FetchedPage:
        check_fetch_url(url)
        try:
            return await asyncio.wait_for(self._fetch(url), timeout=TOTAL_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            raise PageFetchError(FETCH_FAILED, "Reading the page took too long.")

    async def _fetch(self, url: str) -> FetchedPage:
        repo = github_repo(url)
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        ) as client:
            if repo:
                owner, name = repo
                api_url = f"{GITHUB_API}/repos/{quote(owner, safe='')}/{quote(name, safe='')}/readme"
                status, headers, body, cut = await self._get(client, api_url, {
                    "Accept": "application/vnd.github.raw+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                })
                self._raise_for_github(status, headers)
                text = self._decode(headers, body)
                source = "github_readme"
            else:
                status, headers, body, cut = await self._get(client, url, {
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,text/markdown;q=0.9",
                })
                self._raise_for_status(status)
                content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type in _HTML_TYPES:
                    text = html_to_text(self._decode(headers, body))
                elif content_type in _TEXT_TYPES:
                    text = self._decode(headers, body)
                else:
                    raise PageFetchError(
                        UNSUPPORTED_CONTENT,
                        f"The page is not HTML or text (content type {content_type or 'missing'}).",
                    )
                source = "web_page"
        text = text.strip()
        if not text:
            raise PageFetchError(
                UNSUPPORTED_CONTENT,
                "The page has no readable text. A page built by JavaScript cannot be read; "
                "try the repository or a docs page.",
            )
        text, text_cut = cut_to_bytes(text, self._max_text_bytes)
        return FetchedPage(url=url, source=source, text=text, truncated=cut or text_cut)

    async def _get(
        self, client: httpx.AsyncClient, url: str, headers: dict
    ) -> Tuple[int, httpx.Headers, bytes, bool]:
        """GET through the guard, following up to MAX_REDIRECTS redirects, each
        checked like the first URL. Returns (status, headers, body, cut)."""
        for _ in range(MAX_REDIRECTS + 1):
            host, path = check_fetch_url(url)
            address = await self._public_address(host)
            target = f"https://[{address}]{path}" if ":" in address else f"https://{address}{path}"
            request = client.build_request(
                "GET",
                target,
                headers={"Host": f"[{host}]" if ":" in host else host, "User-Agent": USER_AGENT, **headers},
                extensions={"sni_hostname": host},
            )
            try:
                response = await client.send(request, stream=True)
            except httpx.TimeoutException:
                raise PageFetchError(FETCH_FAILED, "The site did not answer in time.")
            except httpx.HTTPError as e:
                raise PageFetchError(FETCH_FAILED, f"The page could not be read ({type(e).__name__}).")
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise PageFetchError(FETCH_FAILED, "The site redirected without a location.")
                    url = urljoin(url, location)
                    continue
                body, cut = await self._read_capped(response)
                return response.status_code, response.headers, body, cut
            finally:
                await response.aclose()
        raise PageFetchError(FETCH_FAILED, f"The site redirected more than {MAX_REDIRECTS} times.")

    async def _public_address(self, host: str) -> str:
        try:
            ipaddress.ip_address(host.strip("[]"))
            addresses = [host.strip("[]")]
        except ValueError:
            try:
                addresses = await self._resolver(host, 443)
            except (OSError, asyncio.TimeoutError, UnicodeError):
                raise PageFetchError(FETCH_FAILED, f"The site {host} could not be found.")
        if not addresses:
            raise PageFetchError(FETCH_FAILED, f"The site {host} could not be found.")
        if not all(is_public_address(a) for a in addresses):
            raise PageFetchError(
                URL_REFUSED,
                f"The site {host} resolves to a private or local address, which is not read.",
            )
        return addresses[0].split("%", 1)[0]

    @staticmethod
    async def _read_capped(response: httpx.Response) -> Tuple[bytes, bool]:
        chunks: List[bytes] = []
        size = 0
        try:
            async for chunk in response.aiter_bytes():
                if size + len(chunk) > MAX_RAW_BYTES:
                    chunks.append(chunk[: MAX_RAW_BYTES - size])
                    return b"".join(chunks), True
                chunks.append(chunk)
                size += len(chunk)
        except httpx.TimeoutException:
            raise PageFetchError(FETCH_FAILED, "The site did not answer in time.")
        except httpx.HTTPError as e:
            raise PageFetchError(FETCH_FAILED, f"The page could not be read ({type(e).__name__}).")
        return b"".join(chunks), False

    @staticmethod
    def _decode(headers: httpx.Headers, body: bytes) -> str:
        charset = None
        for param in headers.get("content-type", "").split(";")[1:]:
            key, _, value = param.partition("=")
            if key.strip().lower() == "charset":
                charset = value.strip().strip('"') or None
        try:
            return body.decode(charset or "utf-8", errors="replace")
        except LookupError:
            return body.decode("utf-8", errors="replace")

    @staticmethod
    def _raise_for_status(status: int) -> None:
        if status == 404 or status == 410:
            raise PageFetchError(PAGE_NOT_FOUND, f"The page was not found (HTTP {status}).")
        if status == 429:
            raise PageFetchError(RATE_LIMITED, "The site is limiting requests (HTTP 429). Try again later.")
        if not 200 <= status < 300:
            raise PageFetchError(FETCH_FAILED, f"The site answered HTTP {status}.")

    @staticmethod
    def _raise_for_github(status: int, headers: httpx.Headers) -> None:
        if status == 404:
            raise PageFetchError(
                PAGE_NOT_FOUND,
                "GitHub has no README for this repository, or the repository is private or does not exist.",
            )
        if status == 429 or (status == 403 and headers.get("x-ratelimit-remaining") == "0"):
            reset = headers.get("x-ratelimit-reset")
            when = ""
            if reset and reset.isdigit():
                from datetime import datetime, timezone
                when = " until " + datetime.fromtimestamp(int(reset), timezone.utc).strftime("%H:%M UTC")
            raise PageFetchError(
                RATE_LIMITED,
                f"GitHub's limit for unauthenticated requests is used up{when}. Try again later.",
            )
        if not 200 <= status < 300:
            raise PageFetchError(FETCH_FAILED, f"GitHub answered HTTP {status}.")
