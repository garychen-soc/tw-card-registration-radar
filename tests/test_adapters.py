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


def test_listing_never_uses_conditional_get() -> None:
    """清單請求不得用條件式 GET。

    304 不帶 body，而清單沒有本機存檔可退回 —— 收到 304 就會把空字串當成
    清單內容，產出 0 筆。實測星展在第二次執行時整個來源因此歸零。
    """
    seen: list[bool] = []
    spec = _spec(
        {
            "kind": "json_api",
            "entry_url": "https://www.example.com/list",
            "data_url": "https://www.example.com/api/list.json",
            "fields": {"title": "title", "url": "url"},
        }
    )
    fetcher = FakeFetcher(
        pages={"https://www.example.com/api/list.json": json.dumps([{"title": "A", "url": "/a"}])}
    )
    original = fetcher.get

    def get(url: str, *, conditional: bool = True, **kwargs: object):  # type: ignore[no-untyped-def]
        seen.append(conditional)
        return original(url, conditional=conditional, **kwargs)  # type: ignore[arg-type]

    fetcher.get = get  # type: ignore[method-assign]
    read_listing(spec, fetcher)
    assert seen == [False]


def test_html_listing_also_avoids_conditional_get() -> None:
    seen: list[bool] = []
    spec = _spec(
        {
            "kind": "html_list",
            "entry_url": "https://www.example.com/list",
            "link_pattern": r'href="([^"]*/promo/[^"]+)"',
        }
    )
    fetcher = FakeFetcher(pages={"https://www.example.com/list": '<a href="/promo/1"></a>'})
    original = fetcher.get

    def get(url: str, *, conditional: bool = True, **kwargs: object):  # type: ignore[no-untyped-def]
        seen.append(conditional)
        return original(url, conditional=conditional, **kwargs)  # type: ignore[arg-type]

    fetcher.get = get  # type: ignore[method-assign]
    read_listing(spec, fetcher)
    assert seen == [False]


WICKET_PAGE_1 = """
<div class="list">
  <a href="Detail?sn=D000001">A</a><a href="Detail?sn=D000002">B</a>
</div>
<div class="nav">
  <a href="./Result?0-1.-fmList-divSearchResult-nav-navigation-1-pageLink">2</a>
  <a href="./Result?0-1.-fmList-divSearchResult-nav-navigation-2-pageLink">3</a>
  <a href="./Result?0-1.-fmList-divSearchResult-nav-next">下一頁</a>
</div>
"""


def test_wicket_page_links_rewrites_url_and_skips_arrows() -> None:
    """兩個實測必要條件：`-1.-` 要改寫成 `-1.0-`（否則 500）；
    上一頁／下一頁的箭頭不是頁碼，不能當成分頁連結。"""
    from radar.adapters.listing import _wicket_page_links

    links = _wicket_page_links(WICKET_PAGE_1, "https://www.example.com/promotion/Result")
    assert sorted(links) == [2, 3]
    assert "-1.0-fmList" in links[2], "未改寫 behavior id 會讓 Wicket 回 500"
    assert "next" not in links[2]


def test_wicket_pagination_follows_pages_and_tolerates_state_reset() -> None:
    """Wicket 的頁面狀態會失步，Ajax 回應變成彈回第 1 頁的 redirect。
    實測第 3 次翻頁起就會發生 —— 不能直接放棄。"""
    from radar.adapters.listing import _unwrap_cdata

    page2 = '<a href="Detail?sn=D000003">C</a>' + WICKET_PAGE_1.split('<div class="nav">')[1]
    reset = "<ajax-response><redirect><![CDATA[./Result?1]]></redirect></ajax-response>"
    assert "<![CDATA[" not in _unwrap_cdata(reset)

    spec = _spec(
        {
            "kind": "html_list",
            "entry_url": "https://www.example.com/promotion/Result",
            "link_pattern": r'href="(Detail\?sn=[A-Za-z0-9]+)"',
            "pagination_kind": "wicket_ajax",
            "pagination_base": "promotion/Result",
            "max_pages": 4,
        }
    )
    pages = {"https://www.example.com/promotion/Result": WICKET_PAGE_1}
    fetcher = FakeFetcher(pages=pages)
    original = fetcher.get

    def get(url: str, **kwargs: object):  # type: ignore[no-untyped-def]
        if "pageLink" in url:
            fetcher.pages[url] = page2
        return original(url, **kwargs)  # type: ignore[arg-type]

    fetcher.get = get  # type: ignore[method-assign]
    items = read_listing(spec, fetcher)
    assert {item.url.rsplit("=", 1)[1] for item in items} == {"D000001", "D000002", "D000003"}


def test_category_codes_fetch_every_category_url() -> None:
    """台新分 A–I 九類，每類一個網址。單一分類失敗不讓整個來源歸零。"""
    from radar.transport import FetchFailed

    spec = _spec(
        {
            "kind": "html_list",
            "entry_url": "https://www.example.com/offerList/A",
            "url_template": "https://www.example.com/offerList/{category}",
            "category_codes": ["A", "B", "C"],
            "link_pattern": r'href="([^"]*/detail/WM_\d+)"',
        }
    )
    fetcher = FakeFetcher(
        pages={
            "https://www.example.com/offerList/A": '<a href="/detail/WM_1"></a>',
            "https://www.example.com/offerList/C": '<a href="/detail/WM_3"></a>',
        },
        failures={"https://www.example.com/offerList/B": FetchFailed("HTTP 503")},
    )
    items = read_listing(spec, fetcher)
    assert {item.url.rsplit("_", 1)[1] for item in items} == {"1", "3"}


def test_json_api_walks_categories_and_pages() -> None:
    """逐分類 POST + 逐頁。第一銀行的活動只存在於 JS 打的 REST 端點上 ——
    入口頁 372KB HTML 只有約 950 字純文字、0 個日期。"""

    class ApiFetcher(FakeFetcher):
        def get(self, url, *, data=None, headers=None, conditional=True):  # type: ignore[no-untyped-def]
            assert data is not None
            self.posted.append((url, dict(data)))
            key = (data["categoryIdList"], int(data["pageNumberSel"]))
            rows = {("A", 1): ["a1", "a2"], ("A", 2): ["a3"], ("B", 1): ["b1"]}.get(key, [])
            body = json.dumps({"activityListData": [{"u": f"/p/{r}", "t": r} for r in rows]})
            self.pages[url] = body
            return super().get(url, conditional=False)

    spec = _spec(
        {
            "kind": "json_api",
            "entry_url": "https://www.example.com/list",
            "data_url": "https://www.example.com/api",
            "items_path": ["activityListData"],
            "category_codes": ["A", "B"],
            "max_pages": 5,
            "form_data": {"categoryIdList": "{category}"},
            "form_fields": {"page": "pageNumberSel"},
            "fields": {"url": "u", "title": "t"},
        }
    )
    fetcher = ApiFetcher()
    items = read_listing(spec, fetcher)

    assert [item.title for item in items] == ["a1", "a2", "a3", "b1"]
    assert items[0].url == "https://www.example.com/p/a1"
    # A 類第 3 頁回空就停，不會一路打到 max_pages
    assert [
        (payload["categoryIdList"], payload["pageNumberSel"]) for _, payload in fetcher.posted
    ] == [("A", "1"), ("A", "2"), ("A", "3"), ("B", "1"), ("B", "2")]


def test_json_api_stops_when_a_page_repeats_itself() -> None:
    """端點忽略頁碼、每頁回同一批時必須停。

    實測第一銀行若把所有分類代碼併成一次請求就是這個行為 —— 照分頁器公告的
    總頁數走會無限拿到重複資料。
    """

    class StuckFetcher(FakeFetcher):
        def get(self, url, *, data=None, headers=None, conditional=True):  # type: ignore[no-untyped-def]
            assert data is not None
            self.posted.append((url, dict(data)))
            self.pages[url] = json.dumps(
                {"activityListData": [{"u": "/p/same", "t": "same"}]}
            )
            return super().get(url, conditional=False)

    spec = _spec(
        {
            "kind": "json_api",
            "entry_url": "https://www.example.com/list",
            "data_url": "https://www.example.com/api",
            "items_path": ["activityListData"],
            "max_pages": 99,
            "form_data": {"q": "x"},
            "form_fields": {"page": "pageNumberSel"},
            "fields": {"url": "u", "title": "t"},
        }
    )
    fetcher = StuckFetcher()
    items = read_listing(spec, fetcher)

    assert len(items) == 1
    assert [payload["pageNumberSel"] for _, payload in fetcher.posted] == ["1", "2"]


def test_listing_scope_restricts_links_to_one_tab_panel() -> None:
    """華南一頁裡有存款、貸款、保險等多個分頁，不限縮會收到別業務的連結。"""
    html = """
    <a aria-label="信用卡" aria-controls="panel-card" href="#">信用卡</a>
    <a aria-label="貸款" aria-controls="panel-loan" href="#">貸款</a>
    <div id="panel-card">
      <a href="/wps/card/a" title="連結至卡片活動A">x</a>
      <a href="/wps/card/b" title="另開視窗連結至卡片活動B">x</a>
    </div>
    <div id="panel-loan"><a href="/wps/loan/c" title="連結至貸款活動C">x</a></div>
    """
    spec = _spec(
        {
            "kind": "html_list",
            "entry_url": "https://www.example.com/list",
            "scope_tab_label": "信用卡",
            "link_pattern": r'<a\s+href="(/wps/[^"]+)"[^>]*?title="(?:另開視窗)?連結至([^"]+)"',
        }
    )
    items = read_listing(spec, FakeFetcher(pages={"https://www.example.com/list": html}))

    assert [item.title for item in items] == ["卡片活動A", "卡片活動B"]
