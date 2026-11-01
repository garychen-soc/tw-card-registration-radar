"""彰化銀行：信用卡優惠活動總頁 → 四大分類 → 各活動明細。

總頁 ``bonusDetail.jsp?id=3657`` 的編輯區（``.content-inside``）以四張圖片
連到分類頁（美食饗宴、購物消費、旅遊住宿、其他優惠），分類名取圖片 alt。
分類頁編輯區內每個 ``a.editor_link`` 是一個活動，標題取連結文字（v1 只讀了
5 頁；Codex 只收「【」開頭的標題，漏掉美食、旅遊、其他分類的大半）。

官方編輯區有幾筆連結寫成內網位址 ``http://10.100.6.38/frontend/bonusDetail.jsp?id=N``。
這裡不跟隨該位址，而是只在「路徑是 bonusDetail.jsp、參數只有數字 id」時
改寫成官網同一支程式 ``https://www.bankchb.com/frontend/bonusDetail.jsp?id=N``
（實測官網同 id 即同一活動）；其餘非官方連結記入 ``skipped``。

明細主文在 ``.content-inside``。活動下架後官網仍回 200 但編輯區不存在，
此時回空字串，不退回整頁（整頁只有選單）。
"""

from __future__ import annotations

import ipaddress
import re
from datetime import date
from urllib.parse import urlsplit

from ..dates import parse_period
from ..models import Stub, Terms
from ..transport import is_allowed
from .base import Adapter, absolute, main_text, node_text, tree

SITE = "https://www.bankchb.com/frontend/"
_DETAIL = re.compile(r"^id=(\d+)$")
_PERIOD = re.compile(r"(?<![一-鿿])活動(?:期間|時間)\s*[:：]?\s*(.{6,60})")


def terms_period(text: str) -> tuple[date | None, date | None]:
    match = _PERIOD.search(text)
    return parse_period(match.group(1)) if match else (None, None)


def detail_url(href: str, base: str) -> str:
    """回傳官網 bonusDetail 網址；不是活動明細就回空字串。"""
    url = absolute(base, href)
    parts = urlsplit(url)
    match = _DETAIL.match(parts.query)
    if parts.path.rstrip("/").split("/")[-1] != "bonusDetail.jsp" or not match:
        return ""
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
        internal = True
    except ValueError:
        internal = False
    if internal or host == "www.bankchb.com":
        return f"{SITE}bonusDetail.jsp?id={match.group(1)}"
    return url


class ChbAdapter(Adapter):
    id = "chb"
    name = "彰化銀行"
    domains = ["bankchb.com"]
    entry_url = "https://www.bankchb.com/frontend/bonusDetail.jsp?id=3657"
    portal_url = "https://www.bankchb.com/frontend/CampaignLog.jsp"
    portal_hint = "彰化銀行官網「活動登錄」頁，找到對應活動登錄"

    def discover(self) -> list[Stub]:
        hub = tree(self.get(self.entry_url).text).css_first(".content-inside")
        categories: list[tuple[str, str]] = []
        for link in hub.css("a.editor_link_img[href]") if hub is not None else []:
            url = detail_url(link.attributes.get("href") or "", self.entry_url)
            img = link.css_first("img")
            label = (img.attributes.get("alt") or "") if img is not None else ""
            if url and url != self.entry_url and (url, label) not in categories:
                categories.append((url, label))
        skip = {self.entry_url, *(url for url, _ in categories)}
        stubs: list[Stub] = []
        seen: set[str] = set()
        for category_url, label in categories:
            body = tree(self.get(category_url).text).css_first(".content-inside")
            for link in body.css("a.editor_link[href]") if body is not None else []:
                href = link.attributes.get("href") or ""
                url = detail_url(href, category_url)
                if not url:
                    continue  # 「本行網站」等非活動連結
                if not is_allowed(url, self.domains):
                    self.skipped.append(f"非官方明細連結：{href}")
                    continue
                if url in skip or url in seen:
                    continue
                stub = self.stub(key=url, title=node_text(link), url=url, category=label)
                if stub:
                    seen.add(url)
                    stubs.append(stub)
        return stubs

    def terms(self, stub: Stub) -> Terms:
        html = self.get(stub.url).text
        page = tree(html)
        title = node_text(page.css_first("title")).removeprefix("彰化銀行-優惠訊息-")
        if page.css_first(".content-inside") is None:
            return Terms(text="", title=title)
        text = main_text(html, ".content-inside")
        start, end = terms_period(text)
        return Terms(text=text, title=title, period_start=start, period_end=end)


ADAPTER = ChbAdapter
