"""凱基銀行：信用卡活動清單（含分頁）。

清單頁 ``/personal/promotion/card-campaign`` 以 ``?p=N`` 分頁（v1 只讀第 1 頁，
漏了 2、3 頁共 23 筆）。分頁連結從頁面上的 ``?p=N`` 讀出，逐頁讀到沒有新頁為止。
每張卡片的「了解更多」（``a.btn--arrowRight``）指向
``card-campaign/<slug>``；標題取卡片的 ``.fs-h3``，期間取卡片說明
``.kgibStatic011__item-title`` 的「活動期間：115/9/1~115/11/30」（民國年）。
同一明細被兩張卡片指到（誠品）時照樣各回一個 Stub。

明細主文限縮在 ``main``，並移除分享列、Cookie 告知與頁底「謹慎理財」警語。
"""

from __future__ import annotations

import re
from datetime import date

from ..dates import parse_period
from ..models import Stub, Terms
from .base import Adapter, absolute, canonical, main_text, node_text, tree

_DETAIL = re.compile(r"/personal/promotion/card-campaign/[\w-]+$")
_PAGE = re.compile(r"card-campaign\?p=(\d+)")
_CHROME = (".kgibOther003__sharegroup", ".cookie-wrapperBox", ".campaign__signupModal")
_PERIOD = re.compile(r"(?<![一-鿿])活動(?:期間|時間)\s*[:：]?\s*(.{6,60})")
MAX_PAGES = 20


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


class KgiAdapter(Adapter):
    id = "kgi"
    name = "凱基銀行"
    domains = ["kgibank.com.tw", "kgibank.com"]
    entry_url = "https://www.kgibank.com.tw/zh-tw/personal/promotion/card-campaign"
    portal_url = "https://www.kgibank.com/creditcard/campaign/registrationlist"
    portal_hint = "凱基銀行信用卡活動登錄清單，找到對應活動登錄"

    def discover(self) -> list[Stub]:
        stubs: list[Stub] = []
        seen: set[tuple[str, str]] = set()
        pending: list[int] = [1]
        done: set[int] = set()
        while pending and len(done) < MAX_PAGES:
            number = pending.pop(0)
            done.add(number)
            url = self.entry_url if number == 1 else f"{self.entry_url}?p={number}"
            html = self.get(url).text
            for found in sorted({int(n) for n in _PAGE.findall(html)}):
                if found not in done and found not in pending:
                    pending.append(found)
            for stub in self._cards(html):
                if (stub.url, stub.title) not in seen:
                    seen.add((stub.url, stub.title))
                    stubs.append(stub)
        return unique_keys(stubs)

    def _cards(self, html: str) -> list[Stub]:
        stubs: list[Stub] = []
        for link in tree(html).css("a.btn--arrowRight[href]"):
            href = link.attributes.get("href") or ""
            if not _DETAIL.search(href.split("?")[0]):
                continue
            card = link.parent
            while card is not None and card.css_first(".fs-h3") is None:
                card = card.parent
            if card is None:
                continue
            note = card.css_first(".kgibStatic011__item-title")
            summary = node_text(note)
            start, end = parse_period(summary.split("活動期間", 1)[-1][:40]) \
                if "活動期間" in summary else (None, None)
            url = canonical(absolute(self.entry_url, href), keep_query=False)
            stub = self.stub(key=url, title=node_text(card.css_first(".fs-h3")), url=url,
                             period_start=start, period_end=end, summary=summary[:160])
            if stub:
                stubs.append(stub)
        return stubs

    def terms(self, stub: Stub) -> Terms:
        html = self.get(stub.url).text
        page = tree(html)
        for selector in _CHROME:
            for node in page.css(selector):
                node.decompose()
        for node in page.css(".bg-warm-white"):
            if node_text(node).startswith("謹慎理財"):
                node.decompose()
        scope = page.css_first("main")
        text = main_text(scope.html or "", "") if scope is not None else ""
        start, end = terms_period(text)
        return Terms(text=text, title=node_text(tree(html).css_first("h1")),
                     period_start=start, period_end=end)


ADAPTER = KgiAdapter
