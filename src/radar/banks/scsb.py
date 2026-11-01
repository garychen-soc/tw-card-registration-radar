"""上海商銀：信用卡「熱門活動」清單。

清單頁 ``/content/card/hotList`` 的「全部」分類一次列出所有卡片（無分頁）。
每張 ``div.card`` 的標題取 ``.main_title``、期間取 ``.date``、分類取
``.distag``、網址取 ``a.stretched-link``。明細有 ``hotPage/<id>`` 與舊版
``news_*.html`` 兩種，版型相同：主文在 ``.rightCol``，移除分享列與麵包屑。
"""

from __future__ import annotations

import re
from datetime import date

from ..dates import parse_period
from ..models import Stub, Terms
from .base import Adapter, absolute, canonical, main_text, node_text, tree

_DETAIL = re.compile(r"^/content/card/(?:hotPage/\d+|news_[\w-]+\.html)$")
_CHROME = (".sharebar", ".breadcrumb")
_PERIOD = re.compile(r"(?<![一-鿿])活動(?:期間|時間)\s*[:：]?\s*(.{6,60})")


def terms_period(text: str) -> tuple[date | None, date | None]:
    match = _PERIOD.search(text)
    return parse_period(match.group(1)) if match else (None, None)


class ScsbAdapter(Adapter):
    id = "scsb"
    name = "上海商銀"
    domains = ["scsb.com.tw"]
    entry_url = "https://www.scsb.com.tw/content/card/hotList"
    portal_url = "https://ebank.scsb.com.tw/ternal/ofwbcv05/page#/ofwb/cv/05/01"
    portal_hint = "上海商銀網路銀行「信用卡活動登錄」頁（免登入網銀）"

    def discover(self) -> list[Stub]:
        stubs: list[Stub] = []
        seen: set[str] = set()
        for card in tree(self.get(self.entry_url).text).css("div.card"):
            link = card.css_first("a.stretched-link[href]")
            if link is None:
                continue
            url = canonical(absolute(self.entry_url, link.attributes.get("href") or ""),
                            keep_query=False)
            if not _DETAIL.match(url.split("scsb.com.tw", 1)[-1]):
                self.skipped.append(f"非活動明細：{url}")
                continue
            if url in seen:
                continue
            start, end = parse_period(node_text(card.css_first(".date")))
            stub = self.stub(
                key=url,
                title=node_text(card.css_first(".main_title")),
                url=url,
                period_start=start,
                period_end=end,
                summary=node_text(card.css_first(".sub_title"))[:160],
                category=node_text(card.css_first(".distag")),
            )
            if stub:
                seen.add(url)
                stubs.append(stub)
        return stubs

    def terms(self, stub: Stub) -> Terms:
        html = self.get(stub.url).text
        page = tree(html)
        for selector in _CHROME:
            for node in page.css(selector):
                node.decompose()
        scope = page.css_first(".rightCol")
        text = main_text(scope.html or "", "") if scope is not None else ""
        start, end = terms_period(text)
        title = node_text(page.css_first(".rightCol h1") or page.css_first(".rightCol h2"))
        return Terms(text=text, title=title, period_start=start, period_end=end)


ADAPTER = ScsbAdapter
