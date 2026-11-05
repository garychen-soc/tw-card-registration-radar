"""元大銀行：信用卡優惠活動清單 ``promotionActivity/list.do``（表單分頁）。

**瀏覽器 UA 例外（Gary 2026-11-05 核准）。** 官網 WAF 會擋帶專案識別字串的
預設 UA（HTTP 403），一般瀏覽器 UA 則回 200；故本擷取器覆寫 ``user_agent``。
只換 UA，不登入、不解 CAPTCHA，網域白名單與節流照舊。結構依 v1
``sources/yuanta.toml`` 與 Codex 版 ``_yuanta_cards``：

* 卡片是 ``li``，內含 ``div.pic``，標題在 ``h6``、摘要在 ``p``，明細連結
  ``/promotionActivity/in.do?id=…`` 是 ``div.pic`` 的兄弟節點；
* 分頁以 POST 同一網址，欄位 ``pN``（頁碼）、``pA``（總頁數）、``iA``（總筆數），
  首頁的隱藏欄位帶 ``pA``／``iA``。

清單卡片沒有期間欄位；明細頁（2026-11-05 實測）內容在 ``div.event_info``，
標題 ``h6``，期間取自「活動時間：」那一列 ``li``。
"""

from __future__ import annotations

import re

from ..dates import parse_period
from ..models import Stub, Terms
from ..transport import FetchFailed, is_allowed
from .base import BROWSER_USER_AGENT, Adapter, absolute, canonical, main_text, node_text, tree

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
    user_agent = BROWSER_USER_AGENT

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
        html = self.get(stub.url).text
        box = tree(html).css_first("div.event_info")
        if box is None:
            return Terms(text="")
        start, end = None, None
        for row in box.css("li"):
            label = node_text(row.css_first("div.tit"))
            if label.startswith("活動時間"):
                start, end = parse_period(node_text(row).removeprefix(label))
                break
        return Terms(
            text=main_text(html, "div.event_info"),
            title=node_text(box.css_first("h6")),
            period_start=start,
            period_end=end,
        )

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
