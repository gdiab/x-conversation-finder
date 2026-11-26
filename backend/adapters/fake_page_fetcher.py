"""Fake page fetcher for tests (issue #42). No network.

Returns the page text given for a URL, or raises the PageFetchError given for
it. Unknown URLs raise page_not_found. It checks the URL like the real
fetcher (check_fetch_url), so a non-https URL fails the same way.
"""

from typing import Dict, List, Union

from backend.adapters.http_page_fetcher import check_fetch_url, github_repo
from backend.ports.page_fetcher import PAGE_NOT_FOUND, FetchedPage, PageFetchError, PageFetcherPort


class FakePageFetcher(PageFetcherPort):
    def __init__(self, pages: Dict[str, Union[str, PageFetchError]]):
        self.pages = pages
        self.fetched: List[str] = []

    async def fetch(self, url: str) -> FetchedPage:
        check_fetch_url(url)
        self.fetched.append(url)
        page = self.pages.get(url)
        if isinstance(page, PageFetchError):
            raise page
        if page is None:
            raise PageFetchError(PAGE_NOT_FOUND, "The page was not found (HTTP 404).")
        return FetchedPage(url=url, source="github_readme" if github_repo(url) else "web_page", text=page)
