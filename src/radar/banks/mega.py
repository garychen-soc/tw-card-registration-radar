"""兆豐銀行：信用卡優惠總覽的 DiscountOverview API。

總覽頁是 JS 渲染的；卡片由 ``DiscountOverview/GetDiscount`` 一次回傳全部（約 300 筆），
「需登錄／強檔／最新」與卡別、通路、地區篩選都在前端做，所以一次請求就是完整清單。
POST 參數是 Sitecore 元件 GUID（itemID、settingID），官方改版重建元件就會換 ——
屆時端點回 HTTP 200 但內容是空的，這裡直接報錯，不靜默歸零。

* 標題：``Title``。
* 期間：``Description``（「活動期間：115/11/6~11/9」）。
* 網址：``DetailPageLinkHtml`` 是整個 ``<a>`` 標籤，取 href；沒有 href 的卡片在官網
  上不可點，記入 skipped。
* ``Removal=c-card--disabled`` 是官方「已下架」標記，不收。

明細內文在 ``.l-majorBlock__item--text``：同一個 ``main`` 裡的分享列（上方）、
「您可能有興趣」推薦卡片與法定警語（下方）都在它外面。
"""

from __future__ import annotations

import re

from ..dates import parse_period
from ..models import Stub, Terms
from .base import Adapter, absolute, canonical, clean_title, main_text, node_text, tree

_API = (
    "https://www.megabank.com.tw/api/client/DiscountOverview/GetDiscount"
    "?sc_lang=zh-TW&sc_site=bank-zh-tw&dic_lang=zh-TW"
)
_FORM = {
    "itemID": "{F9A985A0-EA67-4BF8-A865-9E1522CBC298}",
    "settingID": "{2C3011E0-9805-415A-87BA-9E4B02EE09BA}",
}
_HREF = re.compile(r"""href=["']([^"']+)""")
_CONTENT = ".l-majorBlock__item--text"


class MegaAdapter(Adapter):
    id = "mega"
    name = "兆豐銀行"
    domains = ["megabank.com.tw"]
    entry_url = "https://www.megabank.com.tw/personal/credit-card/event/overview"
    portal_url = "https://www.megabank.com.tw/personal/credit-card/event/overview"
    portal_hint = "兆豐信用卡優惠總覽（篩選「需登錄」），於各活動頁的登錄按鈕完成登錄"

    def discover(self) -> list[Stub]:
        rows = self.get_json(_API, data=dict(_FORM))
        if not isinstance(rows, list) or not rows:
            raise ValueError("兆豐 GetDiscount 回傳空清單（Sitecore 元件 GUID 可能已變更）")
        live = [row for row in rows if isinstance(row, dict) and not row.get("Removal")]
        hrefs: list[str] = []
        for row in live:
            match = _HREF.search(str(row.get("DetailPageLinkHtml") or ""))
            hrefs.append(canonical(absolute(self.entry_url, match.group(1))) if match else "")
        shared = {url for url in hrefs if url and hrefs.count(url) > 1}

        stubs: list[Stub] = []
        seen: set[str] = set()
        for row, url in zip(live, hrefs, strict=True):
            title = clean_title(str(row.get("Title") or ""))
            if not url:
                self.skipped.append(f"卡片沒有明細連結：{title}")
                continue
            key = f"{url}#{title}" if url in shared else url
            if key in seen:
                continue
            seen.add(key)
            description = clean_title(str(row.get("Description") or ""))
            start, end = parse_period(description)
            raw_tags = row.get("Tags")
            tags = raw_tags if isinstance(raw_tags, list) else []
            stub = self.stub(
                key=key,
                title=title,
                url=url,
                period_start=start,
                period_end=end,
                summary=description,
                category="、".join(str(tag) for tag in tags),
            )
            if stub:
                stubs.append(stub)
        return stubs

    def terms(self, stub: Stub) -> Terms:
        page = self.get(stub.url).text
        doc = tree(page)
        node = doc.css_first(_CONTENT) or doc.css_first("main")
        # 包一層 <main>：main_text 會在容器內再挑第一個夠長的 <article>，而自訂
        # 版型的活動頁（例如旅展）每個贈品都是一個 <article>，不包就只剩第一個。
        text = main_text(f"<main>{node.html}</main>") if node is not None else ""
        start, end = _labelled_period(text)
        heading = doc.css_first(f"{_CONTENT} h1, {_CONTENT} h2") or doc.css_first("title")
        return Terms(text=text, title=node_text(heading), period_start=start, period_end=end)


def _labelled_period(text: str):  # type: ignore[no-untyped-def]
    index = text.find("活動期間")
    return parse_period(text[index:index + 60]) if index >= 0 else (None, None)


ADAPTER = MegaAdapter
