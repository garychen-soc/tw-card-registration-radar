"""三種清單型態的讀取測試（離線）。"""

from __future__ import annotations

import json
from datetime import date

from conftest import FakeFetcher

from radar.adapters.listing import read_listing
from radar.spec import SourceSpec

BASE = {
    "id": "demo",
    "bank_name": "示範銀行",
    "domains": ["example.com"],
}


def _spec(listing: dict[str, object], **extra: object) -> SourceSpec:
    return SourceSpec.model_validate({**BASE, "listing": listing, **extra})


def test_json_api_flat_list_with_field_mapping() -> None:
    spec = _spec(
        {
            "kind": "json_api",
            "entry_url": "https://www.example.com/list",
            "data_url": "https://www.example.com/api/list.json",
            "fields": {
                "title": "Title",
                "summary": "SubTitle",
                "url": "Url",
                "start": "StartDate",
                "end": "EndDate",
            },
        }
    )
    payload = [
        {
            "Title": "夏日回饋",
            "SubTitle": "最高 5%",
            "Url": "/promo/1",
            "StartDate": "2026-08-01T00:00:00",
            "EndDate": "2026/08/31",
        }
    ]
    fetcher = FakeFetcher(pages={"https://www.example.com/api/list.json": json.dumps(payload)})
    items = read_listing(spec, fetcher)
    assert len(items) == 1
    assert items[0].url == "https://www.example.com/promo/1"
    assert items[0].title == "夏日回饋"
    assert items[0].start == date(2026, 8, 1)
    assert items[0].end == date(2026, 8, 31)


def test_json_api_items_path_navigation() -> None:
    """台中銀的清單包在 row 底下。"""
    spec = _spec(
        {
            "kind": "json_api",
            "entry_url": "https://www.example.com/list",
            "data_url": "https://www.example.com/api/list.json",
            "items_path": ["row"],
            "fields": {"title": "Title", "url": "Url"},
        }
    )
    payload = {"row": [{"Title": "A", "Url": "/a"}, {"Title": "B", "Url": "/b"}]}
    fetcher = FakeFetcher(pages={"https://www.example.com/api/list.json": json.dumps(payload)})
    assert len(read_listing(spec, fetcher)) == 2


def test_json_api_walks_nested_catalogs() -> None:
    """聯邦的端點按 catalog 分組。未指定 items_path 時遞迴撈出帶 url 的物件。"""
    spec = _spec(
        {
            "kind": "json_api",
            "entry_url": "https://www.example.com/list",
            "data_url": "https://www.example.com/api/list.json",
            "categories": ["網購數位", "百貨零售"],
            "fields": {
                "title": "title",
                "url": "url",
                "category": "catalog",
                "featured_rank": "hotOrder",
            },
        }
    )
    payload = {
        "groups": [
            {"rows": [{"title": "網購 A", "url": "/a", "catalog": "網購數位", "hotOrder": 3}]},
            {"rows": [{"title": "百貨 B", "url": "/b", "catalog": "百貨零售", "hotOrder": 0}]},
            {"rows": [{"title": "貸款 C", "url": "/c", "catalog": "貸款"}]},
        ]
    }
    fetcher = FakeFetcher(pages={"https://www.example.com/api/list.json": json.dumps(payload)})
    items = read_listing(spec, fetcher)
    # 貸款不在 categories 白名單內，應被排除
    assert [item.title for item in items] == ["網購 A", "百貨 B"]
    assert items[0].featured is True
    assert items[1].featured is False


def test_json_api_deduplicates_shared_urls() -> None:
    spec = _spec(
        {
            "kind": "json_api",
            "entry_url": "https://www.example.com/list",
            "data_url": "https://www.example.com/api/list.json",
            "fields": {"title": "title", "url": "url"},
        }
    )
    payload = [{"title": "A", "url": "/same"}, {"title": "A 副本", "url": "/same"}]
    fetcher = FakeFetcher(pages={"https://www.example.com/api/list.json": json.dumps(payload)})
    assert len(read_listing(spec, fetcher)) == 1


def test_html_list_extracts_links_and_titles() -> None:
    spec = _spec(
        {
            "kind": "html_list",
            "entry_url": "https://www.example.com/list",
            "item_selector": "li.card",
            "link_pattern": r'href="([^"]*/promo/[^"]+)"',
        }
    )
    html = """
    <ul>
      <li class="card"><a href="/promo/1"></a><h6>夏日回饋</h6></li>
      <li class="card"><a href="/promo/2"></a><h6>秋季加碼</h6></li>
      <li class="other"><a href="/loan/9"></a><h6>信貸</h6></li>
    </ul>
    """
    items = read_listing(spec, FakeFetcher(pages={"https://www.example.com/list": html}))
    assert [item.title for item in items] == ["夏日回饋", "秋季加碼"]
    assert items[0].url == "https://www.example.com/promo/1"


def test_form_paged_reads_hidden_state_and_posts_following_pages() -> None:
    """元大／台北富邦式的分頁：總頁數藏在隱藏欄位，後續頁需 POST 帶回狀態。"""
    spec = _spec(
        {
            "kind": "form_paged",
            "entry_url": "https://www.example.com/list.do",
            "link_pattern": r'href="([^"]*/in\.do\?id=[^"]+)"',
            "max_pages": 30,
            "form_fields": {"page": "pN", "total_pages": "pA", "total_items": "iA"},
        }
    )
    page1 = """
    <input name="pA" value="3"><input name="iA" value="42">
    <a href="/in.do?id=1"></a>
    """
    other = '<a href="/in.do?id=%d"></a>'
    fetcher = FakeFetcher(
        pages={"https://www.example.com/list.do": page1},
    )

    calls: list[dict[str, str] | None] = []
    original = fetcher.get

    def get(url: str, *, data: dict[str, str] | None = None, **kwargs: object):  # type: ignore[no-untyped-def]
        calls.append(data)
        if data is not None:
            fetcher.pages[url] = other % int(data["pN"])
        return original(url, data=data, **kwargs)  # type: ignore[arg-type]

    fetcher.get = get  # type: ignore[method-assign]
    items = read_listing(spec, fetcher)

    assert [call["pN"] for call in calls if call] == ["2", "3"]
    assert all(call["iA"] == "42" for call in calls if call)
    assert {item.url.rsplit("=", 1)[1] for item in items} == {"1", "2", "3"}


def test_form_paged_without_form_fields_returns_first_page_only() -> None:
    spec = _spec(
        {
            "kind": "form_paged",
            "entry_url": "https://www.example.com/list.do",
            "link_pattern": r'href="([^"]*/in\.do\?id=[^"]+)"',
        }
    )
    fetcher = FakeFetcher(pages={"https://www.example.com/list.do": '<a href="/in.do?id=1"></a>'})
    assert len(read_listing(spec, fetcher)) == 1


def test_fingerprint_changes_with_content() -> None:
    spec = _spec(
        {
            "kind": "json_api",
            "entry_url": "https://www.example.com/list",
            "data_url": "https://www.example.com/api/list.json",
            "fields": {"title": "title", "url": "url"},
        }
    )
    first = FakeFetcher(
        pages={"https://www.example.com/api/list.json": json.dumps([{"title": "A", "url": "/a"}])}
    )
    second = FakeFetcher(
        pages={
            "https://www.example.com/api/list.json": json.dumps([{"title": "A 改標", "url": "/a"}])
        }
    )
    assert read_listing(spec, first)[0].fingerprint != read_listing(spec, second)[0].fingerprint
