"""星展銀行：「刷卡優惠」頁的結構化磚塊＋三個官方子清單。

入口 ``cards-offers/default.page`` 是 Next.js 頁，``__NEXT_DATA__`` 內的
``categoryTiles`` 就是頁面上五個分類（精選、旅遊、餐飲、購物、繳費）的
卡片：標題取 ``DBS_Title``、摘要取 ``DBS_SubTitle``、網址取
``DBS_FriendlyURL``。精選分類與各分類重複的同一張卡（同網址同標題）只收一次。

其中三張磚塊是「清單頁」，展開成各自的卡片，標題取清單卡片上的商店名，
不讓多個商店共用一個標題：

* ``offers/ce/default.html`` 網購：``.brand-card a`` 的 ``.brand_name``
* ``offers/installment/default.html`` 分期：``li.store_box`` 的 ``h2``、
  ``p.date`` 期間
* ``offers/department_store/default.html`` 百貨：``ul.kv_mall a`` 的文字；
  只有 logo 圖、alt 為空的三家百貨沒有可用標題，依規則丟棄並記入 ``skipped``

文章、攻略、紅利商城頁（``*.page``、``cardguide``、``insurance-payment-guide``、
``fly_redeem_apple``）不是活動，不列。
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from ..dates import parse_period
from ..models import Stub, Terms
from ..transport import Fetcher
from .base import Adapter, absolute, canonical, main_text, node_text, tree

SITE = "https://www.dbs.com.tw/"
_HUBS = {
    "/personal-zh/cards/offers/ce/default.html": "mall",
    "/personal-zh/cards/offers/installment/default.html": "installment",
    "/personal-zh/cards/offers/department_store/default.html": "department",
}
_ACTIVITY_PATH = re.compile(r"^/personal-zh/cards/(?:offers?/[\w-]+|MGM)/")
_NOT_ACTIVITY = ("/cardguide/", "/insurance-payment-guide/")
_CHROME = ("header", "footer", "nav", ".fix-ad", "#myModal", ".go_top", ".overlay")
_PERIOD = re.compile(r"(?<![一-鿿])活動(?:期間|時間)\s*[:：]?\s*(.{6,60})")


def _plain(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value or "")


def _tiles(data: Any) -> list[dict[str, Any]]:
    """在 ``__NEXT_DATA__`` 裡找所有 ``categoryTiles``（不綁死路徑）。"""
    found: list[dict[str, Any]] = []
    if isinstance(data, dict):
        tiles = data.get("categoryTiles")
        if isinstance(tiles, list):
            found.extend(t for t in tiles if isinstance(t, dict))
        for value in data.values():
            found.extend(_tiles(value))
    elif isinstance(data, list):
        for value in data:
            found.extend(_tiles(value))
    return found


def terms_period(text: str) -> tuple[date | None, date | None]:
    match = _PERIOD.search(text)
    return parse_period(match.group(1)) if match else (None, None)


def unique_keys(stubs: list[Stub]) -> list[Stub]:
    """同一網址被多張卡片指到時，鍵改成「網址|標題」以保持唯一。"""
    counts: dict[str, int] = {}
    for stub in stubs:
        counts[stub.key] = counts.get(stub.key, 0) + 1
    return [
        s.model_copy(update={"key": f"{s.key}|{s.title}"}) if counts[s.key] > 1 else s
        for s in stubs
    ]


class DbsAdapter(Adapter):
    id = "dbs"
    name = "星展銀行"
    domains = ["dbs.com.tw"]
    entry_url = "https://www.dbs.com.tw/personal-zh/cards/cards-offers/default.page"
    portal_url = (
        "https://www.dbs.com.tw/personal-zh/digital-service/cardplus/deeplink/index.html"
        "?SERVICE_ID=camp_registration&source=pweb"
    )
    portal_hint = "星展 Card+ App「活動登錄」（官網連結會導向 App）"

    def __init__(self, http: Fetcher) -> None:
        super().__init__(http)
        self._seen: set[tuple[str, str]] = set()
        self._hubs_done: set[str] = set()

    def discover(self) -> list[Stub]:
        page = tree(self.get(self.entry_url).text)
        script = page.css_first("script#__NEXT_DATA__")
        data = json.loads(script.text() if script is not None else "{}")
        self._seen.clear()
        self._hubs_done.clear()
        stubs: list[Stub] = []
        for category in _tiles(data):
            label = node_text(tree(str(category.get("title") or "")).body)
            for tile in category.get("tilesContent") or []:
                href = str(tile.get("DBS_FriendlyURL") or tile.get("DBS_LearnMore") or "")
                title = _plain(str(tile.get("DBS_Title") or ""))
                stubs.extend(self._entry(href, title, label, str(tile.get("DBS_SubTitle") or "")))
        return unique_keys(stubs)

    def _entry(self, href: str, title: str, category: str, summary: str = "",
               period: tuple[date | None, date | None] = (None, None)) -> list[Stub]:
        url = canonical(absolute(SITE, href))
        path = urlsplit(url).path
        if path in _HUBS:
            return self._hub(url, _HUBS[path], title)
        if not _ACTIVITY_PATH.match(path) or any(x in path for x in _NOT_ACTIVITY):
            self.skipped.append(f"非活動頁：{title}｜{url}")
            return []
        stub = self.stub(key=url, title=title, url=url, category=category,
                         summary=_plain(summary)[:160],
                         period_start=period[0], period_end=period[1])
        if stub is None or (stub.url, stub.title) in self._seen:
            return []
        self._seen.add((stub.url, stub.title))
        return [stub]

    def _hub(self, url: str, kind: str, label: str) -> list[Stub]:
        hub = canonical(url, keep_query=False)
        if hub in self._hubs_done:
            return []
        self._hubs_done.add(hub)
        page = tree(self.get(hub).text)
        label = re.sub(r"\s+", " ", label).strip()
        stubs: list[Stub] = []
        if kind == "mall":
            for link in page.css(".brand-card a[href]"):
                name = node_text(link.css_first(".brand_name"))
                stubs.extend(self._entry(absolute(hub, link.attributes.get("href") or ""),
                                         name, label))
        elif kind == "installment":
            for box in page.css("li.store_box"):
                anchor = box.css_first("a[href]")
                if anchor is None:
                    continue
                period = parse_period(node_text(box.css_first("p.date")))
                stubs.extend(self._entry(absolute(hub, anchor.attributes.get("href") or ""),
                                         node_text(box.css_first("h2")), label,
                                         node_text(box.css_first("p.info")), period))
        else:
            for link in page.css("ul.kv_mall a[href]"):
                stubs.extend(self._entry(absolute(hub, link.attributes.get("href") or ""),
                                         node_text(link), label))
        return stubs

    def terms(self, stub: Stub) -> Terms:
        html = self.get(stub.url).text
        page = tree(html)
        title = node_text(page.css_first("title"))
        for selector in _CHROME:
            for node in page.css(selector):
                node.decompose()
        scope = (page.css_first("section.main-content") or page.css_first("main")
                 or page.body)
        text = main_text(scope.html or "", "") if scope is not None else ""
        start, end = terms_period(text)
        return Terms(text=text, title=title, period_start=start, period_end=end)


ADAPTER = DbsAdapter
