"""陽信銀行：信用卡「專案活動專區」的 ``div.ad-link div.card`` 卡片。

標題取卡片 ``.card-title``，期間取 ``.card-text`` 的「活動期間／活動日期」。
只收逐活動的官方明細（``/activityevent/…``、``/net/Prg/ActNewsView/N``、
``/creditcard/wcardinfo/``）；卡組織、紅利兌換、公益、App、Sunny Pay 等導覽
入口不列。

其中三張導覽卡片本身是信用卡活動的子列表（同樣的卡片版型），會再展開一層：
「熱門網購」（每月 momo／蝦皮等滿額登錄活動）、「特店優惠」、「卡友日」。
子列表與主列表重複的明細只收一次。

不少 ``/activityevent/`` 頁與部分 ActNewsView 是整張圖片，HTML 幾乎沒有文字；
``Terms.text`` 照實回傳，不補值。
"""

from __future__ import annotations

import re
from collections import Counter

from ..dates import parse_period
from ..models import Stub, Terms
from .base import Adapter, absolute, canonical, clean_title, main_text, node_text, tree

_DETAIL = re.compile(
    r"/activityevent/[^/]+/\w+\.html$|/net/Prg/ActNewsView/\d+$|/creditcard/wcardinfo/$"
)
_APP = re.compile(r"sunnybank_app", re.I)
_SUBLIST = re.compile(r"/net/Main/Page/(\d+)$")
# 展開的子列表：頁碼 → 名稱。其餘 /net/Main/Page/N 是導覽入口。
SUBLISTS = {48: "熱門網購", 64: "特店優惠", 66: "天天陽信卡友日"}
_PERIOD = re.compile(r"活動(?:期間|日期)\s*:?\s*(.{0,40})")
_PERIOD_LABEL = re.compile(r"^活動(?:期間|日期)\s*(?::|$)", re.M)
CONTENT = "#mainbody .txt"


def _cards(html: str, base: str) -> list[tuple[str, str, str]]:
    """(title, url, card_text)；url 已去 fragment。"""
    found = []
    for card in tree(html).css("#mainbody div.ad-link div.card"):
        anchor = card.css_first("a[href]")
        href = (anchor.attributes.get("href") or "") if anchor else ""
        found.append((
            node_text(card.css_first(".card-title")),
            canonical(absolute(base, href)) if href else "",
            node_text(card.css_first(".card-text")),
        ))
    return found


class SunnyAdapter(Adapter):
    id = "sunny"
    name = "陽信銀行"
    domains = ["sunnybank.com.tw"]
    entry_url = "https://www.sunnybank.com.tw/net/Main/Page/6?onews=1"
    portal_url = "https://www.sunnybank.com.tw/portal/pt/pt01002/PT01002Index.xhtml"
    portal_hint = "陽信銀行網路銀行「活動登錄」"

    def discover(self) -> list[Stub]:
        response = self.get(self.entry_url)
        pages = [("專案活動專區", _cards(response.text, response.final_url))]
        if not pages[0][1]:
            raise ValueError("專案活動專區沒有任何卡片；頁面結構可能已改版")

        for _title, url, _ in pages[0][1]:
            match = _SUBLIST.search(url)
            if match and int(match.group(1)) in SUBLISTS:
                sub = self.get(url)
                pages.append((SUBLISTS[int(match.group(1))], _cards(sub.text, sub.final_url)))

        stubs: list[Stub] = []
        seen: set[str] = set()
        for category, cards in pages:
            fresh = []
            for title, url, text in cards:
                if not _DETAIL.search(url) or _APP.search(url):
                    match = _SUBLIST.search(url)
                    if not (match and int(match.group(1)) in SUBLISTS):
                        self.skipped.append(f"導覽入口：{title}｜{url}")
                    continue
                if url in seen:
                    continue
                fresh.append((title, url, text))
            shared = Counter(url for _, url, _ in fresh)
            for title, url, text in fresh:
                period = _PERIOD.search(text)
                start, end = parse_period(period.group(1)) if period else (None, None)
                stub = self.stub(
                    key=f"{url}#{clean_title(title)}" if shared[url] > 1 else url,
                    title=title,
                    url=url,
                    period_start=start,
                    period_end=end,
                    summary=text[:160],
                    category="" if category == "專案活動專區" else category,
                )
                if stub:
                    stubs.append(stub)
            seen.update(url for _, url, _ in fresh)
        return stubs

    def terms(self, stub: Stub) -> Terms:
        page = tree(self.get(stub.url).text)
        heading = page.css_first(f"{CONTENT} h3") or page.css_first("title")
        title = node_text(heading)
        # 「回上頁」按鈕、頁首選單與 <title> 不是內容。
        for node in page.css("#btn-back, header, title"):
            node.decompose()
        html = page.html or ""
        selector = CONTENT if page.css_first(CONTENT) is not None else "body"
        text = main_text(html, selector)
        start, end = None, None
        labels = _PERIOD_LABEL.findall(text)
        if len(labels) == 1:
            start, end = parse_period(text.split(labels[0], 1)[1][:60])
        return Terms(text=text, title=title, period_start=start, period_end=end)


ADAPTER = SunnyAdapter
