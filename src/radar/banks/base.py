"""銀行擷取器介面。

每家銀行一個模組，只回答兩件事：

``discover(http)``
    官方列表／API 上有哪些活動 → ``list[Stub]``。
``terms(http, stub)``
    某筆活動的條款文字 → ``Terms``。

**硬規則（違反就是 v1 的錯誤重演）：**

1. ``Stub.title`` 只能取自結構化欄位：列表卡片的標題元素、API 的 title
   欄位。找不到標題的卡片整筆丟棄並計入 ``skipped``，絕不拿內文第一行頂替。
2. 列表上的導覽、分類、App 下載、公益、紅利兌換等非活動入口不列為 Stub。
3. ``Terms.text`` 必須限縮到主要內容容器（用 ``main_text(html, selector)``），
   導覽列、頁尾、側欄、「相關活動」連結不得混入。
4. 同一份明細被多張卡片指到時，照樣各回一個 Stub；合併由 pipeline 依條款
   內容雜湊處理（標題會保留成 aliases）。
5. 只用 ``self.get()`` 取網頁 —— 它帶網域白名單、節流與重試；不登入、不解
   CAPTCHA、不偽造身分。
6. 解析不到就留空（``None`` / ``""``），不推測。登錄時點完全不在這層處理。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

from ..htmltext import scope_html, to_text
from ..models import Stub, Terms
from ..textnorm import normalize
from ..transport import Fetcher, Response

Transport = Literal["http", "browser"]


class Adapter(ABC):
    id: ClassVar[str]
    name: ClassVar[str]
    domains: ClassVar[list[str]]
    entry_url: ClassVar[str]
    portal_url: ClassVar[str] = ""
    portal_hint: ClassVar[str] = ""
    transport: ClassVar[Transport] = "http"
    min_host_interval: ClassVar[float] = 0.5

    def __init__(self, http: Fetcher) -> None:
        self.http = http
        self.skipped: list[str] = []

    # ---- 子類實作 -------------------------------------------------------

    @abstractmethod
    def discover(self) -> list[Stub]: ...

    @abstractmethod
    def terms(self, stub: Stub) -> Terms: ...

    # ---- 共用工具 -------------------------------------------------------

    def get(
        self, url: str, *, data: dict[str, str] | None = None, headers: dict[str, str] | None = None
    ) -> Response:
        return self.http.get(url, data=data, headers=headers, conditional=False)

    def get_json(
        self, url: str, *, data: dict[str, str] | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        return self.get(url, data=data, headers=headers).json()

    def stub(self, *, key: str, title: str, url: str, **fields: Any) -> Stub | None:
        """建 Stub；標題不合格就記入 ``skipped`` 並回 None。"""
        clean = clean_title(title)
        if not clean:
            self.skipped.append(f"無標題：{url}")
            return None
        return Stub(bank_id=self.id, key=key, title=clean, url=url, **fields)


_SPACE = re.compile(r"\s+")


def clean_title(title: str) -> str:
    """收斂空白與不可見字元；全空就回空字串。不做任何內容改寫。"""
    return _SPACE.sub(" ", normalize(title or "")).strip()


def tree(html: str) -> LexborHTMLParser:
    return LexborHTMLParser(html)


def node_text(node: Any) -> str:
    if node is None:
        return ""
    return _SPACE.sub(" ", node.text(separator=" ", strip=True) or "").strip()


def main_text(html: str, selector: str = "", *, tab_label: str = "") -> str:
    """主要內容區純文字。``selector`` 指向該銀行明細頁的內容容器。"""
    return normalize(to_text(scope_html(html, selector=selector, tab_label=tab_label)))


def absolute(base: str, href: str) -> str:
    return urljoin(base, href.strip())


def canonical(url: str, *, keep_query: bool = True) -> str:
    """去掉 fragment；必要時去掉 query。用於 Stub.key。"""
    parts = urlsplit(url.strip())
    query = parts.query if keep_query else ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))
