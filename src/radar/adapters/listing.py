"""三種清單型態的通用讀取器。

前身每家銀行一個手寫函式，17 家共 3,716 行，差異只在清單 selector 與分頁機制。
這裡三個函式涵蓋實測到的所有情形，由 spec 驅動。

新增一家銀行時的判斷：官方有 JSON 清單端點 → ``json_api``；清單在 HTML 裡
→ ``html_list``；翻頁需要 POST 表單狀態 → ``form_paged``。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from ..parse.datetimes import find_date_range
from ..parse.normalize import normalize_inline
from ..spec import ListingSpec, SourceSpec
from ..transport import Response, TransportError

# 清單請求一律不用條件式 GET。304 不帶 body，而清單沒有本機存檔可退回 ——
# 收到 304 就會把空字串當成清單內容，產出 0 筆。這個 bug 曾讓星展在第二次
# 執行時整個來源歸零（明細那條路徑已處理，當時漏了清單）。
LISTING_CONDITIONAL = False


class Fetch(Protocol):
    """runner 與測試共用的抓取介面。"""

    def get(
        self,
        url: str,
        *,
        data: dict[str, str] | None = ...,
        headers: dict[str, str] | None = ...,
        conditional: bool = ...,
    ) -> Response: ...


@dataclass
class ListingItem:
    """清單層級的一筆活動。明細尚未讀取。"""

    url: str
    title: str
    summary: str = ""
    start: date | None = None
    end: date | None = None
    category: str = ""
    featured: bool = False
    props: Any = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """清單指紋。內容未變且快取未過期時可跳過明細讀取。

        沿用前身的策略 —— 實測有效：沿用 935 筆、省下 798 次明細讀取。
        """
        payload = json.dumps(
            {
                "url": self.url,
                "title": self.title,
                "summary": self.summary,
                "start": self.start.isoformat() if self.start else None,
                "end": self.end.isoformat() if self.end else None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _walk(payload: Any) -> Iterator[dict[str, Any]]:
    """在任意巢狀結構裡找出所有 dict，供未指定 items_path 時使用。"""
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _walk(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _walk(value)


def _get(row: dict[str, Any], path: str) -> Any:
    """支援點號的巢狀取值。國泰世華的日期在 campaignProps 底下。"""
    if not path:
        return None
    value: Any = row
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _public_url(listing: ListingSpec, raw: str) -> str:
    """把清單給的路徑轉成公開網址。

    國泰世華的 ``campaignPath`` 是 AEM 內部路徑
    ``/content/cub-aem-cs/zh-tw/cathaybk/...``，直接用會連到內部路由；
    去掉前綴接上公開網域才是使用者實際看到的頁面。
    """
    path = raw.strip()
    if listing.url_strip_prefix and path.startswith(listing.url_strip_prefix):
        path = path[len(listing.url_strip_prefix) :]
    if listing.url_base:
        return urljoin(listing.url_base, path)
    return urljoin(listing.entry_url, path)


def _navigate(payload: Any, path: list[str]) -> Any:
    for key in path:
        if isinstance(payload, dict):
            payload = payload.get(key)
        else:
            return None
    return payload


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for candidate in (text[:10], text.replace("/", "-")[:10]):
        try:
            return date.fromisoformat(candidate)
        except ValueError:
            continue
    match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def read_json_api(spec: SourceSpec, fetcher: Fetch) -> list[ListingItem]:
    listing = spec.listing
    response = fetcher.get(listing.data_url, conditional=LISTING_CONDITIONAL)
    payload = response.json()
    rows = _navigate(payload, listing.items_path) if listing.items_path else payload

    fields = listing.fields
    url_key = fields.get("url", "url")
    candidates: list[dict[str, Any]] = []
    if isinstance(rows, list):
        candidates = [row for row in rows if isinstance(row, dict)]
    elif rows is not None:
        # 端點回傳巢狀結構（例如聯邦按 catalog 分組）時，撈出所有帶 url 欄位的 dict
        root_key = url_key.split(".")[0]
        candidates = [row for row in _walk(rows) if root_key in row]

    allowed_categories = set(listing.categories)
    items: list[ListingItem] = []
    seen: set[str] = set()
    for row in candidates:
        raw_url = _get(row, url_key)
        if not isinstance(raw_url, str) or not raw_url.strip():
            continue
        url = _public_url(listing, raw_url)
        category = normalize_inline(str(_get(row, fields.get("category", "")) or ""))
        if allowed_categories and category and category not in allowed_categories:
            continue
        if url in seen:
            continue
        seen.add(url)
        featured_key = fields.get("featured_rank", "")
        featured_value = _get(row, featured_key) if featured_key else None
        items.append(
            ListingItem(
                url=url,
                title=normalize_inline(str(_get(row, fields.get("title", "title")) or "")),
                summary=normalize_inline(str(_get(row, fields.get("summary", "")) or "")),
                start=_as_date(_get(row, fields.get("start", ""))),
                end=_as_date(_get(row, fields.get("end", ""))),
                category=category,
                featured=bool(featured_value) and str(featured_value) not in {"0", "False"},
                props=_get(row, fields.get("props", "")) if fields.get("props") else None,
                raw=row,
            )
        )
    return items


def _items_from_html(listing: ListingSpec, html: str, base_url: str) -> list[ListingItem]:
    blocks: list[tuple[str, str]]
    if listing.item_selector:
        tree = HTMLParser(html)
        # 用換行分隔，這樣區塊文字的第一行就是卡片標題
        blocks = [
            (node.html or "", node.text(separator="\n") or "")
            for node in tree.css(listing.item_selector)
        ]
    else:
        blocks = [(html, "")]

    pattern = re.compile(listing.link_pattern, re.I) if listing.link_pattern else None
    items: list[ListingItem] = []
    seen: set[str] = set()
    for block_html, block_text in blocks:
        if pattern is None:
            continue
        for match in pattern.finditer(block_html):
            href = match.group(1) if match.groups() else match.group(0)
            # 第二個擷取群組（有的話）當標題。實測台新把活動名稱放在錨點的
            # title 屬性裡，錨點文字只是「瞭解詳情」。
            link_title = match.group(2) if len(match.groups()) >= 2 else ""
            url = urljoin(base_url, href.strip())
            if url in seen:
                continue
            seen.add(url)
            # 卡片上常直接印期間（富邦是 2026.01.01~2026.12.31）。
            # 抓到就帶進 ListingItem，明細頁抓不到時可以補位。
            card_start, card_end, _, _ = find_date_range(
                block_text, default_year=date.today().year, limit_chars=200
            )
            items.append(
                ListingItem(
                    url=url,
                    title=normalize_inline(
                        link_title
                        or _pick(block_html, listing.title_selector)
                        or _block_title(block_html, block_text)
                    ),
                    summary=normalize_inline(_pick(block_html, listing.summary_selector)),
                    start=card_start,
                    end=card_end,
                )
            )
    return items


def _pick(block_html: str, selector: str) -> str:
    """用 CSS selector 從區塊中取文字。找不到就回空字串。"""
    if not selector:
        return ""
    node = HTMLParser(block_html).css_first(selector)
    return (node.text() or "").strip() if node else ""


def _block_title(block_html: str, block_text: str) -> str:
    for tag in ("h6", "h5", "h4", "h3", "h2"):
        match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block_html, re.S | re.I)
        if match:
            return re.sub(r"<[^>]+>", "", match.group(1))
    # 沒有標題標籤時取區塊文字的第一行 —— 卡片的第一行就是活動名稱
    for line in block_text.splitlines():
        candidate = line.strip()
        if len(candidate) >= 2:
            return candidate[:120]
    return block_text[:120]


# Wicket 的分頁連結長成 `?0-1.-fmList-...-navigation-2-pageLink`，直接 GET 會 500。
# 必須把 `<版本>-1.-` 改寫成 `<版本>-1.0-`（補上 behavior id），並附 Wicket 標頭。
_WICKET_PAGE_LINK = re.compile(
    r'<a[^>]+href="([^"]*-nav-navigation-\d+-pageLink)"[^>]*>(.*?)</a>', re.I | re.S
)
_WICKET_VERSION = re.compile(r"(\d+-1)\.-")
_WICKET_REDIRECT = re.compile(r"<redirect>\s*<!\[CDATA\[(.*?)\]\]>\s*</redirect>", re.I | re.S)


def _wicket_page_links(html: str, base_url: str) -> dict[int, str]:
    """回傳 {可見頁碼: 連結}。只收標籤是數字的 —— 上一頁／下一頁的箭頭不是頁碼。"""
    links: dict[int, str] = {}
    for match in _WICKET_PAGE_LINK.finditer(html):
        label = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        if not label.isdigit():
            continue
        href = _WICKET_VERSION.sub(r"\1.0-", match.group(1))
        links[int(label)] = urljoin(base_url, href)
    return links


def _unwrap_cdata(body: str) -> str:
    """Wicket 的 Ajax 回應把更新後的 HTML 片段包在 CDATA 裡。
    去掉包裝才能用同一套 selector 與正則解析。"""
    return body.replace("<![CDATA[", "").replace("]]>", "")


def _read_wicket_pages(
    listing: ListingSpec, fetcher: Fetch, first_html: str, first_url: str
) -> list[ListingItem]:
    """跟隨 Apache Wicket 的 Ajax 分頁。

    兩個非顯而易見的必要條件（都是實測出來的）：

    * 分頁連結要把 ``<版本>-1.-`` 改寫成 ``<版本>-1.0-``，否則回 500。
    * 要帶 ``Accept``、``Referer``、``Wicket-Ajax`` 等標頭，否則回 403。

    還有兩個結構問題：

    * 頁碼列只顯示一個視窗（例如 1–10），想到第 15 頁必須先點視窗內最大的
      頁碼讓視窗往前移，再繼續 —— 找不到目標頁碼時退而點擊可見的最大頁碼。
    * 伺服器的頁面狀態會失步，這時 Ajax 回應變成一個彈回第 1 頁的 redirect
      （實測第 3 次翻頁起就會發生）。此時不能直接放棄 —— 跟隨 redirect 取得
      新的完整頁面後從那裡重走，容忍連續數輪沒有新資料才收手。
    """
    items = _items_from_html(listing, first_html, first_url)
    seen = {item.url for item in items}
    html, url, referer = first_html, first_url, first_url
    target = 2
    attempts = 0
    stale_rounds = 0
    max_attempts = listing.max_pages * 3
    max_stale_rounds = 4

    while target <= listing.max_pages and attempts < max_attempts:
        attempts += 1
        links = _wicket_page_links(html, url)
        requested = target
        chosen = links.get(target)
        if chosen is None:
            lower = [page for page in links if page < target]
            if not lower:
                break
            requested = max(lower)
            chosen = links[requested]
            if requested < 2:
                break

        headers = {
            "Accept": "application/xml, text/xml, */*; q=0.01",
            "Cache-Control": "no-cache",
            "Referer": referer,
            "Wicket-Ajax": "true",
            "Wicket-Ajax-BaseURL": listing.pagination_base or "",
            "X-Requested-With": "XMLHttpRequest",
        }
        buster = f"{'&' if '?' in chosen else '?'}_={int(time.time() * 1000) + attempts}"
        try:
            response = fetcher.get(f"{chosen}{buster}", headers=headers, conditional=False)
        except TransportError:
            break  # 少幾頁的資料比整個來源歸零好
        body, final = _unwrap_cdata(response.text), response.final_url

        redirect = _WICKET_REDIRECT.search(response.text)
        if redirect:
            try:
                followed = fetcher.get(urljoin(final, redirect.group(1).strip()), conditional=False)
            except TransportError:
                break
            body, final = followed.text, followed.final_url

        fresh = [item for item in _items_from_html(listing, body, final) if item.url not in seen]
        for item in fresh:
            seen.add(item.url)
        items.extend(fresh)
        html, url, referer = body, final, final
        if fresh:
            stale_rounds = 0
        else:
            stale_rounds += 1
            if stale_rounds >= max_stale_rounds:
                break
        if requested == target and fresh:
            target += 1
    return items


WICKET_ATTEMPTS = 3


def _best_wicket_run(
    listing: ListingSpec, fetcher: Fetch, entry_url: str, first: Response
) -> list[ListingItem]:
    """重跑 Wicket 分頁數次並取最多的一次。

    伺服器的頁面狀態不穩定，同一份 spec 在不同執行中拿到的筆數差很多
    （實測 9／54／90／117／45）。這種變異會讓逐來源覆蓋率回歸的防護不停誤擋 ——
    正確的處理是降低來源端的變異，而不是放寬防護門檻。

    代價是最多 3 倍的分頁請求。只對宣告 wicket_ajax 的來源生效。
    """
    best = _read_wicket_pages(listing, fetcher, first.text, first.final_url)
    for _ in range(WICKET_ATTEMPTS - 1):
        try:
            retry = fetcher.get(entry_url, conditional=False)
        except TransportError:
            break
        found = _read_wicket_pages(listing, fetcher, retry.text, retry.final_url)
        if len(found) > len(best):
            best = found
    return best


def read_html_list(spec: SourceSpec, fetcher: Fetch) -> list[ListingItem]:
    listing = spec.listing
    # 分類清單：逐一請求每個分類的網址（台新分 A–I 九類）
    urls = (
        [listing.url_template.format(category=code) for code in listing.category_codes]
        if listing.url_template and listing.category_codes
        else [listing.entry_url]
    )
    items: list[ListingItem] = []
    seen: set[str] = set()
    for index, url in enumerate(urls):
        try:
            response = fetcher.get(url, conditional=LISTING_CONDITIONAL)
        except TransportError:
            if index == 0 and len(urls) == 1:
                raise
            continue  # 單一分類失敗不讓整個來源歸零
        if listing.pagination_kind == "wicket_ajax":
            found = _best_wicket_run(listing, fetcher, url, response)
        else:
            found = _items_from_html(listing, response.text, response.final_url)
        for item in found:
            if item.url not in seen:
                seen.add(item.url)
                items.append(item)
    return items


_HIDDEN_INPUT = r'name="{name}"[^>]*value="(\d+)"'


def read_form_paged(spec: SourceSpec, fetcher: Fetch) -> list[ListingItem]:
    """需要表單狀態才能翻頁的清單。

    涵蓋兩種實測到的形態：

    元大式
        GET ``entry_url`` 拿第一頁，總頁數藏在隱藏欄位，後續頁 POST 回同一個
        網址並帶回那些狀態值。
    玉山式
        清單來自另一個端點（``post_url``），**第一頁就要 POST** 並附固定參數
        （``form_data``），回傳的是 HTML 片段；總筆數藏在 ``id="total"`` 這類
        欄位裡，總頁數要用「總筆數 ÷ 首頁筆數」推算。

    單一分頁失敗不中止 —— 少一頁的資料比整個來源歸零好。
    """
    listing = spec.listing
    page_field = listing.form_fields.get("page")
    post_url = listing.post_url or listing.entry_url

    def payload(page: int, extra: dict[str, str] | None = None) -> dict[str, str]:
        data = dict(listing.form_data)
        if page_field:
            data[page_field] = str(page)
        data.update(extra or {})
        return data

    if listing.post_url:
        first = fetcher.get(post_url, data=payload(1), conditional=False)
    else:
        first = fetcher.get(listing.entry_url, conditional=LISTING_CONDITIONAL)
    items = _items_from_html(listing, first.text, first.final_url)
    if not page_field:
        return items

    total_pages = _page_count(listing, first.text, len(items))
    total_field = listing.form_fields.get("total_pages", "")
    items_field = listing.form_fields.get("total_items", "")
    state: dict[str, str] = {}
    if total_field:
        state[total_field] = str(total_pages)
    if items_field:
        found = re.search(
            _HIDDEN_INPUT.format(name=re.escape(items_field)), first.text, re.I
        )
        if found:
            state[items_field] = found.group(1)

    seen = {item.url for item in items}
    for page in range(2, total_pages + 1):
        response = fetcher.get(post_url, data=payload(page, state), conditional=False)
        for item in _items_from_html(listing, response.text, response.final_url):
            if item.url not in seen:
                seen.add(item.url)
                items.append(item)
    return items


def _page_count(listing: ListingSpec, html: str, items_on_first_page: int) -> int:
    """總頁數。優先用隱藏欄位的總頁數，否則用總筆數推算。"""
    total_field = listing.form_fields.get("total_pages", "")
    if total_field and (
        match := re.search(_HIDDEN_INPUT.format(name=re.escape(total_field)), html, re.I)
    ):
        return max(1, min(int(match.group(1)), listing.max_pages))
    if listing.total_pattern and items_on_first_page:
        match = re.search(listing.total_pattern, html, re.I)
        if match:
            total_items = int(match.group(1))
            pages = -(-total_items // items_on_first_page)  # ceil
            return max(1, min(pages, listing.max_pages))
    return 1


def read_single_page(spec: SourceSpec, fetcher: Fetch) -> list[ListingItem]:
    """入口頁本身就是唯一的活動頁。

    中信的 LINE Pay 優惠頁一頁 14 個活動，頁上的連結全指向 ``line.me``
    而非活動明細 —— 沒有逐活動的官方網址可抓。這種來源只有一個「活動頁」，
    再由 ``detail.cardinality = "many"`` 切成多個子活動。

    不在這裡抓取 —— runner 讀明細時自然會抓，失敗也會由它記錄成來源健康。
    """
    return [ListingItem(url=spec.listing.entry_url, title=spec.bank_name)]


READERS = {
    "json_api": read_json_api,
    "html_list": read_html_list,
    "form_paged": read_form_paged,
    "single_page": read_single_page,
}


def read_listing(spec: SourceSpec, fetcher: Fetch) -> list[ListingItem]:
    return READERS[spec.listing.kind](spec, fetcher)
