"""三種清單型態的讀取測試（離線）。"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from datetime import date

import pytest
from conftest import FakeFetcher

from radar.adapters.listing import _wicket_window, read_listing
from radar.spec import SourceSpec
from radar.transport import Response

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


WICKET_ENTRY = "https://cardpromote.example.com/promotion/Result"
WICKET_PAGE_SIZE = 9


def _wicket_spec(**overrides: object) -> SourceSpec:
    return _spec(
        {
            "kind": "html_list",
            "entry_url": WICKET_ENTRY,
            "link_pattern": r'href="(Detail\?sn=[A-Za-z0-9]+)"',
            "pagination_kind": "wicket_ajax",
            "pagination_base": "promotion/Result",
            "total_pattern": r"共\s*(\d+)\s*筆",
            "max_pages": 40,
            **overrides,
        }
    )


@dataclass
class WicketSite:
    """模擬富邦的清單頁：**每次翻頁都有機會被判成狀態過期並把你送回第 1 頁**。

    這是實測出來的行為，不是假想的：清單頁在 Envoy 後面有多個節點，Wicket 的
    頁面狀態只存在處理該次 render 的那一個節點上、且沒有 session 黏著，所以
    單跳 40 次只成功 20 次，加延遲也沒有用（0／0.5／1／2／4 秒各 8 次，
    成功 6／3／4／5／2）。用固定種子的亂數重現這個「一半機會」。

    頁碼列只顯示一個寬度 10 的視窗，所以第 11 頁以後不能一跳直達 —— 這是舊版
    走到一半就迷路的另一半原因。
    """

    total: int = 24
    items_total: int = 215
    announced: int = 215
    """頁面上印出來的總筆數。刻意與 ``items_total`` 分開 —— 官方的數字可能過期。"""
    stale_probability: float = 0.5
    seed: int = 0
    _rng: random.Random = field(init=False)
    page_id: int = 0
    hops: int = 0
    stale: int = 0
    requested: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def _page_html(self, page: int) -> str:
        first = (page - 1) * WICKET_PAGE_SIZE
        count = max(0, min(WICKET_PAGE_SIZE, self.items_total - first))
        cards = "".join(
            f'<a href="Detail?sn=X{first + index:04d}">活動 {first + index}</a>'
            for index in range(1, count + 1)
        )
        nav: list[str] = []
        for candidate in _wicket_window(page, self.total, 10):
            if candidate == page:
                # 當前頁的頁碼連結被停用（沒有 href）—— 走訪端就是靠這個認出
                # 「我實際落在第幾頁」，而不是相信自己要求的頁碼。
                nav.append(f'<a class="page-link" disabled="disabled"><span>{candidate}</span></a>')
                continue
            nav.append(
                f'<a class="page-link" href="./Result?{self.page_id}-1.'
                f'-fmList-divSearchResult-nav-navigation-{candidate}-pageLink">'
                f"<span>{candidate}</span></a>"
            )
        for arrow in ("next", "last"):
            nav.append(
                f'<a href="./Result?{self.page_id}-1.'
                f'-fmList-divSearchResult-nav-{arrow}"><img/></a>'
            )
        return (
            f"<p>共{self.announced}筆相關結果</p>"
            f'<div class="list">{cards}</div><ul>{"".join(nav)}</ul>'
        )

    def _response(self, url: str, text: str) -> Response:
        return Response(
            requested_url=url,
            final_url=url.split("&_=")[0],
            status_code=200,
            text=text,
            content_type="text/html",
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    def get(
        self,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        conditional: bool = True,
    ) -> Response:
        self.requested.append(url)
        target = re.search(r"navigation-(\d+)-pageLink", url)
        if target or "-nav-last" in url:
            assert headers is not None, "缺 Wicket 標頭會 403"
            assert headers.get("Wicket-Ajax") == "true", "缺 Wicket 標頭會 403"
            assert "-1.0-" in url, "未改寫 behavior id 會讓 Wicket 回 500"
            self.hops += 1
            if self._rng.random() < self.stale_probability:
                self.stale += 1
                self.page_id += 1
                return self._response(
                    url,
                    "<ajax-response><redirect>"
                    f"<![CDATA[./Result?{self.page_id}]]></redirect></ajax-response>",
                )
            page = self.total if target is None else int(target.group(1))
            return self._response(url, f"<![CDATA[{self._page_html(page)}]]>")
        # 入口頁，以及狀態過期後被彈去的新頁面實例 —— 一律是第 1 頁
        self.page_id += 1
        return self._response(url, self._page_html(1))


@pytest.mark.parametrize("seed", [0, 1, 2, 7, 99])
def test_wicket_pagination_collects_every_page_despite_state_resets(seed: int) -> None:
    """核心回歸：一半的翻頁請求被判成狀態過期，收到的筆數仍必須是全部。

    舊版把狀態過期當成「偶發失步」，用「連續幾輪沒有新資料就收手」收尾，於是
    「走到第幾頁」變成隨機變數 —— 同一份程式碼連跑三次得到 108／117／135 筆，
    更早還出現過 90／99／153／198。換成不同種子就是換一組失手位置，筆數不該跟著變。
    """
    site = WicketSite(seed=seed)
    warnings: list[str] = []
    items = read_listing(_wicket_spec(), site, warnings)
    assert len(items) == 215, f"seed={seed} 少收了分頁"
    assert len({item.url for item in items}) == 215
    assert warnings == [], "全部收齊時不該留下警示"
    assert site.stale > 0, "測試本身沒有重現狀態過期就沒有意義"


def test_wicket_pagination_reports_pages_it_could_not_reach() -> None:
    """走不完必須留下警示。少幾頁的資料比整個來源歸零好，但不可以看起來像全部收完。"""
    site = WicketSite(stale_probability=1.0)
    warnings: list[str] = []
    items = read_listing(_wicket_spec(), site, warnings)
    assert len(items) == 9, "第 1 頁是完整 GET 拿到的，仍然要留著"
    assert any("未走訪完整" in message for message in warnings)
    assert any("215" in message for message in warnings), "要說出官方宣告的總筆數"


def test_wicket_bounce_back_is_not_counted_as_the_requested_page() -> None:
    """狀態過期時伺服器悄悄回第 1 頁 —— 不能把它當成要求的那一頁。

    舊版沒有讀「實際落在第幾頁」，於是把彈回的第 1 頁內容當成目標頁收下並把
    目標往前推，看起來一路走到底、實際只有前幾頁。
    """
    site = WicketSite(total=3, items_total=25, announced=25, stale_probability=1.0)
    items = read_listing(_wicket_spec(), site, [])
    detail = "https://cardpromote.example.com/promotion/Detail?sn=X"
    assert {item.url for item in items} == {f"{detail}{index:04d}" for index in range(1, 10)}


def test_wicket_total_pages_follow_the_announced_count() -> None:
    """總頁數由官方宣告的總筆數推算，不是等分頁連結探索自己停下來。

    頁碼列一次只顯示 10 個頁碼，靠探索永遠看不到第 11 頁以後存在 —— 也順便
    確認沒有繞遠路：24 頁在「每頁最多兩跳」下不該用掉幾十跳。
    """
    site = WicketSite(stale_probability=0.0)
    items = read_listing(_wicket_spec(), site, [])
    assert len(items) == 215
    assert site.hops <= 30, f"用了 {site.hops} 跳，超出『每頁最多兩跳』的預期"


def test_wicket_extends_beyond_a_stale_announced_count() -> None:
    """官方宣告的總筆數過期（活動變多）時，看到更大的頁碼要把目標往上補。

    宣告 125 筆會推算成 14 頁，實際卻有 24 頁。停在推算值會少收 10 頁，而且
    對帳的分母也是那個過期的數字，於是看起來像「收齊了」—— 這是最難發現的一種漏收。
    """
    site = WicketSite(announced=125, stale_probability=0.0)
    warnings: list[str] = []
    items = read_listing(_wicket_spec(), site, warnings)
    assert len(items) == 215, "只走宣告的 14 頁會停在 126 筆"
    assert warnings == []


def test_wicket_window_matches_the_observed_layout() -> None:
    """實測：第 1 頁看到 1–10、第 10 頁看到 6–15、最後一頁（24）看到 15–24。"""
    assert list(_wicket_window(1, 24, 10)) == list(range(1, 11))
    assert list(_wicket_window(6, 24, 10)) == list(range(2, 12))
    assert list(_wicket_window(10, 24, 10)) == list(range(6, 16))
    assert list(_wicket_window(24, 24, 10)) == list(range(15, 25))


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


def test_json_api_extracts_url_from_html_and_skips_disabled_rows() -> None:
    """端點沒有純網址欄位、且會標記已下架的資料列 —— 兆豐兩者都有。

    連結包在 DetailPageLinkHtml 這個完整的 <a> 標籤裡；Removal=c-card--disabled
    是官方標記「活動已結束」，實測 29 筆中有 8 筆仍留著明細連結，不排除就會把
    114/2/20~115/2/19 那種早就結束的活動發布出去。
    """
    payload = [
        {
            "Title": "現行活動",
            "Removal": "",
            "Link": '<a href="/event/alive" class="c-card__action">了解更多</a>',
        },
        {
            "Title": "已下架活動",
            "Removal": "c-card--disabled",
            "Link": '<a href="/event/ended" class="c-card__action">了解更多</a>',
        },
        {
            # 官方把連結拿掉的（多數是已結束）—— 抽不出網址就跳過，不是錯誤
            "Title": "沒有明細頁",
            "Removal": "",
            "Link": '<a class="c-card__action">了解更多</a>',
        },
    ]
    spec = _spec(
        {
            "kind": "json_api",
            "entry_url": "https://www.example.com/list",
            "data_url": "https://www.example.com/api",
            "url_pattern": r'href="([^"]+)"',
            "skip_field": "Removal",
            "fields": {"url": "Link", "title": "Title"},
        }
    )
    items = read_listing(
        spec, FakeFetcher(pages={"https://www.example.com/api": json.dumps(payload)})
    )

    assert [item.title for item in items] == ["現行活動"]
    assert items[0].url == "https://www.example.com/event/alive"
