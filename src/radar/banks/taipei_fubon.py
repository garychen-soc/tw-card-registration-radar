"""台北富邦銀行：卡友優惠 ``promotion/Result`` 的 Wicket Ajax 分頁。

清單頁一頁 9 張 ``div.discount-card``，頁首印「您搜尋共 N 筆相關結果」。翻頁是
Wicket Ajax：對頁面腳本裡 ``Wicket.Ajax.ajax({"u":"./Result?…-nav-next"})`` 的網址
送 GET（帶 ``Wicket-Ajax``／``Referer`` 標頭，缺 Referer 會被 403），回應是
``<ajax-response>``，內含下一頁的卡片與新的分頁器。

* 非 Ajax 的 ``href`` 版翻頁連結實測回 HTTP 500，不用。
* Wicket 頁面狀態只存在單一節點上：狀態遺失時回應是 ``<redirect>``（v1 實測
  約一半機率）。處置是重新開清單頁、從第 1 頁往後翻，以 ``sn`` 去重，直到收齊或
  用完請求預算。
* 收齊與否以頁首公告筆數對帳；不足時在 ``skipped`` 記「分頁未收齊：」。

標題取卡片 ``h3.discount-name``，期間取 ``.discount-date``，摘要取
``h5.discount-text``。明細內容在 ``main .md-container``，去掉「回上頁」與
「你可能也喜歡」推薦輪播。
"""

from __future__ import annotations

import math
import re

from ..dates import parse_period
from ..models import Stub, Terms
from ..transport import FetchFailed
from .base import Adapter, absolute, canonical, main_text, node_text, tree

_TOTAL = re.compile(r"共\s*(\d+)\s*筆")
_NEXT = re.compile(r'"u"\s*:\s*"(\./Result\?[^"]*-nav-next)"')
_NEXT_DISABLED = re.compile(r'<a[^>]*class="page-link next"[^>]*disabled', re.I)
_REDIRECT = re.compile(r"<redirect>", re.I)
_COMPONENT = re.compile(r"<component[^>]*>\s*<!\[CDATA\[(.*?)\]\]>\s*</component>", re.S)
_SN = re.compile(r"[?&]sn=([A-Za-z0-9]+)")
_MAX_RESTARTS = 6
_CONTENT = "main .md-container"
_DROP = ".back-box, .may-like, .like-swiper"


class TaipeiFubonAdapter(Adapter):
    id = "taipei_fubon"
    name = "台北富邦銀行"
    domains = ["taipeifubon.com.tw", "fubon.com"]
    entry_url = "https://cardpromote.taipeifubon.com.tw/promotion/Result"
    portal_url = "https://www.fubon.com/banking/event/credit_card/20170718A/index.html"
    portal_hint = "台北富邦信用卡網路會員活動登錄頁；部分活動另指定於 Fubon+ App 登錄"

    def discover(self) -> list[Stub]:
        page = self.get(self.entry_url).text
        total_match = _TOTAL.search(page)
        cards = self._cards(page)
        if not cards:
            raise FetchFailed(f"清單頁找不到優惠卡片：{self.entry_url}")
        total = int(total_match.group(1)) if total_match else 0
        pages = math.ceil(total / len(cards)) if total else 1
        found: dict[str, Stub] = {stub.key: stub for stub in cards}
        budget = pages * 3 + 5
        restarts = 0
        headers = {
            "Wicket-Ajax": "true",
            "Wicket-Ajax-BaseURL": "promotion/Result",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/xml, text/xml, */*; q=0.01",
            "Referer": self.entry_url,
        }
        while budget > 0 and not (total and len(found) >= total):
            next_url = _next_url(page)
            if not next_url:
                break
            budget -= 1
            response = self.get(absolute(self.entry_url, next_url), headers=headers).text
            landed = [] if _REDIRECT.search(response) else self._cards(_ajax_markup(response))
            if not landed:
                # 狀態遺失：重開清單頁，從頭往後翻（已收的以 sn 去重）
                restarts += 1
                if restarts > _MAX_RESTARTS:
                    break
                budget -= 1
                page = self.get(self.entry_url).text
                continue
            page = response  # 下一個 nav-next 網址在 <evaluate> 腳本裡
            for stub in landed:
                found.setdefault(stub.key, stub)
        if total and len(found) < total:
            self.skipped.append(f"分頁未收齊：收到 {len(found)}／公告 {total} 筆")
        return list(found.values())

    def terms(self, stub: Stub) -> Terms:
        doc = tree(self.get(stub.url).text)
        for node in doc.css(_DROP):
            node.decompose()
        start, end = parse_period(node_text(doc.css_first(".detail-date")))
        return Terms(
            text=main_text(doc.html or "", _CONTENT),
            title=node_text(doc.css_first("h2.detail-title")),
            period_start=start,
            period_end=end,
        )

    def _cards(self, html: str) -> list[Stub]:
        stubs: list[Stub] = []
        for card in tree(html).css("div.discount-card"):
            link = card.css_first("a.discount-link") or card.css_first('a[href*="sn="]')
            href = (link.attributes.get("href") or "") if link is not None else ""
            sn = _SN.search(href)
            if not sn:
                continue
            url = canonical(absolute(self.entry_url, href))
            period = node_text(card.css_first(".discount-date"))
            start, end = parse_period(period)
            stub = self.stub(
                key=sn.group(1),
                title=node_text(card.css_first(".discount-name")),
                url=url,
                period_start=start,
                period_end=end,
                summary=node_text(card.css_first(".discount-text"))[:160],
            )
            if stub:
                stubs.append(stub)
        return stubs


def _next_url(html: str) -> str:
    if _NEXT_DISABLED.search(html):
        return ""
    match = _NEXT.search(html)
    return match.group(1) if match else ""


def _ajax_markup(xml: str) -> str:
    """``<ajax-response>`` 裡各 ``<component>`` 的 CDATA。HTML 解析器會把 CDATA
    當成註解吃掉，所以先取出來。"""
    return "\n".join(_COMPONENT.findall(xml))


ADAPTER = TaipeiFubonAdapter
