"""transport 層測試（不觸網）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from radar.transport import BlockedURL, HttpCache, is_allowed

DOMAINS = ["esunbank.com", "card.ubot.com.tw"]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.esunbank.com/zh-tw/personal/credit-card",
        "https://esunbank.com/",
        "https://card.ubot.com.tw/CardActivity",
    ],
)
def test_official_https_urls_are_allowed(url: str) -> None:
    assert is_allowed(url, DOMAINS)


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("http://www.esunbank.com/", "明文 http"),
        ("https://evil.com/", "非白名單網域"),
        ("https://esunbank.com.evil.com/", "後綴偽裝"),
        ("https://10.100.6.38/frontend/bonusDetail.jsp?id=3450", "私有 IP —— 彰銀官方頁實際吐出過"),
        ("https://192.168.1.1/", "私有 IP"),
        ("https://127.0.0.1/", "loopback"),
        ("ftp://esunbank.com/", "非 https scheme"),
    ],
)
def test_unsafe_urls_are_rejected(url: str, why: str) -> None:
    assert not is_allowed(url, DOMAINS), why


def test_bare_public_ip_is_also_rejected() -> None:
    """即使是公開 IP 也拒絕 —— 銀行不會用裸 IP 提供正式服務。"""
    assert not is_allowed("https://8.8.8.8/", [*DOMAINS, "8.8.8.8"])


def test_blocked_url_is_a_distinct_exception_type() -> None:
    """BlockedURL 與 FetchFailed 分開，讓呼叫端能「跳過這一筆」而非讓整次執行失敗。

    前身在單一筆連結被拒時例外往上冒，導致整次更新 exit 1、其餘 16 家一起失敗。
    """
    from radar.transport import FetchFailed, TransportError

    assert issubclass(BlockedURL, TransportError)
    assert issubclass(FetchFailed, TransportError)
    assert not issubclass(BlockedURL, FetchFailed)


def test_http_cache_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "http_cache.json"
    cache = HttpCache(path=path)
    cache.entries["https://x.test/a"] = {
        "etag": 'W/"abc"',
        "last_modified": "Mon, 04 Aug 2026 00:00:00 GMT",
        "content_hash": "deadbeef",
    }
    cache.save()

    reloaded = HttpCache.load(path)
    assert reloaded.validators("https://x.test/a") == {
        "If-None-Match": 'W/"abc"',
        "If-Modified-Since": "Mon, 04 Aug 2026 00:00:00 GMT",
    }
    assert reloaded.content_hash("https://x.test/a") == "deadbeef"
    assert reloaded.validators("https://x.test/unknown") == {}


def test_http_cache_survives_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "http_cache.json"
    path.write_text("{ not json", encoding="utf-8")
    assert HttpCache.load(path).entries == {}


def test_http_cache_ignores_non_dict_entries(tmp_path: Path) -> None:
    path = tmp_path / "http_cache.json"
    path.write_text(json.dumps({"https://x.test/a": "oops"}), encoding="utf-8")
    assert HttpCache.load(path).entries == {}
