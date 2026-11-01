"""第一銀行：信用卡活動頁的分類容器，逐分類讀官方 REST。

入口頁只有空的 ``div.showAjaxActivityData`` 容器，每個分類帶
``data-category_id_list`` 等參數，前端拿去 POST
``CardActivityListCTL/zh/queryActivityListByCategory2``（實測 9 個分類；
「所有類別」與首頁的精選容器 ``all_category`` 不讀）。回應 JSON：

* ``activityListData``：當頁 9 筆；
* ``activityListDataAllCategory``：該分類全部活動（2026-11-01 實測 9 個分類都與
  逐頁翻完的聯集逐筆相同，共 177 筆），所以每分類一次請求即可；欄位缺席或與
  當頁不一致時才退回逐頁翻；
* ``pageSpacingHtml``：分頁器。

標題取 ``activityTitle``（空才用 ``headlineEditor``）；``assetName`` 是 CMS 內部
名稱（如「【線上購物】2026年 momo」），不用。期間取 ``activityDate``。

百貨分類的三筆是 ``CardActivitySchedule`` 總表頁：內容是各百貨當期滿額禮的官方
表格（含兌換須知），所以列為活動；但它們是常駐頁（assetName 標「_勿刪」），
``activityDate`` 停在 2021／2024 的舊值，不代表活動期間，留空。

一個分類讀不到時（Codex 版 10/26 曾因此少 27 筆）先重試，仍失敗就把該分類以
「分類失敗：」記入 ``skipped``，其餘分類照常回傳，讓整體可被判為 partial。
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..dates import parse_period
from ..models import Stub, Terms
from ..transport import AccessDenied, FetchFailed, TransportError, is_allowed
from .base import Adapter, absolute, canonical, main_text, node_text, tree

_REST = (
    "https://card.firstbank.com.tw/sites/REST/controller/"
    "CardActivityListCTL/zh/queryActivityListByCategory2"
)
_CATEGORY = re.compile(
    r"data-category_id_list\s*=\s*'([^']*)'\s*"
    r"data-all_category_asset_id\s*=\s*'([^']*)'\s*"
    r"data-category_en_name\s*=\s*'([^']*)'"
)
_PAGE = re.compile(r"第(\d+)頁")
_SCHEDULE = "/CardActivitySchedule"
_MAX_PAGES = 30
_CONTENT = ".page-content .container"
# 「2026.1.1.~2026.12.31」這種多一個句點的寫法會讓起日讀不到
_TRAILING_DOT = re.compile(r"(\d)\.(?=\s*[~～\-])")


class FirstAdapter(Adapter):
    id = "first"
    name = "第一銀行"
    domains = ["firstbank.com.tw"]
    entry_url = "https://card.firstbank.com.tw/sites/card/touch/1565690686288"
    portal_url = "https://ccard.firstbank.com.tw/cmsweb/act/ca_index"
    portal_hint = "第一銀行信用卡活動登錄頁（活動頁「一銀官網活動登錄」連結的目的地）"
    category_attempts = 3
    retry_delay = 2.0

    def discover(self) -> list[Stub]:
        entry = self.get(self.entry_url).text
        categories = _categories(entry)
        if not categories:
            raise FetchFailed(f"入口頁找不到活動分類容器：{self.entry_url}")
        stubs: list[Stub] = []
        seen: set[str] = set()
        read = 0
        for form, label in categories:
            try:
                rows = self._category_rows(form)
            except TransportError as exc:
                self.skipped.append(f"分類失敗：{label}（{exc}）")
                continue
            read += 1
            for row in rows:
                stub = self._row_stub(row, label)
                if stub is None or stub.key in seen:
                    continue
                seen.add(stub.key)
                stubs.append(stub)
        if not read:
            raise FetchFailed(f"{len(categories)} 個活動分類全部讀取失敗")
        return stubs

    def terms(self, stub: Stub) -> Terms:
        doc = tree(self.get(stub.url).text)
        for node in doc.css(".content-share, .content-actions"):
            node.decompose()
        start, end = None, None
        for item in doc.css(".event-list-item"):
            if node_text(item.css_first(".event-list-lead")) == "活動期間":
                start, end = _period(node_text(item).removeprefix("活動期間"))
                break
        return Terms(
            text=main_text(doc.html or "", _CONTENT),
            title=node_text(doc.css_first(".page-header h2")),
            period_start=start,
            period_end=end,
        )

    # ---- 內部 -----------------------------------------------------------

    def _category_rows(self, form: dict[str, str]) -> list[dict[str, Any]]:
        first = self._post(form, 1)
        page_rows = _rows(first.get("activityListData"))
        all_rows = _rows(first.get("activityListDataAllCategory"))
        all_ids = {row.get("id") for row in all_rows}
        if all_rows and all(row.get("id") in all_ids for row in page_rows):
            return all_rows
        # 退回逐頁翻：停止條件是「這一頁沒有新項目」或分頁器的最後一頁
        rows = list(page_rows)
        seen = {row.get("id") for row in rows}
        last = max([1, *(int(n) for n in _PAGE.findall(str(first.get("pageSpacingHtml") or "")))])
        for page in range(2, min(last, _MAX_PAGES) + 1):
            fresh = [
                row for row in _rows(self._post(form, page).get("activityListData"))
                if row.get("id") not in seen
            ]
            if not fresh:
                break
            seen.update(row.get("id") for row in fresh)
            rows.extend(fresh)
        return rows

    def _post(self, form: dict[str, str], page: int) -> dict[str, Any]:
        data = {**form, "pageNumberSel": str(page)}
        error: TransportError = FetchFailed("未嘗試")
        for attempt in range(self.category_attempts):
            if attempt and self.retry_delay:
                time.sleep(self.retry_delay)
            try:
                payload = self.get_json(_REST, data=data)
            except AccessDenied:
                raise
            except TransportError as exc:
                error = exc
                continue
            if isinstance(payload, dict) and "activityListData" in payload:
                return payload
            error = FetchFailed("REST 回應缺少 activityListData")
        raise error

    def _row_stub(self, row: dict[str, Any], label: str) -> Stub | None:
        href = str(row.get("activityUrl") or "").strip()
        if not href:
            self.skipped.append(f"無網址：{row.get('id')}")
            return None
        url = canonical(absolute(self.entry_url, href))
        if not is_allowed(url, self.domains):
            self.skipped.append(f"非官方網址：{url}")
            return None
        title = str(row.get("activityTitle") or "").strip() or _html_text(
            str(row.get("headlineEditor") or "")
        )
        date_text = "" if _SCHEDULE in url else str(row.get("activityDate") or "")
        start, end = _period(date_text)
        return self.stub(
            key=str(row.get("id") or url),
            title=title,
            url=url,
            period_start=start,
            period_end=end,
            summary=_html_text(str(row.get("subheadlineEditor") or ""))[:160],
            category=label,
        )


def _categories(html: str) -> list[tuple[dict[str, str], str]]:
    """入口頁的分類容器 → (REST 表單, 中文分類名)。"""
    doc = tree(html)
    result: list[tuple[dict[str, str], str]] = []
    seen: set[str] = set()
    for ids, all_id, en_name in _CATEGORY.findall(html):
        if en_name == "all_category" or en_name in seen:
            continue
        seen.add(en_name)
        label = node_text(doc.css_first(f"#{en_name}_activity_list_title")) or en_name
        form = {
            "categoryIdList": ids,
            "allCategoryAssetId": all_id,
            "categoryEnName": en_name,
            "pageCategoryType": en_name,
            "searchKeyWord": "",
        }
        result.append((form, label))
    return result


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _period(text: str) -> tuple[Any, Any]:
    return parse_period(_TRAILING_DOT.sub(r"\1", text))


def _html_text(fragment: str) -> str:
    return node_text(tree(f"<div>{fragment}</div>").css_first("div")) if fragment else ""


ADAPTER = FirstAdapter
