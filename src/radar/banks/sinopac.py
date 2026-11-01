"""永豐銀行：「刷卡享優惠」總覽頁 ``discount/list.html`` 的 ``li[data-id]`` 卡片。

21 張卡片全部在伺服器 HTML 裡（「更多介紹」只是前端把隱藏的 li 顯示出來）。
標題取卡片 ``h2 a``，摘要取 ``p``。期間取卡片圖片的 ``data-period``
（``YYYYMMDDhhmmss-YYYYMMDDhhmmss``，前端 period.js 用它決定上下架）；迄日
2999 年是常駐頁的佔位值，不是活動期間，起訖都留空。

部分卡片是主題頁（如「旅遊優惠輕鬆享」），頁內以 ``.tab-box`` 分列多個子活動；
一個官方頁面＝一筆活動，子活動交給後段切分。明細內容在 ``#mainArea``，先去掉
頁內分頁選單 ``.slidebar``／``#tabs``。
"""

from __future__ import annotations

import re
from datetime import date

from ..models import Stub, Terms
from ..transport import FetchFailed, is_allowed
from .base import Adapter, absolute, canonical, main_text, node_text, tree

_PERIOD = re.compile(r"^(\d{4})(\d{2})(\d{2})\d*-(\d{4})(\d{2})(\d{2})\d*$")
_DETAIL = re.compile(r"/discount/\d{6,}(?:_H)?\.html$")
_PLACEHOLDER_YEAR = 2999


class SinopacAdapter(Adapter):
    id = "sinopac"
    name = "永豐銀行"
    domains = ["sinopac.com"]
    entry_url = "https://bank.sinopac.com/sinopacBT/personal/credit-card/discount/list.html"
    portal_url = "https://bank.sinopac.com/sinopacBT/personal/credit-card/discount/list.html"
    portal_hint = "查無統一登錄頁；請由刷卡享優惠總覽進入各活動頁，依頁內指定方式登錄"

    def discover(self) -> list[Stub]:
        page = tree(self.get(self.entry_url).text)
        cards = page.css("li[data-id]")
        if not cards:
            raise FetchFailed(f"總覽頁找不到活動卡片：{self.entry_url}")
        stubs: list[Stub] = []
        seen: set[str] = set()
        for card in cards:
            link = card.css_first("h2 a")
            href = (link.attributes.get("href") or "") if link is not None else ""
            url = canonical(absolute(self.entry_url, href), keep_query=False) if href else ""
            if not url or not _DETAIL.search(url) or not is_allowed(url, self.domains):
                self.skipped.append(f"非活動明細連結：{href or card.attributes.get('data-id')}")
                continue
            if url in seen:
                continue
            seen.add(url)
            image = card.css_first("[data-period]")
            start, end = _period(image.attributes.get("data-period") or "" if image else "")
            stub = self.stub(
                key=url,
                title=node_text(link),
                url=url,
                period_start=start,
                period_end=end,
                summary=node_text(card.css_first("p"))[:160],
            )
            if stub:
                stubs.append(stub)
        return stubs

    def terms(self, stub: Stub) -> Terms:
        doc = tree(self.get(stub.url).text)
        for node in doc.css("#mainArea .slidebar, #mainArea #tabs"):
            node.decompose()
        title = node_text(doc.css_first("title")).split("|")[0].strip()
        return Terms(text=main_text(doc.html or "", "#mainArea"), title=title)


def _period(value: str) -> tuple[date | None, date | None]:
    match = _PERIOD.match(value.strip())
    if not match:
        return None, None
    ay, am, ad, by, bm, bd = (int(x) for x in match.groups())
    if by >= _PLACEHOLDER_YEAR:
        return None, None
    try:
        return date(ay, am, ad), date(by, bm, bd)
    except ValueError:
        return None, None


ADAPTER = SinopacAdapter
