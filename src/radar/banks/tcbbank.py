"""台中銀行：信用卡中心的 Promotion Entrance API。

信用卡中心（``/creditcard/J_01.html``）的卡片由 ``/WWWAPI/api/Promotion/Entrance?ID=2``
一次回傳（圖片以 base64 內嵌，整包約 5MB）。標題取 ``Title``；``SubTitle`` 是
「(至115年11月30日有效)」這種官方截止日，拿來當 ``period_end``。API 的
``StartDate``／``EndDate`` 是卡片上下架時間（EndDate 恰為截止日隔天，StartDate
有 2023 年的常設卡片），不是活動期間，所以不當 ``period_start``。

明細有四種：

* ``/Promotion/Info.html?ID=``：整頁是一張圖（``api/Promotion/Info`` 回傳 base64
  圖檔＋image map），沒有任何文字 —— 條款留空，不去抓那張圖。
* ``/news/news_content.asp``：舊版公告頁，內容區 ``.text_content``，多數也是圖片。
* ``/CreditCard/J_*.html``：舊版產品頁，內容區 ``.text_content``；新版產品頁有 ``main``。
* ``/CreditCard/event/*/index.html``：活動小站，沒有語意容器，取全文並截掉
  「謹慎理財 信用無價」之後的法定警語。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from ..dates import parse_period
from ..models import Stub, Terms
from .base import Adapter, absolute, canonical, main_text, node_text, tree

_API = "https://www.tcbbank.com.tw/WWWAPI/api/Promotion/Entrance?ID=2"
_IMAGE_ONLY = re.compile(r"/Promotion/Info\.html", re.I)
# 紅利點數兌換說明不是活動（硬規則 2）。
_REDEMPTION = re.compile(r"紅利.{0,20}兌換")
_LEGAL = re.compile(r"\n謹慎理財[\s\S]*$")
_SELECTORS = ("main", ".text_content")
_HEADING = re.compile(r"^優惠訊息(?:\n|$)")


class TcbbankAdapter(Adapter):
    id = "tcbbank"
    name = "台中銀行"
    domains = ["tcbbank.com.tw"]
    entry_url = "https://www.tcbbank.com.tw/creditcard/J_01.html"
    portal_url = "https://www.tcbbank.com.tw/CreditCard/RegQuery2/Act_login.aspx"
    portal_hint = "台中銀行信用卡活動登錄頁，輸入卡號等資料後選擇活動登錄"

    def discover(self) -> list[Stub]:
        payload = self.get_json(_API)
        rows = payload.get("row") if isinstance(payload, dict) else None
        stubs: list[Stub] = []
        keys: set[str] = set()
        for row in sorted(rows or [], key=_order):
            if not isinstance(row, dict):
                continue
            title = str(row.get("Title") or "")
            raw_url = str(row.get("Url") or "").strip()
            if not raw_url or _REDEMPTION.search(title):
                continue
            url = canonical(absolute(self.entry_url, raw_url))
            key = url if url not in keys else f"{url}#{title.strip()}"
            if key in keys:
                continue
            subtitle = str(row.get("SubTitle") or "")
            _, end = parse_period(subtitle)
            stub = self.stub(
                key=key,
                title=title,
                url=url,
                period_end=end or _day_before(row.get("EndDate")),
                summary=subtitle.strip(),
            )
            if stub:
                keys.add(key)
                stubs.append(stub)
        return stubs

    def terms(self, stub: Stub) -> Terms:
        if _IMAGE_ONLY.search(stub.url):
            # 官方內容只有圖片，沒有可解析的文字（硬規則 6：解析不到就留空）。
            return Terms(text="")
        page = self.get(stub.url).text
        doc = tree(page)
        node = next((n for n in map(doc.css_first, _SELECTORS) if n is not None), doc.body)
        # 包一層 <main>，避免 main_text 在容器內只挑第一個 <article>。
        text = main_text(f"<main>{node.html}</main>") if node is not None else ""
        heading = doc.css_first("h1") or doc.css_first("title")
        # 舊版公告頁的內容是圖片，文字只剩欄目標題「優惠訊息」，那不是條款。
        text = _LEGAL.sub("", _HEADING.sub("", text))
        return Terms(text=text, title=node_text(heading))


def _order(row: object) -> tuple[int, int]:
    """官方顯示順序：BlockSeq（區塊）再 Sort。"""
    if not isinstance(row, dict):
        return (0, 0)

    def num(value: object) -> int:
        try:
            return int(str(value))
        except ValueError:
            return 0

    return (num(row.get("BlockSeq")), num(row.get("Sort")))


def _day_before(value: object) -> date | None:
    """EndDate 是下架時刻（截止日隔天 00:00），退一天才是截止日。"""
    try:
        return (datetime.fromisoformat(str(value)) - timedelta(days=1)).date()
    except ValueError:
        return None


ADAPTER = TcbbankAdapter
