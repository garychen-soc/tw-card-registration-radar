"""Transport behavior independent of a browser installation or network access."""

from unittest.mock import Mock

import pytest
from playwright.sync_api import CDPSession, Page
from playwright.sync_api import TimeoutError as BrowserTimeout

from radar.browser import BrowserFetcher
from radar.fetch import source_fetcher
from radar.spec import SourceSpec
from radar.transport import MAX_REDIRECTS, AccessDenied, BlockedURL, Fetcher, FetchFailed

URL = "https://www.example.com/offer"


@pytest.fixture
def browser() -> BrowserFetcher:
    fetcher = BrowserFetcher(["example.com"], min_host_interval=0)
    page = Mock(spec=Page)
    response = Mock()
    response.status = 200
    page.goto.return_value = response
    page.url = URL
    page.title.return_value = "活動"
    page.content.return_value = "<p>活動期間：2026/10/1~2026/12/31</p>"
    fetcher._page = page
    fetcher._context = Mock()
    fetcher._session = Mock(spec=CDPSession)
    fetcher._main_frame = "main"
    yield fetcher
    fetcher.close()


def test_browser_success_returns_rendered_html_without_claiming_a_cached_response(
    browser: BrowserFetcher,
) -> None:
    response = browser.get(URL, conditional=True)
    assert response.status_code == 200
    assert "2026/10/1" in response.text
    assert response.final_url == URL
    assert not response.not_modified
    assert not browser.cache.has_validators(URL)


@pytest.mark.parametrize("url", ["http://www.example.com/", "https://127.0.0.1/",
                                 "https://untrusted.example/"])
def test_browser_rejects_unsafe_initial_urls_before_navigation(
    browser: BrowserFetcher, url: str,
) -> None:
    with pytest.raises(BlockedURL):
        browser.get(url)
    browser._page.goto.assert_not_called()


def test_browser_rejects_form_submission(browser: BrowserFetcher) -> None:
    with pytest.raises(FetchFailed, match="不提交表單"):
        browser.get(URL, data={"page": "2"})
    browser._page.goto.assert_not_called()


def test_each_redirect_document_is_checked_before_sending(browser: BrowserFetcher) -> None:
    browser._guard_navigation({"requestId": "1", "frameId": "main", "request": {"url": URL}})
    browser._session.send.assert_called_with("Fetch.continueRequest", {"requestId": "1"})
    browser._guard_navigation({"requestId": "2", "frameId": "main",
                               "request": {"url": "http://10.0.0.1/secret"}})
    browser._session.send.assert_called_with("Fetch.failRequest", {
        "requestId": "2", "errorReason": "BlockedByClient",
    })
    assert browser._blocked_url == "http://10.0.0.1/secret"


def test_browser_retains_the_redirect_budget(browser: BrowserFetcher) -> None:
    for index in range(MAX_REDIRECTS + 2):
        browser._guard_navigation({"requestId": str(index), "frameId": "main",
                                   "request": {"url": URL}})
    assert browser._redirect_limit
    browser._session.send.assert_called_with("Fetch.failRequest", {
        "requestId": str(MAX_REDIRECTS + 1), "errorReason": "BlockedByClient",
    })


@pytest.mark.parametrize("status", [401, 403, 429])
def test_browser_does_not_retry_access_denial(browser: BrowserFetcher, status: int) -> None:
    browser._page.goto.return_value.status = status
    with pytest.raises(AccessDenied):
        browser.get(URL)
    assert browser._page.goto.call_count == 1


def test_browser_does_not_parse_an_unresolved_verification_page(browser: BrowserFetcher) -> None:
    browser._page.title.return_value = "請稍候..."
    browser._page.wait_for_function.side_effect = BrowserTimeout("requires verification")
    with pytest.raises(AccessDenied, match="仍要求驗證"):
        browser.get(URL)
    assert browser._page.goto.call_count == 1
    browser._page.content.assert_not_called()


def test_browser_retries_transient_timeout(
    browser: BrowserFetcher, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("radar.browser.time.sleep", lambda _: None)
    response = browser._page.goto.return_value
    browser._page.goto.side_effect = [BrowserTimeout("timeout"), response]
    assert browser.get(URL).status_code == 200
    assert browser._page.goto.call_count == 2


def test_browser_retains_the_response_size_limit(browser: BrowserFetcher) -> None:
    with pytest.raises(FetchFailed, match="回應超過"):
        browser.get(URL, max_bytes=4)
    assert browser._page.goto.call_count == 1


def test_browser_rejects_an_unsafe_final_url(browser: BrowserFetcher) -> None:
    browser._page.url = "http://10.0.0.1/"
    with pytest.raises(BlockedURL):
        browser.get(URL)
    browser._page.content.assert_not_called()


def test_only_declared_sources_use_the_browser_backend() -> None:
    data = {"id": "test", "bank_name": "test", "domains": ["example.com"],
            "listing": {"kind": "single_page", "entry_url": URL}}
    with source_fetcher(SourceSpec.model_validate(data)) as fetcher:
        assert type(fetcher) is Fetcher
    with source_fetcher(SourceSpec.model_validate({**data, "transport": "browser"})) as fetcher:
        assert isinstance(fetcher, BrowserFetcher)


def test_browser_spec_rejects_a_post_listing() -> None:
    with pytest.raises(ValueError, match="靜態 HTML"):
        SourceSpec.model_validate({
            "id": "test", "bank_name": "test", "domains": ["example.com"],
            "transport": "browser", "listing": {"kind": "json_api", "entry_url": URL,
                                                  "data_url": URL},
        })
