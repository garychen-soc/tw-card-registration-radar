"""元大銀行：信用卡優惠活動清單 ``promotionActivity/list.do``（表單分頁）。

**未經線上實測。** 2026-11-01 從本機以專案預設 UA 存取清單頁回 HTTP 403
（v1 以瀏覽器 UA 覆寫繞過；本專案不偽造身分，故未沿用）。結構依 v1
``sources/yuanta.toml`` 與 Codex 版 ``_yuanta_cards``：

* 卡片是 ``li``，內含 ``div.pic``，標題在 ``h6``、摘要在 ``p``，明細連結
  ``/promotionActivity/in.do?id=…`` 是 ``div.pic`` 的兄弟節點；
* 分頁以 POST 同一網址，欄位 ``pN``（頁碼）、``pA``（總頁數）、``iA``（總筆數），
  首頁的隱藏欄位帶 ``pA``／``iA``。

清單卡片沒有期間欄位，期間留空。明細頁內容容器未經確認，暫以通用內容根節點
（``main``／``article``…）限縮，需在可存取的環境實測後改成具體 selector。
"""

from __future__ import annotations

import re

from ..models import Stub, Terms
from ..transport import FetchFailed, is_allowed
from .base import Adapter, absolute, canonical, main_text, node_text, tree

_HIDDEN = r'name="{name}"[^>]*value="(\d+)"|value="(\d+)"[^>]*name="{name}"'
_PAGES = re.compile(_HIDDEN.format(name="pA"))
_ITEMS = re.compile(_HIDDEN.format(name="iA"))
_DETAIL = re.compile(r"/promotionActivity/in\.do\?id=[\w-]+")
_MAX_PAGES = 30


class YuantaAdapter(Adapter):
    id = "yuanta"
    name = "元大銀行"
    domains = ["yuantabank.com.tw"]
    entry_url = "https://www.yuantabank.com.tw/bank/creditCard/promotionActivity/list.do"
    portal_url = "https://www.yuantabank.com.tw/bank/creditCard/productActivityMember/list.do"
    portal_hint = "元大銀行卡友活動登錄頁（沿用 v1 設定，本輪未能實測）"

    def discover(self) -> list[Stub]:
        first = self.get(self.entry_url).text
        pages = _int(_PAGES.search(first)) or 1
        items = _int(_ITEMS.search(first))
        found: dict[str, Stub] = {}
        for stub in self._cards(first):
            found.setdefault(stub.key, stub)
        if not found:
            raise FetchFailed(f"清單頁找不到活動卡片：{self.entry_url}")
        for number in range(2, min(pages, _MAX_PAGES) + 1):
            html = self.get(self.entry_url, data={
                "pN": str(number), "pA": str(pages), "iA": str(items),
            }).text
            for stub in self._cards(html):
                found.setdefault(stub.key, stub)
        if pages > _MAX_PAGES:
            self.skipped.append(f"分頁未收齊：公告 {pages} 頁，只讀 {_MAX_PAGES} 頁")
        if items and len(found) < items:
            self.skipped.append(f"分頁未收齊：收到 {len(found)}／公告 {items} 筆")
        return list(found.values())

    def terms(self, stub: Stub) -> Terms:
        page = self.get(stub.url).text
        return Terms(text=main_text(page), title=node_text(tree(page).css_first("h1, h2")))

    def _cards(self, html: str) -> list[Stub]:
        stubs: list[Stub] = []
        for card in tree(html).css("li"):
            if card.css_first("div.pic") is None:
                continue
            href = next(
                (a.attributes.get("href") or "" for a in card.css("a")
                 if _DETAIL.search(a.attributes.get("href") or "")),
                "",
            )
            if not href:
                continue
            url = canonical(absolute(self.entry_url, href))
            if not is_allowed(url, self.domains):
                self.skipped.append(f"非官方網址：{url}")
                continue
            stub = self.stub(
                key=url,
                title=node_text(card.css_first("h6")),
                url=url,
                summary=node_text(card.css_first("p"))[:160],
            )
            if stub:
                stubs.append(stub)
        return stubs


def _int(match: re.Match[str] | None) -> int:
    if not match:
        return 0
    return int(match.group(1) or match.group(2))


ADAPTER = YuantaAdapter
