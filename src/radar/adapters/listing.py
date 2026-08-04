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
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from ..parse.normalize import normalize_inline
from ..spec import ListingSpec, SourceSpec
from ..transport import Response


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
    response = fetcher.get(listing.data_url)
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
        blocks = [(node.html or "", node.text() or "") for node in tree.css(listing.item_selector)]
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
            url = urljoin(base_url, href.strip())
            if url in seen:
                continue
            seen.add(url)
            items.append(
                ListingItem(
                    url=url,
                    title=normalize_inline(
                        _pick(block_html, listing.title_selector)
                        or _block_title(block_html, block_text)
                    ),
                    summary=normalize_inline(_pick(block_html, listing.summary_selector)),
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
    return block_text[:120]


def read_html_list(spec: SourceSpec, fetcher: Fetch) -> list[ListingItem]:
    response = fetcher.get(spec.listing.entry_url)
    return _items_from_html(spec.listing, response.text, response.final_url)


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
        first = fetcher.get(listing.entry_url)
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
