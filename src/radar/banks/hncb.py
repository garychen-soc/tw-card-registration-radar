"""華南銀行：熱門優惠頁「信用卡」分頁面板的 ``.feature-item`` 卡片。

熱門優惠頁有存款、基金、保險、貸款等六個分頁；只讀 ``aria-label="信用卡"``
的 tab 以 ``aria-controls`` 指到的面板。標題取卡片 ``.feature-title a`` 的
錨點文字。列表沒有期間，Stub 期間一律留空。

面板裡混有外站（可樂旅遊、eztable、公益捐款等）、卡別禮遇導覽頁（只列
國際組織外站連結）、PDF 型 DM 與服務介紹頁，因此只收三種本行活動明細：

* ``/msg/co-management/pb-best-offer/``：單一活動明細。
* ``/card/introduce/``：卡片專案頁（例：熊Q卡，頁內多個活動彈窗）。
* ``urile=…/card/event/channel/``：主題活動頁（例：旅遊），面板上常有數張卡片
  共用同一頁；照樣各回一個 Stub，key 加上標題以保持唯一。

其餘一律記入 ``skipped`` 並註明原因。
"""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import unquote, urlsplit

from ..dates import parse_period
from ..models import Stub, Terms
from .base import Adapter, absolute, canonical, clean_title, main_text, node_text, tree

_HOST = "www.hncb.com.tw"
_ACTIVITY = (
    re.compile(r"/wps/portal/HNCB/msg/co-management/pb-best-offer/"),
    re.compile(r"/wps/portal/HNCB/card/introduce/"),
    re.compile(r"urile=wcm:path:/wps/wcm/connect/hncb_v2/site_map/hncb/card/event/channel/"),
)
_SKIP_REASONS = (
    (re.compile(r"/card_banner/"), "PDF 型 DM（彙整，非單一活動）"),
    (re.compile(r"/site_map/hncb/card/event/"), "卡別禮遇導覽頁（只列外站連結）"),
    (re.compile(r"/per_finance/|/service/"), "服務介紹頁"),
)
CONTENT = "div.page-content"
_PERIOD_LABEL = re.compile(r"^活動期間\s*(?::|$)", re.M)


def _panel_id(html: str, label: str) -> str:
    for anchor in tree(html).css("a[role=tab]"):
        if (anchor.attributes.get("aria-label") or "").strip() == label:
            return anchor.attributes.get("aria-controls") or ""
    return ""


def _strip_hidden(html: str) -> str:
    """移除螢幕閱讀器提示（「以下為重要訊息」「關閉視窗」），它們會把句子切斷。"""
    page = tree(html)
    for node in page.css(".visually-hidden"):
        node.decompose()
    return page.html or html


class HncbAdapter(Adapter):
    id = "hncb"
    name = "華南銀行"
    domains = ["hncb.com.tw"]
    entry_url = (
        "https://www.hncb.com.tw/wps/portal/HNCB/card/?1dmy&urile=wcm%3apath%3a"
        "/wps/wcm/connect/hncb_v2/site_map/hncb/per_finance/service/public_item/best_offer"
    )
    portal_url = (
        "https://netbank.hncb.com.tw/netbank/servlet/TrxDispatcher"
        "?trx=com.lb.wibc.trx.CardPromoteOverall_RWD&state=prompt"
    )
    portal_hint = "華南銀行網路銀行「信用卡活動登錄」"

    def discover(self) -> list[Stub]:
        response = self.get(self.entry_url)
        panel_id = _panel_id(response.text, "信用卡")
        panel = tree(response.text).css_first(f"#{panel_id}") if panel_id else None
        if panel is None:
            raise ValueError("找不到「信用卡」分頁面板；頁面結構可能已改版")

        cards: list[tuple[str, str]] = []
        for item in panel.css(".feature-item"):
            anchor = item.css_first(".feature-title a")
            if anchor is None:
                continue
            href = (anchor.attributes.get("href") or "").strip().lstrip("+")
            url = canonical(absolute(response.final_url, href))
            title = node_text(anchor)
            reason = self._reject(url)
            if reason:
                self.skipped.append(f"{reason}：{title}｜{url}")
                continue
            cards.append((title, url))

        shared = Counter(url for _, url in cards)
        stubs: list[Stub] = []
        for title, url in cards:
            key = f"{url}#{clean_title(title)}" if shared[url] > 1 else url
            stub = self.stub(key=key, title=title, url=url)
            if stub:
                stubs.append(stub)
        return stubs

    @staticmethod
    def _reject(url: str) -> str:
        if urlsplit(url).hostname != _HOST:
            return "外站"
        decoded = unquote(url)
        if any(pattern.search(decoded) for pattern in _ACTIVITY):
            return ""
        for pattern, reason in _SKIP_REASONS:
            if pattern.search(decoded):
                return reason
        return "非活動明細"

    def terms(self, stub: Stub) -> Terms:
        html = _strip_hidden(self.get(stub.url).text)
        text = main_text(html, CONTENT)
        start, end = None, None
        # 只有單一個「活動期間」標籤時才當整頁期間；多活動頁（旅遊、熊Q卡）留空不猜。
        labels = _PERIOD_LABEL.findall(text)
        if len(labels) == 1:
            start, end = parse_period(text.split(labels[0], 1)[1][:80])
        return Terms(text=text, title=node_text(tree(html).css_first("title")),
                     period_start=start, period_end=end)


ADAPTER = HncbAdapter
