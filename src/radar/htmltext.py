"""HTML → 可分析純文字。

段落切分吃的是純文字（實測 7 家明細頁只有 2 家有可辨識的 DOM 段落標題，
但 6 家的純文字都含段落關鍵字），所以這一層的品質直接決定後續解析品質。

兩個必須做對的細節：

* ``script``/``style``/``noscript`` 要整段移除，否則 JS 裡的日期字串會被
  當成活動時間解析。
* 區塊元素之間要補換行。段落錨點靠行結構定位，把整頁擠成一行會讓
  「活動期間」與後面無關的段落黏在一起。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from selectolax.parser import HTMLParser

_DROP = ("script", "style", "noscript", "template", "svg")
_BLOCK = (
    "p",
    "div",
    "li",
    "tr",
    "br",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "dt",
    "dd",
    "th",
    "td",
    "section",
    "article",
)


def to_text(html: str) -> str:
    tree = HTMLParser(html)
    for selector in _DROP:
        for node in tree.css(selector):
            node.decompose()
    root = tree.body or tree.root
    if root is None:
        return ""
    text = root.text(separator="\n", strip=True)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def strings_of(payload: Any) -> Iterator[str]:
    """遞迴取出巢狀 JSON 裡的所有字串。

    國泰世華的明細頁是 SPA（實測純文字只有 31 字），唯一可用的內容在清單 API
    的 ``campaignProps`` 裡，結構未公開且會變動，因此不依賴特定欄位路徑，
    而是把所有字串攤平後交給同一套段落與時間解析。
    """
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped:
            yield stripped
    elif isinstance(payload, dict):
        for value in payload.values():
            yield from strings_of(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from strings_of(value)


def links(html: str, base_url: str) -> list[tuple[str, str]]:
    """回傳 (絕對網址, 連結文字)。``data-link`` 優先於 ``href``
    —— 部分銀行把真實目標放在 data 屬性上。"""
    from urllib.parse import urljoin

    tree = HTMLParser(html)
    result: list[tuple[str, str]] = []
    for node in tree.css("a"):
        href = node.attributes.get("data-link") or node.attributes.get("href") or ""
        if not href:
            continue
        try:
            resolved = urljoin(base_url, href)
        except ValueError:
            continue
        result.append((resolved, (node.text() or "").strip()))
    return result
