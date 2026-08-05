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

import re
from collections.abc import Iterator
from typing import Any

from selectolax.parser import HTMLParser

# 整段移除的元素。除了不可見內容，也移除頁面樣板 —— 實測元大的活動頁
# 若不移除導覽與 Cookie 告知，活動標題會變成「為提供您更好、更個人化的服務…」，
# 而導覽列的「活動登錄」選單會讓每一筆活動都被判定為需要登錄
# （57 筆中 49 筆因此誤標）。
_DROP = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "iframe",
    "nav",
    "footer",
    "aside",
)
# class / id 看得出是樣板的元素。刻意保守 —— 只列明確的導覽與提示元件，
# 不碰 banner、promo 這類可能就是活動內容的字樣。
_BOILERPLATE = re.compile(
    r"(?:^|[-_\s])(?:nav|navbar|navigation|menu|submenu|breadcrumb|cookie|"
    r"gotop|go-top|to-top|backtop|sitemap|langu|language|skip|accessibility)"
    r"(?:$|[-_\s])",
    re.I,
)
# 內容根節點的候選，依優先序。找到就只分析它，找不到才退回 body。
_CONTENT_ROOTS = ("main", "[role=main]", "article", "#content", ".content", "#main")

# 連結密度：整塊幾乎都是連結文字的區塊是選單，不是內容。
# class 名稱比對治不了這類 —— 實測玉山的「活動登錄／中獎名單／卡友權益／
# 常見問題」側邊選單既不在 <nav> 裡、class 也不像導覽，卻污染了 150 筆活動的
# 登錄原文，讓它們全被判定為「需登錄但抓不到時點」。
LINK_DENSITY_LIMIT = 0.8
LINK_COUNT_LIMIT = 5
# 佔全文這個比例以上的區塊一律不刪。連結密度是「這塊是選單」的訊號，但主內容
# 區塊本身也可能連結很密 —— 實測華南「信用卡」分頁面板有 44 個連結，
# 整塊被刪後 to_text 產出 0 字。選單只會是頁面的一小部分，內容不會。
LINK_DROP_MAX_SHARE = 0.5
_LINKY = ("ul", "ol", "div", "section", "dl")
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


def scope_html(html: str, *, selector: str = "", tab_label: str = "") -> str:
    """把 HTML 限縮到指定容器。找不到就回原本的 HTML（降級而非失敗）。

    ``tab_label`` 走分頁面板的間接指向：找 ``aria-label`` 等於它的 tab，
    再取 ``aria-controls`` 指到的容器。分頁式 CMS 頁面很常見 —— 實測華南的
    活動全在「信用卡」分頁的面板裡（5,899 字），不限縮就會混進存款、貸款、
    保險等其他分頁的內容，而通用的樣板／連結密度濾網也會把它整塊刪掉。
    """
    if not selector and not tab_label:
        return html
    tree = HTMLParser(html)
    if tab_label:
        pattern = re.compile(
            rf'<a[^>]+aria-controls="([^"]+)"[^>]*aria-label="{re.escape(tab_label)}"'
            rf'|<a[^>]+aria-label="{re.escape(tab_label)}"[^>]*aria-controls="([^"]+)"',
            re.I,
        )
        match = pattern.search(html)
        if match:
            panel_id = match.group(1) or match.group(2)
            node = tree.css_first(f"#{panel_id}")
            if node is not None and node.html:
                return node.html
    if selector:
        node = tree.css_first(selector)
        if node is not None and node.html:
            return node.html
    return html


def to_text(html: str) -> str:
    tree = HTMLParser(html)
    for selector in _DROP:
        for node in tree.css(selector):
            node.decompose()
    for node in tree.css("[class], [id]"):
        marker = f"{node.attributes.get('class') or ''} {node.attributes.get('id') or ''}"
        if _BOILERPLATE.search(marker):
            node.decompose()

    unfiltered = _extract(tree)
    _drop_link_lists(tree)
    filtered = _extract(tree)
    # 過濾後一無所剩時退回未過濾的文字 —— 寧可帶點選單雜訊，
    # 也不要讓整個來源變成空的（實測華南就是這樣掉到 0 筆）。
    return filtered or unfiltered


def _extract(tree: HTMLParser) -> str:
    """從（可能已被過濾的）樹取出純文字。"""
    root = None
    for selector in _CONTENT_ROOTS:
        root = tree.css_first(selector)
        if root is not None and len((root.text() or "").strip()) >= 200:
            break
        root = None
    root = root or tree.body or tree.root
    if root is None:
        return ""
    text = root.text(separator="\n", strip=True)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _drop_link_lists(tree: HTMLParser) -> None:
    """移除連結密度過高的區塊（選單、側邊欄、頁尾連結列）。

    代價是會連帶移除「參與門市清單」這類本身就是連結列表的內容。對本專案的
    用途（期間、登錄時點、條件解析）而言那是可接受的取捨 —— 讓選單文字混進
    活動內文的傷害大得多。
    """
    body = tree.body or tree.root
    total = _dense_len(body.text()) if body is not None else 0
    for tag in _LINKY:
        for node in tree.css(tag):
            # 只數非空白字元。HTML 的縮排會稀釋密度 —— 排版整齊的頁面
            # 會因此漏掉明顯是選單的區塊。
            length = _dense_len(node.text())
            if not length:
                continue
            if total and length / total >= LINK_DROP_MAX_SHARE:
                continue
            anchors = node.css("a")
            if len(anchors) < LINK_COUNT_LIMIT:
                continue
            link_length = sum(_dense_len(a.text()) for a in anchors)
            if link_length / length >= LINK_DENSITY_LIMIT:
                node.decompose()


def _dense_len(text: str | None) -> int:
    return len(re.sub(r"\s+", "", text or ""))


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
