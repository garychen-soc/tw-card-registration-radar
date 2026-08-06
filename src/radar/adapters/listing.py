"""三種清單型態的通用讀取器。

前身每家銀行一個手寫函式，17 家共 3,716 行，差異只在清單 selector 與分頁機制。
這裡三個函式涵蓋實測到的所有情形，由 spec 驅動。

新增一家銀行時的判斷：官方有 JSON 清單端點 → ``json_api``；清單在 HTML 裡
→ ``html_list``；翻頁需要 POST 表單狀態 → ``form_paged``。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from ..htmltext import scope_html
from ..parse.datetimes import find_date_range
from ..parse.normalize import normalize_inline
from ..spec import ListingSpec, SourceSpec
from ..transport import Response, TransportError

# 清單請求一律不用條件式 GET。304 不帶 body，而清單沒有本機存檔可退回 ——
# 收到 304 就會把空字串當成清單內容，產出 0 筆。這個 bug 曾讓星展在第二次
# 執行時整個來源歸零（明細那條路徑已處理，當時漏了清單）。
LISTING_CONDITIONAL = False


class Fetch(Protocol):
    """runner 與測試共用的抓取介面。"""

    def get(
        self,
        url: str,
        *,
        data: dict[str, str] | None = ...,
        headers: dict[str, str] | None = ...,
        conditional: bool = ...,
    ) -> Response: ...


@dataclass
class ListingItem:
    """清單層級的一筆活動。明細尚未讀取。"""

    url: str
    title: str
    summary: str = ""
    start: date | None = None
    end: date | None = None
    category: str = ""
    registration_text: str = ""
    """官方在清單層直接公告的登錄期間原文。

    實測第一銀行的端點有 ``loginDate`` 欄位（「2026.1.1~2026.12.31(每月登錄，
    額滿即關閉登錄功能)」、「每月22日上午10點起(逐月登錄，額滿即關閉)」），
    其中 8 筆是明細頁的內文解析不到的。這是銀行**自己標記為登錄期間**的欄位，
    比在頁面散文裡找證據可靠得多。
    """
    featured: bool = False
    props: Any = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """清單指紋。內容未變且快取未過期時可跳過明細讀取。

        沿用前身的策略 —— 實測有效：沿用 935 筆、省下 798 次明細讀取。
        """
        payload = json.dumps(
            {
                "url": self.url,
                "title": self.title,
                "summary": self.summary,
                "start": self.start.isoformat() if self.start else None,
                "end": self.end.isoformat() if self.end else None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _walk(payload: Any) -> Iterator[dict[str, Any]]:
    """在任意巢狀結構裡找出所有 dict，供未指定 items_path 時使用。"""
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _walk(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _walk(value)


def _get(row: dict[str, Any], path: str) -> Any:
    """支援點號的巢狀取值。國泰世華的日期在 campaignProps 底下。"""
    if not path:
        return None
    value: Any = row
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _public_url(listing: ListingSpec, raw: str) -> str:
    """把清單給的路徑轉成公開網址。

    國泰世華的 ``campaignPath`` 是 AEM 內部路徑
    ``/content/cub-aem-cs/zh-tw/cathaybk/...``，直接用會連到內部路由；
    去掉前綴接上公開網域才是使用者實際看到的頁面。
    """
    path = raw.strip()
    if listing.url_strip_prefix and path.startswith(listing.url_strip_prefix):
        path = path[len(listing.url_strip_prefix) :]
    if listing.url_base:
        return urljoin(listing.url_base, path)
    return urljoin(listing.entry_url, path)


def _navigate(payload: Any, path: list[str]) -> Any:
    for key in path:
        if isinstance(payload, dict):
            payload = payload.get(key)
        else:
            return None
    return payload


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for candidate in (text[:10], text.replace("/", "-")[:10]):
        try:
            return date.fromisoformat(candidate)
        except ValueError:
            continue
    # 允許 `.` 分隔 —— 第一銀行的 activityDate 寫成 "2026.8.1-2026.9.6"
    match = re.search(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def read_json_api(
    spec: SourceSpec, fetcher: Fetch, warnings: list[str] | None = None
) -> list[ListingItem]:
    """JSON 端點的清單。

    三種形態都走這裡，差別只在 spec：

    單一 GET
        國泰世華、聯邦 —— 一次拿到全部。

    逐分類 POST（第一銀行）
        ``category_codes`` 列出分類代碼，``form_data`` 的值裡用 ``{category}``
        佔位。實測第一銀行的活動全部只存在於這個端點：整頁 372KB HTML 只有
        約 950 字純文字、0 個日期，活動是 JS 打
        ``queryActivityListByCategory2`` 拿回來的。改打端點後從 1 筆變成 75 筆，
        而且回應直接帶 ``activityDate``（活動期間）與 ``loginDate``（登錄期間）。

    逐頁
        ``form_fields.page`` 指定頁碼參數名，``max_pages`` 設上限。**當一頁沒有
        帶回任何新項目就停** —— 不能只靠分頁器的總頁數：實測第一銀行若把所有
        分類代碼併成一次請求，它會忽略 ``pageNumberSel``，每頁都回同一批 9 筆，
        照分頁器走會無限拿到重複資料。
    """
    listing = spec.listing
    items: list[ListingItem] = []
    seen: set[str] = set()
    page_field = listing.form_fields.get("page", "")

    for category in listing.category_codes or [""]:
        url = listing.data_url.replace("{category}", category)
        base_body = {
            key: value.replace("{category}", category)
            for key, value in listing.form_data.items()
        }
        pages = listing.max_pages if (page_field and base_body) else 1
        for page in range(1, pages + 1):
            body = dict(base_body) if base_body else None
            if body is not None and page_field:
                body[page_field] = str(page)
            response = fetcher.get(url, data=body, conditional=LISTING_CONDITIONAL)
            added = _collect_json_items(listing, response.json(), items, seen)
            if not added:
                break
    return items


def _collect_json_items(
    listing: ListingSpec,
    payload: Any,
    items: list[ListingItem],
    seen: set[str],
) -> int:
    """把一次回應的資料列併進 items，回傳新增筆數。"""
    rows = _navigate(payload, listing.items_path) if listing.items_path else payload

    fields = listing.fields
    url_key = fields.get("url", "url")
    candidates: list[dict[str, Any]] = []
    if isinstance(rows, list):
        candidates = [row for row in rows if isinstance(row, dict)]
    elif rows is not None:
        # 端點回傳巢狀結構（例如聯邦按 catalog 分組）時，撈出所有帶 url 欄位的 dict
        root_key = url_key.split(".")[0]
        candidates = [row for row in _walk(rows) if root_key in row]

    allowed_categories = set(listing.categories)
    added = 0
    for row in candidates:
        raw_url = _get(row, url_key)
        if not isinstance(raw_url, str) or not raw_url.strip():
            continue
        url = _public_url(listing, raw_url)
        category = normalize_inline(str(_get(row, fields.get("category", "")) or ""))
        if allowed_categories and category and category not in allowed_categories:
            continue
        if url in seen:
            continue
        seen.add(url)
        added += 1
        featured_key = fields.get("featured_rank", "")
        featured_value = _get(row, featured_key) if featured_key else None
        items.append(
            ListingItem(
                url=url,
                title=normalize_inline(str(_get(row, fields.get("title", "title")) or "")),
                summary=normalize_inline(str(_get(row, fields.get("summary", "")) or "")),
                start=_as_date(_get(row, fields.get("start", ""))),
                end=_as_date(_get(row, fields.get("end", ""))),
                category=category,
                registration_text=normalize_inline(
                    str(_get(row, fields.get("registration", "")) or "")
                ),
                featured=bool(featured_value) and str(featured_value) not in {"0", "False"},
                props=_get(row, fields.get("props", "")) if fields.get("props") else None,
                raw=row,
            )
        )
    return added


def _items_from_html(listing: ListingSpec, html: str, base_url: str) -> list[ListingItem]:
    blocks: list[tuple[str, str]]
    if listing.item_selector:
        tree = HTMLParser(html)
        # 用換行分隔，這樣區塊文字的第一行就是卡片標題
        blocks = [
            (node.html or "", node.text(separator="\n") or "")
            for node in tree.css(listing.item_selector)
        ]
    else:
        blocks = [(html, "")]

    pattern = re.compile(listing.link_pattern, re.I) if listing.link_pattern else None
    items: list[ListingItem] = []
    seen: set[str] = set()
    for block_html, block_text in blocks:
        if pattern is None:
            continue
        for match in pattern.finditer(block_html):
            href = match.group(1) if match.groups() else match.group(0)
            # 第二個擷取群組（有的話）當標題。實測台新把活動名稱放在錨點的
            # title 屬性裡，錨點文字只是「瞭解詳情」。
            link_title = match.group(2) if len(match.groups()) >= 2 else ""
            url = urljoin(base_url, href.strip())
            if url in seen:
                continue
            seen.add(url)
            # 卡片上常直接印期間（富邦是 2026.01.01~2026.12.31）。
            # 抓到就帶進 ListingItem，明細頁抓不到時可以補位。
            card_start, card_end, _, _ = find_date_range(
                block_text, default_year=date.today().year, limit_chars=200
            )
            items.append(
                ListingItem(
                    url=url,
                    title=normalize_inline(
                        link_title
                        or _pick(block_html, listing.title_selector)
                        or _block_title(block_html, block_text)
                    ),
                    summary=normalize_inline(_pick(block_html, listing.summary_selector)),
                    start=card_start,
                    end=card_end,
                )
            )
    return items


def _pick(block_html: str, selector: str) -> str:
    """用 CSS selector 從區塊中取文字。找不到就回空字串。"""
    if not selector:
        return ""
    node = HTMLParser(block_html).css_first(selector)
    return (node.text() or "").strip() if node else ""


def _block_title(block_html: str, block_text: str) -> str:
    for tag in ("h6", "h5", "h4", "h3", "h2"):
        match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block_html, re.S | re.I)
        if match:
            return re.sub(r"<[^>]+>", "", match.group(1))
    # 沒有標題標籤時取區塊文字的第一行 —— 卡片的第一行就是活動名稱
    for line in block_text.splitlines():
        candidate = line.strip()
        if len(candidate) >= 2:
            return candidate[:120]
    return block_text[:120]


# Wicket 的分頁連結長成 `?<pageId>-<renderCount>.-fmList-...-navigation-2-pageLink`，
# 直接 GET 會 500。必須把 `<版本>.-` 改寫成 `<版本>.0-`（補上 behavior id），並附 Wicket 標頭。
_WICKET_PAGE_LINK = re.compile(
    r'<a[^>]+href="([^"]*-nav-navigation-\d+-pageLink)"[^>]*>(.*?)</a>', re.I | re.S
)
# 「跳到最後一頁」。它是唯一能一跳抵達尾端的連結，因此也是問伺服器「究竟有幾頁」
# 最省請求的方式 —— 落地後看停用的頁碼就知道總頁數。
_WICKET_TAIL_LINK = re.compile(r'<a[^>]+href="([^"]*-nav-last)"', re.I)
_WICKET_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
# 版本號寫成 `(\d+-\d+)` 而不是 `(\d+-1)`：renderCount 不保證永遠是 1，寫死 1 時
# 只要伺服器換了 renderCount，改寫就整批失效、每一頁都回 500 而且看起來像「沒有分頁」。
_WICKET_VERSION = re.compile(r"(\d+-\d+)\.-")
_WICKET_REDIRECT = re.compile(r"<redirect>\s*<!\[CDATA\[(.*?)\]\]>\s*</redirect>", re.I | re.S)
_WICKET_HEADERS = {
    "Accept": "application/xml, text/xml, */*; q=0.01",
    "Cache-Control": "no-cache",
    "Wicket-Ajax": "true",
    "X-Requested-With": "XMLHttpRequest",
}


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def _wicket_page_links(html: str, base_url: str) -> dict[int, str]:
    """回傳 {可見頁碼: 連結}。只收標籤是數字的 —— 上一頁／下一頁的箭頭不是頁碼。"""
    links: dict[int, str] = {}
    for match in _WICKET_PAGE_LINK.finditer(html):
        label = _strip_tags(match.group(2))
        if not label.isdigit():
            continue
        href = _WICKET_VERSION.sub(r"\1.0-", match.group(1))
        links[int(label)] = urljoin(base_url, href)
    return links


def _wicket_tail_link(html: str, base_url: str) -> str | None:
    match = _WICKET_TAIL_LINK.search(html)
    if not match:
        return None
    return urljoin(base_url, _WICKET_VERSION.sub(r"\1.0-", match.group(1)))


def _wicket_current_page(html: str, links: dict[int, str]) -> int | None:
    """目前實際所在的頁碼 —— Wicket 把當前頁的頁碼連結停用（沒有 href）。

    非得看實際落地頁碼不可：伺服器把頁面狀態判為過期時會悄悄把你送回第 1 頁，
    若拿「我要求的頁碼」當落地頁碼，就會把第 1 頁的九筆當成第 7 頁收下，
    然後以為已經收完。這正是舊版每次跑出不同筆數的其中一個環節。

    用可見頁碼區間當守門員，避免頁面別處的無連結數字（頁尾電話、樓層）被誤認。
    """
    low = min(links) - 1 if links else 1
    high = max(links) + 1 if links else 1
    for match in _WICKET_ANCHOR.finditer(html):
        attrs, inner = match.group(1), match.group(2)
        if "href=" in attrs.lower():
            continue
        label = _strip_tags(inner)
        if not label.isdigit():
            continue
        page = int(label)
        if page not in links and low <= page <= high:
            return page
    return None


def _wicket_window(page: int, total: int, view: int) -> range:
    """站在 ``page`` 時頁碼列會顯示哪些頁碼。

    Wicket 的頁碼列寬度固定（實測 view=10）、位置隨當前頁移動，兩端夾住：
    第 1 頁看到 1–10、第 10 頁看到 6–15、最後一頁（24）看到 15–24，也就是
    ``[page-4, page+5]`` 夾在 ``[1, total]`` 內。知道這條規則才能算出
    「該點哪一頁，下一跳就能直接點到目標頁」。
    """
    offset = max(0, view // 2 - 1)
    start = min(max(page - offset, 1), max(1, total - view + 1))
    return range(start, min(total, start + view - 1) + 1)


def _wicket_next_hop(
    links: dict[int, str], remaining: set[int], current: int, total: int, view: int
) -> int:
    """挑下一個要點的頁碼。

    能直接點到還沒收的頁就直接點；否則點一個「點下去之後頁碼列會涵蓋目標頁」的
    頁碼。有了「跳到最後一頁」當墊腳石，實測 24 頁的清單裡任何一頁都在兩跳內可達
    （2–10 從第 1 頁一跳、24 用 last 一跳、11–15 經第 10 頁、15–23 經第 24 頁）。
    """
    direct = remaining & links.keys()
    if direct:
        return min(direct, key=lambda page: (abs(page - current), page))
    want = min(remaining, key=lambda page: (abs(page - current), page))
    covering = [page for page in links if want in _wicket_window(page, total, view)]
    return min(covering or list(links), key=lambda page: (abs(page - want), page))


def _unwrap_cdata(body: str) -> str:
    """Wicket 的 Ajax 回應把更新後的 HTML 片段包在 CDATA 裡。
    去掉包裝才能用同一套 selector 與正則解析。"""
    return body.replace("<![CDATA[", "").replace("]]>", "")


# 一次完整走訪（24 頁）實測用掉 62／105／121／166 次請求。預算給到每頁 12 次
# 再加 60，是把「連續失手」的長尾也蓋進去 —— 觸到上限代表真的走不完，會留警示。
WICKET_REQUEST_BUDGET_PER_PAGE = 12
WICKET_REQUEST_BUDGET_BASE = 60


def _read_wicket_pages(
    listing: ListingSpec,
    fetcher: Fetch,
    first_html: str,
    first_url: str,
    warnings: list[str],
) -> list[ListingItem]:
    """跟隨 Apache Wicket 的 Ajax 分頁，並且對帳到官方宣告的總筆數。

    兩個非顯而易見的必要條件（都是實測出來的）：

    * 分頁連結要把 ``<版本>.-`` 改寫成 ``<版本>.0-``，否則回 500。
    * 要帶 ``Accept``、``Referer``、``Wicket-Ajax`` 等標頭，否則回 403。

    **為什麼不能照著頁碼一路往下走。** 富邦的清單頁在 Envoy 後面有多個節點
    （失敗回應帶 ``x-envoy-upstream-service-time``），而 Wicket 的頁面狀態存在
    處理該次 render 的那一個節點上，且沒有 session 黏著。於是每一次 Ajax 翻頁
    都是獨立的擲硬幣：實測固定條件下單跳 40 次只成功 20 次，而且**加延遲沒有用**
    （延遲 0／0.5／1／2／4 秒各試 8 次，成功 6／3／4／5／2）。沒中的那一半，
    伺服器回一個 redirect 把你送回第 1 頁的新頁面實例。

    舊版把這件事當成「偶發失步」，用「連續 N 輪沒有新資料就收手」收尾，於是
    「走到第幾頁」變成一個隨機變數 —— 同一份程式碼連跑三次得到 108／117／135 筆，
    更早還出現過 90／99／153／198。取三次的最大值只是把平均值拉高，變異還在，
    逐來源覆蓋率退步防護照樣誤擋。

    **改成以「頁碼集合」為目標，逐頁對帳。** 總頁數由官方宣告的總筆數推算
    （``total_pattern``，富邦頁面印「共215筆相關結果」，9 筆／頁 → 24 頁），
    每一頁都留在 ``remaining`` 裡直到真的收到。失手就跟隨 redirect 回到第 1 頁
    重新起跳，不放棄任何一頁。實測連跑四次都拿到 215 筆、0 頁未走訪。

    走不完時（預算用盡）在 ``warnings`` 留下訊息，由 runner 轉成
    ``listing_page_unreadable`` 警示並讓來源健康度變成 partial —— 不可以看起來
    像全部收錄成功。
    """
    items = _items_from_html(listing, first_html, first_url)
    seen = {item.url for item in items}
    page_size = len(items)
    announced = _wicket_announced_total(listing, first_html)
    total = _page_count(listing, first_html, page_size)
    view = max(_wicket_page_links(first_html, first_url), default=1)
    total = max(total, view)

    remaining = set(range(2, total + 1))
    html, url, current = first_html, first_url, 1
    used = 0
    budget = WICKET_REQUEST_BUDGET_PER_PAGE * total + WICKET_REQUEST_BUDGET_BASE

    while remaining and used < budget:
        links = _wicket_page_links(html, url)
        tail = _wicket_tail_link(html, url)
        if tail is not None and total not in links:
            # 把「跳到最後一頁」當成通往 total 的連結。它同時是總頁數的權威來源：
            # 落地後讀到的頁碼若超出推算值，下面會把 total 往上補。
            links[total] = tail
        if not links:
            break
        target = _wicket_next_hop(links, remaining, current, total, view)
        used += 1
        try:
            response = fetcher.get(
                _cache_busted(links[target]),
                headers={
                    **_WICKET_HEADERS,
                    "Referer": url,
                    "Wicket-Ajax-BaseURL": listing.pagination_base or "",
                },
                conditional=False,
            )
        except TransportError:
            break  # 少幾頁的資料比整個來源歸零好；下面會留下警示
        redirect = _WICKET_REDIRECT.search(response.text)
        if redirect is None:
            html, url = _unwrap_cdata(response.text), response.final_url
        else:
            used += 1
            try:
                followed = fetcher.get(
                    urljoin(response.final_url, redirect.group(1).strip()), conditional=False
                )
            except TransportError:
                break
            html, url = followed.text, followed.final_url

        links = _wicket_page_links(html, url)
        current = _wicket_current_page(html, links) or (1 if redirect else target)
        for item in _items_from_html(listing, html, url):
            if item.url not in seen:
                seen.add(item.url)
                items.append(item)
        # 官方新增活動、或宣告的總筆數過期時，實際頁數會比推算的多。看到更大的
        # 頁碼就把目標往上補 —— 否則會停在推算值，看起來像「收完了」。
        # 先補再把落地頁移出待辦，否則剛收到的那一頁會被補回待辦、白跑一趟。
        highest = min(max(links, default=0), listing.max_pages)
        if highest > total:
            remaining.update(range(total + 1, highest + 1))
            total = highest
            budget = WICKET_REQUEST_BUDGET_PER_PAGE * total + WICKET_REQUEST_BUDGET_BASE
        remaining.discard(current)

    if remaining:
        pages = ", ".join(str(page) for page in sorted(remaining)[:12])
        warnings.append(
            f"清單分頁未走訪完整：第 {pages} 頁在 {used} 次請求內都拿不到"
            "（Wicket 頁面狀態不在同一個後端節點上，每次翻頁約一半機會失手）"
        )
    if announced and len(items) < announced:
        warnings.append(f"官方清單宣告共 {announced} 筆，實際只取得 {len(items)} 筆")
    if total >= listing.max_pages:
        warnings.append(f"清單頁數已達 max_pages={listing.max_pages} 上限，可能還有後續分頁")
    return items


def _wicket_announced_total(listing: ListingSpec, html: str) -> int:
    """官方自己印在頁面上的總筆數。唯一能證明「收完了」的外部依據。"""
    if not listing.total_pattern:
        return 0
    match = re.search(listing.total_pattern, html, re.I)
    return int(match.group(1)) if match else 0


def _cache_busted(url: str) -> str:
    return f"{url}{'&' if '?' in url else '?'}_={int(time.time() * 1000)}"


def read_html_list(
    spec: SourceSpec, fetcher: Fetch, warnings: list[str] | None = None
) -> list[ListingItem]:
    listing = spec.listing
    notes = warnings if warnings is not None else []
    # 分類清單：逐一請求每個分類的網址（台新分 A–I 九類）
    urls = (
        [listing.url_template.format(category=code) for code in listing.category_codes]
        if listing.url_template and listing.category_codes
        else [listing.entry_url]
    )
    items: list[ListingItem] = []
    seen: set[str] = set()
    for index, url in enumerate(urls):
        try:
            response = fetcher.get(url, conditional=LISTING_CONDITIONAL)
        except TransportError:
            if index == 0 and len(urls) == 1:
                raise
            continue  # 單一分類失敗不讓整個來源歸零
        if listing.pagination_kind == "wicket_ajax":
            found = _read_wicket_pages(listing, fetcher, response.text, response.final_url, notes)
        else:
            # 限縮到指定面板再找連結。華南一頁裡有存款、貸款、保險等多個分頁，
            # 不限縮會把其他業務的連結一起收進來。
            scoped = scope_html(
                response.text,
                selector=listing.scope_selector,
                tab_label=listing.scope_tab_label,
            )
            found = _items_from_html(listing, scoped, response.final_url)
        for item in found:
            if item.url not in seen:
                seen.add(item.url)
                items.append(item)
    return items


_HIDDEN_INPUT = r'name="{name}"[^>]*value="(\d+)"'


def read_form_paged(
    spec: SourceSpec, fetcher: Fetch, warnings: list[str] | None = None
) -> list[ListingItem]:
    """需要表單狀態才能翻頁的清單。

    涵蓋兩種實測到的形態：

    元大式
        GET ``entry_url`` 拿第一頁，總頁數藏在隱藏欄位，後續頁 POST 回同一個
        網址並帶回那些狀態值。
    玉山式
        清單來自另一個端點（``post_url``），**第一頁就要 POST** 並附固定參數
        （``form_data``），回傳的是 HTML 片段；總筆數藏在 ``id="total"`` 這類
        欄位裡，總頁數要用「總筆數 ÷ 首頁筆數」推算。

    單一分頁失敗不中止 —— 少一頁的資料比整個來源歸零好。
    """
    listing = spec.listing
    page_field = listing.form_fields.get("page")
    post_url = listing.post_url or listing.entry_url

    def payload(page: int, extra: dict[str, str] | None = None) -> dict[str, str]:
        data = dict(listing.form_data)
        if page_field:
            data[page_field] = str(page)
        data.update(extra or {})
        return data

    if listing.post_url:
        first = fetcher.get(post_url, data=payload(1), conditional=False)
    else:
        first = fetcher.get(listing.entry_url, conditional=LISTING_CONDITIONAL)
    items = _items_from_html(listing, first.text, first.final_url)
    if not page_field:
        return items

    total_pages = _page_count(listing, first.text, len(items))
    total_field = listing.form_fields.get("total_pages", "")
    items_field = listing.form_fields.get("total_items", "")
    state: dict[str, str] = {}
    if total_field:
        state[total_field] = str(total_pages)
    if items_field:
        found = re.search(
            _HIDDEN_INPUT.format(name=re.escape(items_field)), first.text, re.I
        )
        if found:
            state[items_field] = found.group(1)

    seen = {item.url for item in items}
    for page in range(2, total_pages + 1):
        response = fetcher.get(post_url, data=payload(page, state), conditional=False)
        for item in _items_from_html(listing, response.text, response.final_url):
            if item.url not in seen:
                seen.add(item.url)
                items.append(item)
    return items


def _page_count(listing: ListingSpec, html: str, items_on_first_page: int) -> int:
    """總頁數。優先用隱藏欄位的總頁數，否則用總筆數推算。"""
    total_field = listing.form_fields.get("total_pages", "")
    if total_field and (
        match := re.search(_HIDDEN_INPUT.format(name=re.escape(total_field)), html, re.I)
    ):
        return max(1, min(int(match.group(1)), listing.max_pages))
    if listing.total_pattern and items_on_first_page:
        match = re.search(listing.total_pattern, html, re.I)
        if match:
            total_items = int(match.group(1))
            pages = -(-total_items // items_on_first_page)  # ceil
            return max(1, min(pages, listing.max_pages))
    return 1


def read_single_page(
    spec: SourceSpec, fetcher: Fetch, warnings: list[str] | None = None
) -> list[ListingItem]:
    """入口頁本身就是唯一的活動頁。

    中信的 LINE Pay 優惠頁一頁 14 個活動，頁上的連結全指向 ``line.me``
    而非活動明細 —— 沒有逐活動的官方網址可抓。這種來源只有一個「活動頁」，
    再由 ``detail.cardinality = "many"`` 切成多個子活動。

    不在這裡抓取 —— runner 讀明細時自然會抓，失敗也會由它記錄成來源健康。
    """
    return [ListingItem(url=spec.listing.entry_url, title=spec.bank_name)]


# 第三個參數是「清單層級的問題」的出口。清單讀取只回 items 時，「有幾頁根本沒讀到」
# 這件事沒有地方可以說，於是少收一半的資料看起來和全部收完一模一樣。
Reader = Callable[[SourceSpec, Fetch, list[str] | None], list[ListingItem]]

READERS: dict[str, Reader] = {
    "json_api": read_json_api,
    "html_list": read_html_list,
    "form_paged": read_form_paged,
    "single_page": read_single_page,
}


def read_listing(
    spec: SourceSpec, fetcher: Fetch, warnings: list[str] | None = None
) -> list[ListingItem]:
    return READERS[spec.listing.kind](spec, fetcher, warnings)
