"""王道銀行：活動總覽頁的 ``article.o-article`` 卡片。

總覽頁混有存款、換匯、投資活動；只收「活動詳情」連到
``event-announce/debit_*`` 的簽帳金融卡活動。標題取卡片 ``.heading``，
期間取卡片 ``.description`` 的「活動期間」。
"""

from __future__ import annotations

import re

from ..dates import parse_period
from ..models import Stub, Terms
from .base import Adapter, absolute, canonical, main_text, node_text, tree

_DEBIT = re.compile(r"/event-announce/debit_[\w-]+")


class ObankAdapter(Adapter):
    id = "obank"
    name = "王道銀行"
    domains = ["o-bank.com"]
    entry_url = "https://www.o-bank.com/retail/event/event-compaign"
    portal_url = "https://www.o-bank.com/egift/RegisterCampaign"
    portal_hint = "王道銀行活動登錄頁"

    def discover(self) -> list[Stub]:
        page = tree(self.get(self.entry_url).text)
        stubs: list[Stub] = []
        for card in page.css("article.o-article"):
            href = next(
                (
                    a.attributes.get("href") or ""
                    for a in card.css("a")
                    if _DEBIT.search(a.attributes.get("href") or "")
                ),
                "",
            )
            if not href:
                continue
            url = canonical(absolute(self.entry_url, href), keep_query=False)
            description = node_text(card.css_first(".description"))
            period = description.split("活動期間", 1)[-1] if "活動期間" in description else ""
            start, end = parse_period(period[:60])
            stub = self.stub(
                key=url,
                title=node_text(card.css_first(".heading")),
                url=url,
                period_start=start,
                period_end=end,
                summary=description[:160],
            )
            if stub:
                stubs.append(stub)
        return stubs

    def terms(self, stub: Stub) -> Terms:
        page = self.get(stub.url).text
        return Terms(text=main_text(page, "main"), title=node_text(tree(page).css_first("h1")))


ADAPTER = ObankAdapter
