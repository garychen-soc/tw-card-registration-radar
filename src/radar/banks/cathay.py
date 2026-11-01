"""國泰世華：AEM 活動清單 JSON ＋ 明細頁的 ``.model.json``。

活動專區（``/cathaybk/personal/event/overview``）是 React SPA，HTML 只有外殼。
「信用卡」分頁的資料源是 ``overview/credit-card.model.list.json``，一次回傳全部
活動（另一個「數位服務」分頁是證券、外幣、基金，不收）。

* 標題：卡片顯示的 ``campaignTitle``（官方用換行排版），沒有才退回 ``jcr:title``。
* 期間：``startDate``／``endDate`` 是 UTC 時間戳，台北時間要加 8 小時 ——
  ``2025-12-31T16:00:00Z`` 是台北的 2026-01-01，直接取日期會早一天。
* 網址：``campaignPath`` 是 AEM 內部路徑 ``/content/cub-aem-cs/zh-tw/...html``，
  去掉前綴與 ``.html`` 就是官方公開網址（官網自己也會 302 到無副檔名版本）。
* 明細：HTML 是 SPA 外殼（純文字只有幾十字），內容在同路徑的 ``.model.json``。
  只讀 ``root/responsivegrid/cub_main`` 這棵元件樹 —— 頁首、頁尾是另外的
  experience fragment，不在這棵樹裡。

「小樹點(信用卡)」分類底下的 ``/credit-card/bonus/`` 多半是點數兌換、累積辦法、
FAQ 這類常設說明頁；只有標題看得出是期間活動（加碼、回饋）的才收。
"""

from __future__ import annotations

import html as htmllib
import re
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from ..models import Stub, Terms
from ..transport import is_allowed
from .base import Adapter, clean_title, main_text

_BASE = "https://www.cathay-cube.com.tw"
_PREFIX = "/content/cub-aem-cs/zh-tw"
_TAIPEI = timezone(timedelta(hours=8))

# 「小樹點(信用卡)」分類（/credit-card/bonus/）裡的常設說明頁：點數兌換、折抵、
# 累積辦法、FAQ、權益套餐介紹、兌換平台。這些不是期間活動。
_BONUS_INFO = re.compile(r"兌換|折抵|累積辦法|FAQ|權益套餐|通關平台", re.I)
# 「生活繳費」分類裡的常設服務介紹與公益頁：代扣繳、繳費平台、電子帳單、
# eTag 代扣繳、捐款。內文只有申辦方式，沒有活動期間或回饋條件。
_SERVICE = re.compile(r"/credit-card/payment/(?:all/donate|2024/billpayment-04(?:-2)?|"
                      r"2024/billpaymentq2|2025/eparking)\.html$")
# 卡片另有 campaignLink（官網靜態活動頁）且 AEM 頁幾乎是空殼時，條款改讀該頁。
_THIN = 200
# 靜態活動頁的頁尾從信用卡法定警語開始。
_FOOTER = re.compile(r"\n(?:CUBE App\n\|\n)?(?:聯絡我們|謹慎理財)[\s\S]*$")
# 元件內不屬於內文的欄位（頁面屬性、追蹤資料、按鈕連結、版面設定、圖片）。
_SKIP_KEYS = frozenset({
    "pageProperties", "dataLayer", "btnContent", "ctaContent", "ctaLink",
    "columnClassNames", "pagePath", "fileReference", "image", "alt", "imageAlt",
    "backgroundImageDesktop", "backgroundImageMobile", "backgroundImageDesktopAlt",
})
# 不輸出的元件：麵包屑、按鈕、「更多活動」推薦卡片。
_SKIP_TYPES = ("cub-breadcrumb", "cub-button", "cub-campaignpromotioncard")
_IMG = re.compile(r"<img\b[^>]*>", re.I)


class CathayAdapter(Adapter):
    id = "cathay"
    name = "國泰世華"
    domains = ["cathay-cube.com.tw", "cathaybk.com.tw"]
    entry_url = f"{_BASE}/cathaybk/personal/event/overview"
    list_url = f"{_BASE}/cathaybk/personal/event/overview/credit-card.model.list.json"
    portal_url = "https://www.cathaybk.com.tw/promotion/"
    portal_hint = "國泰世華信用卡活動登錄（卡友登錄專區）；標示 CUBE App 領券的活動改在 App 內領取"

    def discover(self) -> list[Stub]:
        payload = self.get_json(self.list_url)
        stubs: list[Stub] = []
        for campaign in payload.get("campaigns") or []:
            path = str(campaign.get("campaignPath") or "")
            props = campaign.get("campaignProps") or {}
            if not path.startswith(_PREFIX) or not isinstance(props, dict):
                continue
            names = f"{props.get('campaignTitle') or ''} {props.get('jcr:title') or ''}"
            if "/credit-card/bonus/" in path and _BONUS_INFO.search(names):
                continue
            if _SERVICE.search(path):
                continue
            url = public_url(path)
            stub = self.stub(
                key=url,
                title=card_title(props),
                url=url,
                period_start=taipei_date(props.get("startDate")),
                period_end=taipei_date(props.get("endDate")),
                summary=clean_title(str(props.get("campaignContent") or ""))[:160],
                category=_category(path),
            )
            if stub:
                stubs.append(stub)
        return stubs

    def terms(self, stub: Stub) -> Terms:
        model = self.get_json(f"{stub.url}.model.json")
        title = clean_title(str(model.get("title") or ""))
        main = _find_main(model)
        parts: list[str] = []
        if main is not None:
            _render(main, parts)
        text = main_text("<main>" + "\n".join(parts) + "</main>") if parts else ""
        link = _campaign_link(main)
        if len(text) < _THIN and link and is_allowed(link, self.domains):
            page = self.get(link).text
            fallback = _FOOTER.sub("", main_text(page, "main"))
            if len(fallback) > len(text):
                text = fallback
        return Terms(text=text, title=title)


def public_url(path: str) -> str:
    """``/content/cub-aem-cs/zh-tw/cathaybk/...x.html`` → 官方公開網址（無 .html）。"""
    tail = path[len(_PREFIX):] if path.startswith(_PREFIX) else path
    tail = tail.removesuffix(".html")
    return f"{_BASE}{tail}"


def card_title(props: dict[str, Any]) -> str:
    """卡片標題 ``campaignTitle``。官方用 \\r\\n 排版斷行：中文之間直接接起來，
    其餘斷行換成空白（交給 clean_title 收斂）。"""
    raw = str(props.get("campaignTitle") or "").strip() or str(props.get("jcr:title") or "")
    return re.sub(r"(?<=[^\x00-\x7f])\s*[\r\n]+\s*(?=[^\x00-\x7f])", "", raw)


def taipei_date(value: Any) -> date | None:
    """AEM 的 UTC ISO 字串或毫秒時間戳 → 台北日期。"""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, int | float):
            moment = datetime.fromtimestamp(value / 1000, tz=UTC)
        else:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(_TAIPEI).date()


def _category(path: str) -> str:
    match = re.search(r"/credit-card/([\w-]+)/", path)
    return match.group(1) if match else ""


def _find_main(model: dict[str, Any]) -> dict[str, Any] | None:
    """頁面主體 ``root/responsivegrid``。頁首、頁尾是它的兄弟節點（experience
    fragment），不在這棵樹裡。內容多半包在 ``cub_main``，但也有頁面直接把元件
    放在 responsivegrid 底下，所以從 responsivegrid 起算。"""
    try:
        node = model[":items"]["root"][":items"]["responsivegrid"]
    except (KeyError, TypeError):
        return None
    return node if isinstance(node, dict) else None


def _campaign_link(main: dict[str, Any] | None) -> str:
    """kvbanner 的頁面屬性 ``campaignLink``：卡片實際連到的官網靜態活動頁。"""
    stack: list[Any] = [main]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        props = node.get("pageProperties")
        if isinstance(props, dict) and props.get("campaignLink"):
            return str(props["campaignLink"]).strip()
        stack.extend((node.get(":items") or {}).values())
    return ""


def _render(node: Any, out: list[str]) -> None:
    """把 AEM 元件樹依顯示順序轉成 HTML 片段。"""
    if isinstance(node, list):
        for item in node:
            _render(item, out)
        return
    if not isinstance(node, dict):
        return
    kind = str(node.get(":type") or "")
    if kind.endswith(_SKIP_TYPES):
        return
    for key in ("cq:panelTitle", "title"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            inner = value if "<" in value else htmllib.escape(value)
            out.append(f"<h3>{inner}</h3>")
    for key in ("content", "text"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            out.append(_fragment(value))
    if isinstance(node.get("tables"), list):
        out.append(_table(node["tables"]))
    items = node.get(":items")
    for key, value in node.items():
        if key in _SKIP_KEYS or key.startswith(":") or key in ("tables", "content", "text"):
            continue
        # cub-tabs 把同一組分頁同時放在 tabs 與 :items，只走 :items 以免重複
        if key == "tabs" and isinstance(items, dict):
            continue
        if isinstance(value, dict | list):
            _render(value, out)
    if isinstance(items, dict):
        for key in node.get(":itemsOrder") or list(items):
            _render(items.get(key), out)


def _fragment(value: str) -> str:
    """元件的 rich text（HTML）或純文字（以 \\r\\n 分行）。"""
    value = _IMG.sub("", value)
    if "<" in value and ">" in value:
        return f"<div>{value}</div>"
    lines = [htmllib.escape(line) for line in value.splitlines() if line.strip()]
    return "".join(f"<p>{line}</p>" for line in lines)


def _table(columns: list[Any]) -> str:
    """cub-table：每欄一個 header＋content 清單 → 逐列輸出。"""
    headers = [str(col.get("header") or "") for col in columns if isinstance(col, dict)]
    cells = [
        [str(cell.get("content") or "") for cell in col.get("content") or []
         if isinstance(cell, dict)]
        for col in columns if isinstance(col, dict)
    ]
    rows = max((len(col) for col in cells), default=0)
    body = ["<tr>" + "".join(f"<th>{htmllib.escape(h)}</th>" for h in headers) + "</tr>"]
    for index in range(rows):
        row = [col[index] if index < len(col) else "" for col in cells]
        body.append("<tr>" + "".join(f"<td>{_IMG.sub('', cell)}</td>" for cell in row) + "</tr>")
    return "<table>" + "".join(body) + "</table>"


ADAPTER = CathayAdapter
