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
        candidates = [row for row in _walk(rows) if url_key in row]

    allowed_categories = set(listing.categories)
    items: list[ListingItem] = []
    seen: set[str] = set()
    for row in candidates:
        raw_url = row.get(url_key)
        if not isinstance(raw_url, str) or not raw_url.strip():
            continue
        url = urljoin(listing.entry_url, raw_url.strip())
        category = normalize_inline(str(row.get(fields.get("category", ""), "") or ""))
        if allowed_categories and category and category not in allowed_categories:
            continue
        if url in seen:
            continue
        seen.add(url)
        featured_key = fields.get("featured_rank", "")
        featured_value = row.get(featured_key) if featured_key else None
        items.append(
            ListingItem(
                url=url,
                title=normalize_inline(str(row.get(fields.get("title", "title"), "") or "")),
                summary=normalize_inline(str(row.get(fields.get("summary", ""), "") or "")),
                start=_as_date(row.get(fields.get("start", ""))),
                end=_as_date(row.get(fields.get("end", ""))),
                category=category,
                featured=bool(featured_value) and str(featured_value) not in {"0", "False"},
                props=row.get(fields.get("props", "")) if fields.get("props") else None,
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
                    title=normalize_inline(_block_title(block_html, block_text)),
                )
            )
    return items


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
    """需要 POST 表單狀態才能翻頁的清單（元大、台北富邦）。

    第一頁用 GET，並從隱藏欄位讀出總頁數與總筆數；後續頁以 POST 帶回同樣的
    狀態值。單一分頁失敗不中止 —— 少一頁的資料比整個來源歸零好，
    失敗頁數回報給呼叫端計入來源健康。
    """
    listing = spec.listing
    first = fetcher.get(listing.entry_url)
    items = _items_from_html(listing, first.text, first.final_url)

    page_field = listing.form_fields.get("page")
    total_field = listing.form_fields.get("total_pages")
    if not page_field or not total_field:
        return items

    match = re.search(_HIDDEN_INPUT.format(name=re.escape(total_field)), first.text, re.I)
    total_pages = min(int(match.group(1)), listing.max_pages) if match else 1
    items_field = listing.form_fields.get("total_items", "")
    items_match = (
        re.search(_HIDDEN_INPUT.format(name=re.escape(items_field)), first.text, re.I)
        if items_field
        else None
    )

    seen = {item.url for item in items}
    for page in range(2, total_pages + 1):
        data = {page_field: str(page), total_field: str(total_pages)}
        if items_field and items_match:
            data[items_field] = items_match.group(1)
        response = fetcher.get(listing.entry_url, data=data, conditional=False)
        for item in _items_from_html(listing, response.text, response.final_url):
            if item.url not in seen:
                seen.add(item.url)
                items.append(item)
    return items


READERS = {
    "json_api": read_json_api,
    "html_list": read_html_list,
    "form_paged": read_form_paged,
}


def read_listing(spec: SourceSpec, fetcher: Fetch) -> list[ListingItem]:
    return READERS[spec.listing.kind](spec, fetcher)
