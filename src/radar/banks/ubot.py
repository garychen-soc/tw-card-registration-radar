"""聯邦銀行：信用卡優惠頁的 Rewards API。

``/CardActivity`` 的七個分類（卡片優惠、百貨零售、旅遊優惠、交通汽修、網購數位、
生活繳費、購物娛樂）都由 ``/eCardServiceAPI/api/Rewards`` 一次回傳；「強打優惠」
只是 ``hotOrder`` 標記，不是另一批活動。標題取 ``title``，期間取 ``sdt``／``edt``。

同一份明細常被多張卡片指到（例如 Uber Eats 與 foodpanda 都連到
``202607delivery/index.htm``）：照樣各回一個 Stub，key 加上標題區分，條款相同
由 pipeline 依雜湊合併。

明細是各自設計的活動小站（``activity.ubot.com.tw``），沒有共同的內容容器，而且
子活動常放在分頁或彈窗（注意事項）裡 —— 所以取整頁主文（通用濾網已移除 nav／
header／footer），不挑單一分頁；再刪掉每頁重複的「您即將離開聯邦銀行網站」
確認框與「關閉」按鈕字。
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

from ..models import Stub, Terms
from .base import Adapter, canonical, clean_title, main_text, node_text, tree

_API = "https://card.ubot.com.tw/eCardServiceAPI/api/Rewards"
# 服務說明與公益募款，不是活動（硬規則 2）。
_NOT_ACTIVITY = re.compile(r"即時簡訊|世界展望會|公益|捐款")
# 外連確認框有好幾種措辭：「提醒您！」可有可無、說明可能折成兩行。
_LEAVE = re.compile(
    r"(?m)^(?:提醒您！?\n)?您即將離開[^\n]*\n(?:[^\n]*請點選[^\n]*\n)?我同意\n我不同意(?:\n|$)"
)
_CLOSE = re.compile(r"(?m)^(?:關閉|提醒您！?)(?:\n|$)")


class UbotAdapter(Adapter):
    id = "ubot"
    name = "聯邦銀行"
    # newnewbank.com.tw 是聯邦的數位帳戶品牌 New New Bank（憑證 O=UNION BANK OF TAIWAN）。
    domains = ["ubot.com.tw", "newnewbank.com.tw"]
    entry_url = "https://card.ubot.com.tw/CardActivity"
    portal_url = "https://card.ubot.com.tw/eCard/activity_login/register_activity.aspx"
    portal_hint = "聯邦銀行信用卡活動登錄（限時登錄活動專區），登入後在活動清單找到對應活動"

    def discover(self) -> list[Stub]:
        payload = self.get_json(_API)
        if not isinstance(payload, dict) or payload.get("rtnCode") != "0000":
            raise ValueError(f"聯邦 Rewards API 回傳異常：{str(payload)[:120]}")
        rows = [row for row in payload.get("info") or [] if isinstance(row, dict)]
        shared = Counter(canonical(str(row.get("url") or "")) for row in rows)
        stubs: list[Stub] = []
        seen: set[str] = set()
        for row in rows:
            raw = str(row.get("url") or "").strip()
            title = clean_title(str(row.get("title") or ""))
            if not raw or _NOT_ACTIVITY.search(title):
                continue
            url = canonical(raw)
            key = f"{url}#{title}" if shared[url] > 1 else url
            if key in seen:
                continue
            seen.add(key)
            stub = self.stub(
                key=key,
                title=title,
                url=url,
                period_start=_day(row.get("sdt")),
                period_end=_day(row.get("edt")),
                summary=clean_title(str(row.get("desc") or "")),
                category=clean_title(str(row.get("catalog") or "")),
            )
            if stub:
                stubs.append(stub)
        return stubs

    def terms(self, stub: Stub) -> Terms:
        page = self.get(stub.url).text
        doc = tree(page)
        body = doc.body.html if doc.body is not None else page
        # 包一層 <main>：否則 main_text 會挑第一個夠長的 <article>／.content，
        # 分頁式小站就只剩其中一個分頁。
        text = _CLOSE.sub("", _LEAVE.sub("", main_text(f"<main>{body}</main>"))).strip()
        heading = doc.css_first("h1") or doc.css_first("title")
        return Terms(text=text, title=node_text(heading))


def _day(value: object) -> date | None:
    """``2026-07-01 00:00:00`` → date。"""
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


ADAPTER = UbotAdapter
