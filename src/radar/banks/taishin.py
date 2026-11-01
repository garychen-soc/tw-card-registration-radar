"""台新銀行：信用卡優惠 A–I 九大分類，逐分類讀官方 JSON 分頁。

分類頁 ``/tscccms/promotion/offerList/{A..I}`` 帶 ``_csrf`` 與 ``#totalPage``；
卡片由 ``getMorePromotionList`` 以 POST（``queryPage`` 從 1 起）回 JSON 陣列，
每筆有 ``promotionId``、``promotionName``、``promotionStartDate``／
``promotionEndDate``（UTC ISO，轉台北日期）、``promotionBrief``。分類頁 HTML 上
的卡片是 JSON 第 1 頁的子集（實測 9 分類皆然），所以只讀 JSON。

標題取 ``promotionName``（錨點文字只是「瞭解詳情」）。同一活動掛在多個分類時
以 ``promotionId`` 去重。明細內容在 ``.page-content``。

某分類讀不到時記「分類失敗：」，其餘照常回傳。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..dates import parse_period
from ..models import Stub, Terms
from ..transport import FetchFailed, TransportError
from .base import Adapter, main_text, node_text, tree

_BASE = "https://mkpcard.taishinbank.com.tw/tscccms"
_LIST = _BASE + "/promotion/offerList/{code}"
_MORE = _BASE + "/promotion/getMorePromotionList/"
_DETAIL = _BASE + "/promotion/detail/{id}"
_CSRF = re.compile(r'name="_csrf"\s+value="([^"]+)"')
_TOTAL_PAGE = re.compile(r'id="totalPage"[^>]*value="(\d+)"')
_ID = re.compile(r"^WM_\d+$")
_MAX_PAGES = 50
_TAIPEI = ZoneInfo("Asia/Taipei")
CATEGORIES = {
    "A": "旅遊及海外購物", "B": "美饌料理", "C": "加油交通", "D": "百貨購物",
    "E": "網購及3C", "F": "量販超市超商", "G": "娛樂及影城", "H": "保險及稅費",
    "I": "其他優惠",
}


class TaishinAdapter(Adapter):
    id = "taishin"
    name = "台新銀行"
    domains = ["taishinbank.com.tw"]
    entry_url = "https://mkpcard.taishinbank.com.tw/tscccms/promotion/offerList/A"
    portal_url = "https://mkpcard.taishinbank.com.tw/tscccms/signin"
    portal_hint = "台新信用卡活動網站登入後登錄；部分活動另指定於 Richart Life 或合作商網頁登錄"

    def discover(self) -> list[Stub]:
        stubs: list[Stub] = []
        seen: set[str] = set()
        read = 0
        for code, label in CATEGORIES.items():
            try:
                rows = self._category_rows(code)
            except TransportError as exc:
                self.skipped.append(f"分類失敗：{code} {label}（{exc}）")
                continue
            read += 1
            for row in rows:
                stub = self._row_stub(row, label)
                if stub is None or stub.key in seen:
                    continue
                seen.add(stub.key)
                stubs.append(stub)
        if not read:
            raise FetchFailed("A–I 九個分類全部讀取失敗")
        return stubs

    def terms(self, stub: Stub) -> Terms:
        page = self.get(stub.url).text
        text = main_text(page, ".page-content")
        tail = text.split("活動期間", 1)[1][:80] if "活動期間" in text else ""
        start, end = parse_period(tail)
        return Terms(
            text=text,
            title=node_text(tree(page).css_first("h1.content-title")),
            period_start=start,
            period_end=end,
        )

    def _category_rows(self, code: str) -> list[dict[str, Any]]:
        url = _LIST.format(code=code)
        page = self.get(url).text
        token, total = _CSRF.search(page), _TOTAL_PAGE.search(page)
        if not token or not total:
            raise FetchFailed(f"分類頁缺少 _csrf 或 totalPage：{url}")
        rows: list[dict[str, Any]] = []
        for number in range(1, min(int(total.group(1)), _MAX_PAGES) + 1):
            payload = self.get_json(_MORE, data={
                "_csrf": token.group(1), "categoryId": code, "queryStoreType": "null",
                "queryRegionId": "null", "queryKeyWord": "", "queryOrderAscDesc": "Desc",
                "queryPage": str(number),
            }, headers={
                "X-Requested-With": "XMLHttpRequest", "Referer": url,
                "Accept": "application/json, text/javascript, */*; q=0.01",
            })
            if not isinstance(payload, list):
                raise FetchFailed(f"分頁回應不是陣列：{code} 第 {number} 頁")
            rows.extend(row for row in payload if isinstance(row, dict))
        if int(total.group(1)) > _MAX_PAGES:
            raise FetchFailed(f"分類 {code} 頁數 {total.group(1)} 超過上限 {_MAX_PAGES}")
        return rows

    def _row_stub(self, row: dict[str, Any], label: str) -> Stub | None:
        promotion_id = str(row.get("promotionId") or "").strip()
        if not _ID.match(promotion_id):
            self.skipped.append(f"無效活動代碼：{promotion_id!r}")
            return None
        return self.stub(
            key=promotion_id,
            title=str(row.get("promotionName") or ""),
            url=_DETAIL.format(id=promotion_id),
            period_start=_taipei_date(row.get("promotionStartDate")),
            period_end=_taipei_date(row.get("promotionEndDate")),
            summary=str(row.get("promotionBrief") or "").strip()[:160],
            category=label,
        )


def _taipei_date(value: Any) -> date | None:
    """``2026-09-21T16:00:00.000+00:00`` → 台北日期 2026-09-22。"""
    try:
        moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(_TAIPEI).date()


ADAPTER = TaishinAdapter
