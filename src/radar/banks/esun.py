"""玉山銀行：刷卡優惠總覽（Sitecore 表單分頁）＋ shopInfo 明細。

總覽頁 ``/discount/shops`` 的卡片由 ``DiscountSearchResult`` 端點 POST 回傳 HTML
片段，``category`` 留空就是全部分類；每頁 9 筆，總筆數在 ``id="total"``，總頁數
＝ceil(總筆數 ÷ 首頁筆數)。標題取卡片 ``.l-cardDiscountAllContent__discount--title``。

明細有兩種：

* ``shopInfo?sno=`` 留在官網：內文是並列的 ``div.l-cardDiscountContent``
  （活動優惠、店家資訊、注意事項各一塊）。有些只有優惠摘要，完整辦法與注意事項
  在「了解更多」連到的 ``event.esunbank.com.tw`` 活動網頁 —— 一併讀進來接在後面。
* ``shopInfo?sno=`` 直接 302 到 ``event.esunbank.com.tw``：讀該頁的 ``main``。

活動網頁結尾的「謹慎理財 信用無價」法定警語與費率說明不是活動條款，截掉。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..dates import parse_period
from ..models import Stub, Terms
from ..transport import is_allowed
from .base import Adapter, absolute, canonical, main_text, node_text, tree

_LIST = "https://www.esunbank.com/api/client/CreditCardDiscount/DiscountSearchResult?sc_lang=zh-TW"
_FORM = {
    "itemID": "{D594B641-F7C9-458D-AD85-FE9B53220895}",
    "rootUrl": "/zh-tw/personal/credit-card/discount/shopInfo",
    "category": "",
    "keywords": "",
}
_TOTAL = re.compile(r'id="total"\s+value="(\d+)"')
_MAX_PAGES = 60
# 明細內容是好幾個並列的 div.l-cardDiscountContent：第一個是「活動優惠」，
# 其後的分頁（店家資訊、注意事項…）沒有 data-comp-name，要全部收。
_CONTENT = "div.l-cardDiscountContent"
_EVENT_HOSTS = ("event.esunbank.com.tw", "esun.co")
_MORE = re.compile(r"了解更多|活動詳情|活動辦法|詳細內容|看更多")
_LEGAL = re.compile(r"\n謹慎理財[\s\S]*$")
_MAX_LINKED = 2
# 列表裡混著的非活動卡片：App 使用教學、點數兌換查詢、數位服務介紹、信貸方案。
_NOT_ACTIVITY = re.compile(r"使用教學|線上查詢|數位服務|信貸|貸款")


class EsunAdapter(Adapter):
    id = "esun"
    name = "玉山銀行"
    # esun.co 是玉山自己的短網址網域（DigiCert EV，O=E.SUN COMMERCIAL BANK），
    # 官方頁用它連到 event.esunbank.com.tw。
    domains = ["esunbank.com.tw", "esunbank.com", "esun.co"]
    entry_url = "https://www.esunbank.com/zh-tw/personal/credit-card/discount/shops"
    portal_url = "https://www.esunbank.com/zh-tw/personal/credit-card/discount/shops"
    portal_hint = "玉山信用卡刷卡優惠專區的「活動登錄」，或玉山 Wallet App 登錄"

    def discover(self) -> list[Stub]:
        first = self._page(1)
        cards = _cards(first)
        size = len(cards)
        match = _TOTAL.search(first)
        total = int(match.group(1)) if match else size
        pages = min(-(-total // size), _MAX_PAGES) if size else 1
        if size and -(-total // size) > _MAX_PAGES:
            self.skipped.append(f"總筆數 {total} 超過 {_MAX_PAGES} 頁上限，只讀前 {_MAX_PAGES} 頁")
        for page in range(2, pages + 1):
            more = _cards(self._page(page))
            if not more:
                break
            cards.extend(more)

        stubs: list[Stub] = []
        seen: set[str] = set()
        for href, title, summary in cards:
            url = canonical(absolute(self.entry_url, href))
            if url in seen:
                continue
            seen.add(url)
            if _NOT_ACTIVITY.search(title):
                continue
            start, end = parse_period(summary) if "活動期間" in summary else (None, None)
            stub = self.stub(key=url, title=title, url=url, summary=summary,
                             period_start=start, period_end=end)
            if stub:
                stubs.append(stub)
        if len(seen) < total:
            self.skipped.append(f"官方總數 {total}，列表只讀到 {len(seen)} 筆不重複卡片")
        return stubs

    def terms(self, stub: Stub) -> Terms:
        response = self.get(stub.url)
        page = response.text
        doc = tree(page)
        if _is_event(response.final_url):
            text = _LEGAL.sub("", main_text(page, "main"))
            title = node_text(doc.css_first("h1"))
            start, end = _labelled_period(text)
            return Terms(text=text, title=title, period_start=start, period_end=end)

        blocks = [node.html or "" for node in doc.css(_CONTENT)]
        text = main_text("<main>" + "".join(blocks) + "</main>") if blocks else ""
        title = node_text(doc.css_first("h1"))
        start, end = _labelled_period(text)
        for link in _linked_events(doc, response.final_url)[:_MAX_LINKED]:
            if not is_allowed(link, self.domains):
                continue
            extra = _LEGAL.sub("", main_text(self.get(link).text, "main"))
            if extra:
                text = f"{text}\n{extra}"
        return Terms(text=text, title=title, period_start=start, period_end=end)

    def _page(self, number: int) -> str:
        return self.get(_LIST, data={**_FORM, "currentPage": str(number)}).text


def _cards(html: str) -> list[tuple[str, str, str]]:
    out = []
    for card in tree(html).css("div.paginationList"):
        anchor = card.css_first("a[href]")
        href = anchor.attributes.get("href") or "" if anchor else ""
        if "shopInfo" not in href:
            continue
        title = node_text(card.css_first(".l-cardDiscountAllContent__discount--title"))
        summary = node_text(card.css_first(".l-cardDiscountAllContent__discount--word"))
        out.append((href, title, summary))
    return out


def _is_event(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host.endswith(_EVENT_HOSTS)


def _linked_events(doc, base: str) -> list[str]:  # type: ignore[no-untyped-def]
    """明細內容區裡「了解更多」這類連到活動網頁的連結，依出現順序去重。"""
    links: list[str] = []
    anchors = [a for block in doc.css(_CONTENT) for a in block.css("a[href]")]
    for anchor in anchors:
        url = absolute(base, anchor.attributes.get("href") or "")
        if _is_event(url) and _MORE.search(node_text(anchor)) and url not in links:
            links.append(url)
    return links


def _labelled_period(text: str):  # type: ignore[no-untyped-def]
    """內文第一個「活動期間」標示的日期區間。"""
    index = text.find("活動期間")
    return parse_period(text[index:index + 60]) if index >= 0 else (None, None)


ADAPTER = EsunAdapter
