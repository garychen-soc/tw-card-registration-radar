"""中國信託：官網「優惠資訊」JSON ＋ LINE Pay 卡六大生活圈優惠頁。

兩個來源，都在 ``www.ctbcbank.com`` 免驗證可讀：

``/web/content/twrbo/setting/offerdata.offerdatalist.json``
    官網信用卡優惠專區（``cc_offer``）與首頁優惠元件共用的設定檔，
    官方前端（``CPTL-HME-QSH-OfferSearch``）依 ``offerCategory`` 與
    ``onTime``／``offTime`` 篩出顯示中的優惠。這裡照同一規則篩：
    ``cc_offer``，以及路徑在 ``/creditcard/`` 下的 60 週年活動；
    存款、外幣、信貸、網銀（``ib_offer``／``wm_offer``／``loan_offer``）不收。
    標題取 ``offerTitle``，期間取 ``offerStart``／``offerEnd``。

``/content/dam/minisite/long/creditcard/LINEPay/page_<分類>.html``
    LINE Pay 卡六大生活圈。每張 ``li.card-list__item`` 是一個商店優惠，
    標題取卡片標題圖的 alt（官方以圖片呈現商店名），明細是同頁的
    ``#lightbox_<id>`` 對話框。多張卡片共用同一個對話框（ShopBack、
    雅詩蘭黛集團）時照樣各回一個 Stub。卡片連到 LINE Pay 官網
    （``web-tw-pay.line.me``）的「LINE Pay 支付」優惠不是本行條款，
    不在白名單內，記入 ``skipped``。

已知缺口：``/twrbo/`` 下的分類頁（``cc_offer_food.html`` 等）與
``mkt.ctbcbank.com`` 活動頁有 JS 挑戰（HTTP 202），一般 HTTP 讀不到；
這裡不解挑戰，明細抓到 202 時拋 ``AccessDenied``，由 pipeline 記為明細失敗。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..dates import parse_period
from ..models import Stub, Terms
from ..transport import AccessDenied, Fetcher, is_allowed
from .base import Adapter, absolute, canonical, main_text, node_text, tree

TAIPEI = ZoneInfo("Asia/Taipei")
SITE = "https://www.ctbcbank.com/"
LINEPAY = "https://www.ctbcbank.com/content/dam/minisite/long/creditcard/LINEPay/"
LINEPAY_PAGES = ("food", "shopping", "fashion", "pet", "life", "travel")
# 明細頁的頁首、頁尾、浮動按鈕。各 minisite 版型不同，只列實測看到的樣板。
_CHROME = (
    "nav", "header", "footer", "#footer", ".footer", ".fix-left", ".icon-top",
)
_PERIOD = re.compile(r"(?<![\u4e00-\u9fff])活動(?:期間|時間|日期)\s*[:：]?\s*(.{6,60})")


def _when(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%Y/%m/%d %H:%M:%S").replace(tzinfo=TAIPEI)
    except (AttributeError, ValueError):
        return None


def _day(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%Y/%m/%d").date()
    except (AttributeError, ValueError):
        return None


def terms_period(text: str) -> tuple[date | None, date | None]:
    match = _PERIOD.search(text)
    return parse_period(match.group(1)) if match else (None, None)


def unique_keys(stubs: list[Stub]) -> list[Stub]:
    """同一網址被多張卡片指到時，鍵改成「網址|標題」以保持唯一。"""
    counts: dict[str, int] = {}
    for stub in stubs:
        counts[stub.key] = counts.get(stub.key, 0) + 1
    return [
        s.model_copy(update={"key": f"{s.key}|{s.title}"}) if counts[s.key] > 1 else s
        for s in stubs
    ]


class CtbcAdapter(Adapter):
    id = "ctbc"
    name = "中國信託"
    domains = ["ctbcbank.com"]
    entry_url = "https://www.ctbcbank.com/web/content/twrbo/setting/offerdata.offerdatalist.json"
    portal_url = (
        "https://www.ctbcbank.com/twrbo/zh_tw/onlinecounter_index/cc_service/"
        "cc_service_register.html"
    )
    portal_hint = "中國信託官網「信用卡活動登錄」頁，找到對應活動登錄"

    def __init__(self, http: Fetcher) -> None:
        super().__init__(http)
        self._pages: dict[str, str] = {}

    def now(self) -> datetime:
        return datetime.now(TAIPEI)

    # ---- discover ---------------------------------------------------------

    def discover(self) -> list[Stub]:
        # LINE Pay 明細全部可讀，排前面；優惠 JSON 有 7 筆在 mkt 子網域（JS 驗證）
        stubs = self._linepay_stubs() + self._offer_stubs()
        return unique_keys(stubs)

    def _offer_stubs(self) -> list[Stub]:
        now = self.now()
        stubs: list[Stub] = []
        seen: set[tuple[str, str]] = set()
        for item in self.get_json(self.entry_url):
            cats = item.get("offerCategory") or []
            path = str(item.get("offerPath") or "").strip()
            if not ("cc_offer" in cats or ("60th_annivesary" in cats and "/creditcard/" in path)):
                continue
            on, off = _when(item.get("onTime", "")), _when(item.get("offTime", ""))
            if (on and on > now) or (off and off < now):
                continue  # 官方前端同樣不顯示：未上架或已下架
            url = absolute(SITE, path)
            if not is_allowed(url, self.domains):
                self.skipped.append(f"非官方網域：{url}")
                continue
            title = str(item.get("offerTitle") or "")
            if (url, title) in seen:
                continue
            seen.add((url, title))
            stub = self.stub(
                key=url,
                title=title,
                url=url,
                period_start=_day(item.get("offerStart", "")),
                period_end=_day(item.get("offerEnd", "")),
                category="信用卡優惠",
            )
            if stub:
                stubs.append(stub)
        return stubs

    def _linepay_stubs(self) -> list[Stub]:
        stubs: list[Stub] = []
        for name in LINEPAY_PAGES:
            page_url = f"{LINEPAY}page_{name}.html"
            html = self._page(page_url)
            for card in tree(html).css("li.card-list__item"):
                img = card.css_first(".card__title img")
                alt = (img.attributes.get("alt") or "") if img is not None else ""
                title = node_text(card.css_first(".card__title .sr-only")) or alt
                button = card.css_first(".card__footer [data-target]")
                target = (button.attributes.get("data-target") or "") if button else ""
                if not target.startswith("#"):
                    link = card.css_first(".card__footer a[href]")
                    href = (link.attributes.get("href") or "") if link else ""
                    self.skipped.append(f"外站明細（LINE Pay 支付）：{title}｜{href}")
                    continue
                body = card.css_first(".card__body")
                date_text = node_text(card.css_first(".card__date"))
                start, end = parse_period(date_text)
                url = page_url + target
                stub = self.stub(
                    key=url,
                    title=title,
                    url=url,
                    period_start=start,
                    period_end=end,
                    summary=node_text(body).replace(date_text, "").strip()[:160],
                    category="LINE Pay卡",
                )
                if stub:
                    stubs.append(stub)
        return stubs

    # ---- terms ------------------------------------------------------------

    def _page(self, url: str) -> str:
        if url not in self._pages:
            response = self.get(url)
            if response.status_code == 202:
                raise AccessDenied(f"中國信託頁面回 JS 驗證（HTTP 202），不解驗證：{url}")
            self._pages[url] = response.text
        return self._pages[url]

    def terms(self, stub: Stub) -> Terms:
        parts = urlsplit(stub.url)
        page_url = canonical(stub.url, keep_query=True)
        html = self._page(page_url)
        page = tree(html)
        if "/LINEPay/page_" in parts.path and parts.fragment.startswith("lightbox_"):
            box = page.css_first(f"#{parts.fragment}")
            if box is None:
                return Terms(text="", title="")
            heading = box.css_first("h3 img")
            text = main_text(html, f"#{parts.fragment}")
            start, end = terms_period(text)
            return Terms(
                text=text,
                title=(heading.attributes.get("alt") or "") if heading else "",
                period_start=start,
                period_end=end,
            )
        for selector in _CHROME:
            for node in page.css(selector):
                node.decompose()
        # 同頁多家商店（如旅遊玩家 ``stroke.html#lion``）只取片段指到的區塊
        scope = page.body
        if parts.fragment and re.fullmatch(r"[\w-]+", parts.fragment):
            hits = page.css(f"#{parts.fragment}") or page.css(f".{parts.fragment}")
            if len(hits) == 1:
                scope = hits[0]
        text = main_text(scope.html or "", "") if scope is not None else ""
        start, end = terms_period(text)
        return Terms(
            text=text,
            title=node_text(tree(html).css_first("title")),
            period_start=start,
            period_end=end,
        )


ADAPTER = CtbcAdapter
