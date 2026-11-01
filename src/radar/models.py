"""v2 資料契約。

三層，各自只有一個來源：

``Stub``
    銀行官方列表或 API 上的一筆活動。標題只能來自結構化欄位（卡片名稱、API
    的 title 欄位），不得取自內文第一行 —— v1 的「關於我們」「優惠內容」
    就是這樣來的。
``Terms``
    明細頁主要內容區的純文字，是登錄規則唯一的解析輸入。
``Activity``
    發布到網站的一筆活動。一個官方頁面＝一筆活動；同頁多個子活動放在
    ``subs``，每個子活動只帶有原文引句佐證的登錄時點。

所有時間都是台北時區的 aware datetime。資料檔不放「今天」的函數
（是否進行中、是否即將截止），由前端計算。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Stub(Strict):
    """列表／API 上的一筆官方活動。"""

    bank_id: str
    key: str = Field(description="該銀行內穩定且唯一的鍵：API 的活動 id，或正規化後的明細 URL")
    title: str = Field(description="官方列表卡片名稱或 API 標題欄位，原樣保留")
    url: str = Field(description="官方明細頁，https 且在該銀行網域白名單內")
    period_start: date | None = None
    period_end: date | None = None
    summary: str = Field(default="", description="列表卡片上的一句話摘要，沒有就留空")
    category: str = ""
    detail_text: str | None = Field(
        default=None,
        description="API 已附條款全文時直接放這裡，pipeline 就不再抓明細頁",
    )


class Terms(Strict):
    """明細頁的條款文字。"""

    text: str = Field(description="主要內容區純文字，已排除導覽列、頁尾、相關活動連結")
    title: str = Field(default="", description="明細頁自己的標題，只用於與 Stub.title 交叉檢查")
    period_start: date | None = None
    period_end: date | None = None


SlotKind = Literal["opens_at", "range", "deadline"]
SlotOrigin = Literal["explicit", "rule", "enumerated"]


class Evidence(Strict):
    quote: str = Field(description="逐字取自 Terms.text 的原文")
    url: str


class Slot(Strict):
    """一個具體的登錄時點。"""

    start: datetime | None = Field(description="開放登錄時刻；deadline 型為 None")
    end: datetime | None = Field(description="截止時刻；None 表示官方未公告或額滿為止")
    kind: SlotKind
    origin: SlotOrigin = Field(
        description="explicit=條款明寫日期；rule=依每月規則展開；enumerated=條款列舉的日期"
    )
    precision: Literal["minute", "day"] = Field(
        default="minute", description="day＝原文只寫日期沒寫時刻，前端只顯示日期"
    )
    holiday_shifted: bool = False
    note: str = ""
    evidence: Evidence


Reregister = Literal["each_period", "once", "unknown"]
Order = Literal["register_before_spend", "retroactive_ok", "unknown"]


class SubActivity(Strict):
    """頁面上的一個子活動。整頁只有一個活動時 ``name`` 為空字串。"""

    name: str = ""
    slots: list[Slot] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list, description="登錄規則原文，供 UI 顯示")
    reregister: Reregister = "unknown"
    reregister_quote: str = ""
    order: Order = "unknown"
    order_quote: str = ""
    quota: int | None = None
    quota_quote: str = ""
    review: list[str] = Field(default_factory=list)


Registration = Literal["required", "not_required", "unknown"]


class Activity(Strict):
    id: str
    bank_id: str
    bank_name: str
    title: str
    aliases: list[str] = Field(default_factory=list, description="同一份條款在列表上的其他名稱")
    url: str
    period_start: date | None = None
    period_end: date | None = None
    summary: str = ""
    category: str = ""
    registration: Registration = "unknown"
    registration_quote: str = ""
    subs: list[SubActivity] = Field(default_factory=list)
    portal_url: str = ""
    portal_hint: str = ""
    review: list[str] = Field(default_factory=list)
    content_hash: str = ""
    observed_at: datetime
    stale: bool = Field(default=False, description="本輪來源失敗，沿用上一輪資料")


SourceStatus = Literal["ok", "partial", "failed", "blocked"]


class SourceHealth(Strict):
    bank_id: str
    bank_name: str
    status: SourceStatus
    entry_url: str
    stubs: int = 0
    activities: int = 0
    detail_failures: int = 0
    message: str = ""
    carried: int = 0
