"""資料契約。

兩條不可妥協的規則：

1. 只放「事實」，不放「推導」。凡是「今天」的函數（lifecycle、是否高回饋、
   是否即將結束、今日待登錄）一律不進 artifact，由讀取端計算。artifact 放三天
   也不會顯示錯的狀態。
2. 每個有解析風險的欄位都帶 confidence 與 evidence（原文節錄）。抓不到就留 None，
   絕不用固定值填補 —— 顯示「17:00 起（截止時間未確認）」比顯示一個錯的截止時間誠實。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Confidence = float

WindowKind = Literal[
    "range",       # 有明確起訖：2026/8/7 17:00 ~ 2026/8/31 23:59
    "opens_at",    # 只有開放時點，截止未知：8/17 10:00 開放登錄
    "deadline",    # 只有截止：登錄期限 8/31 23:59 止
    "recurring",   # 由循環規則展開：每月 1 日 10:00 開放登錄
]

ContractKind = Literal[
    "register_before_spend",      # 先登錄後消費，登錄前的消費不計入
    "retroactive_ok",             # 可事後補登錄
    "registration_closes_early",  # 登錄先截止，錯過就整檔白刷
    "per_period_reregister",      # 每期／每月需重新登錄
    "unknown",
]

ThresholdKind = Literal["per_transaction", "cumulative", "none", "unknown"]


class Evidence(BaseModel):
    """支撐某個解析結果的原文節錄。UI 可展開讓使用者自行核對。"""

    text: str = Field(max_length=400)
    source_url: str = ""


class Period(BaseModel):
    """活動（可消費）期間。end 為 None 代表官方未公告結束日，不推定。"""

    start: date | None = None
    end: date | None = None
    confidence: Confidence = 0.0
    evidence: Evidence | None = None


class RegistrationWindow(BaseModel):
    """單一登錄時點或區間。

    end 允許 None —— 這是與舊實作最重要的差異。舊版在解析不到結束時間時
    一律填 start + 30 分鐘（後改 15 分鐘），使 90% 的視窗帶著編造的截止時間，
    而真正的截止日被丟棄。
    """

    kind: WindowKind
    start: datetime | None = None
    end: datetime | None = None
    confidence: Confidence = 0.0
    evidence: Evidence | None = None

    @model_validator(mode="after")
    def _coherent(self) -> RegistrationWindow:
        if self.start is None and self.end is None:
            raise ValueError("registration window needs at least one of start/end")
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("registration window end precedes start")
        return self

    @property
    def anchor(self) -> datetime:
        """行事曆與待辦清單的時間定位點。

        deadline 型的視窗只知道截止時間（「登錄期限 8/31 23:59 止」），
        此時定位點是 end；其餘型態是 start。
        """
        anchor = self.start or self.end
        assert anchor is not None  # 由 _coherent 保證
        return anchor

    @property
    def end_known(self) -> bool:
        return self.end is not None


class Recurrence(BaseModel):
    """登錄是否需要重複執行。實測全站 30.6% 的需登錄活動屬於此類，
    舊 schema 完全沒有表達，使用者以為登錄一次就結束。"""

    kind: Literal["once", "monthly", "per_campaign_period"] = "once"
    note: str = ""
    confidence: Confidence = 0.0


class Portal(BaseModel):
    """登錄入口。

    kind 是必要的誠實標記：實測 242 筆需登錄活動只有 20 個不同 URL，
    97.9% 指向銀行的統一登錄頁。假裝是活動專屬連結，使用者到站後還要自己找。
    """

    url: str = ""
    kind: Literal["activity_specific", "bank_portal", "unknown"] = "unknown"
    hint: str = ""


class Quota(BaseModel):
    """名額限制。實測 62.8% 的需登錄活動有限量或額滿字樣，決定登錄要不要搶。"""

    limited: bool = False
    seats: int | None = None
    text: str = ""
    confidence: Confidence = 0.0


class ThresholdTier(BaseModel):
    """階梯式消費門檻。

    依聯邦銀行明細頁實測修正：門檻不是單一數字，而是階梯，每階有自己的
    回饋、分期加碼與名額。舊 schema 只有單一 max_reward_amount_twd，
    使用者看不出「我要刷多少才拿得到哪一階」。
    """

    spend_twd: int
    reward_twd: int | None = None
    reward_percent: float | None = None
    reward_if_installment: int | None = None
    installment_periods: int | None = None
    quota_seats: int | None = None


class Eligibility(BaseModel):
    new_customer_only: bool = False
    first_swipe_only: bool = False
    primary_card_only: bool = False
    cards: list[str] = Field(default_factory=list)
    confidence: Confidence = 0.0
    evidence: Evidence | None = None


class Installment(BaseModel):
    required: bool = False
    periods: list[int] = Field(default_factory=list)
    rate: str = ""
    confidence: Confidence = 0.0


class Conditions(BaseModel):
    eligibility: Eligibility = Field(default_factory=Eligibility)
    threshold_kind: ThresholdKind = "unknown"
    threshold_tiers: list[ThresholdTier] = Field(default_factory=list)
    installment: Installment = Field(default_factory=Installment)
    quota: Quota = Field(default_factory=Quota)
    reward_cap_twd: int | None = None
    reward_cap_percent: float | None = None


class TimingContract(BaseModel):
    """登錄與消費的時序關係 —— 使用者「會不會白刷」的答案。

    舊 schema 只有 registration_required: bool，把這件事壓成一個布林值。
    實測 242 筆需登錄活動中有 90.5% 無法回答「登錄期間 vs 消費期間」。
    """

    kind: ContractKind = "unknown"
    spend_counts_from: datetime | None = None
    last_chance_to_register: datetime | None = None
    spend_days_left_after_registering: int | None = None
    confidence: Confidence = 0.0
    consistency: list[str] = Field(default_factory=list)


class Registration(BaseModel):
    required: bool = False
    windows: list[RegistrationWindow] = Field(default_factory=list)
    recurrence: Recurrence = Field(default_factory=Recurrence)
    portal: Portal = Field(default_factory=Portal)
    timing_contract: TimingContract = Field(default_factory=TimingContract)
    raw_text: str = ""


class Offer(BaseModel):
    """子活動 / 方案。

    這一層是修正舊實作「活動粒度錯誤」的關鍵。舊版以明細頁 URL 雜湊當活動 ID，
    一頁一筆；但台灣銀行的活動頁普遍單頁多活動（實測：玉山 momo 頁含活動一/二/三，
    中信一頁 14 個活動）。合併後各子活動的登錄時點互相污染，才會出現
    「活動至 08-15，卻有 08-17 的登錄時點」。
    """

    id: str
    title: str
    period: Period = Field(default_factory=Period)
    registration: Registration = Field(default_factory=Registration)
    conditions: Conditions = Field(default_factory=Conditions)
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)


class Campaign(BaseModel):
    """活動頁層級。一個 source_url 對應一個 Campaign、可含多個 Offer。"""

    id: str
    bank_id: str
    bank_name: str
    title: str
    source_url: str
    observed_at: datetime
    offers: list[Offer] = Field(default_factory=list)
    terms_raw: str = ""
    content_hash: str = ""


class SourceHealth(BaseModel):
    bank_id: str
    bank_name: str
    requested_url: str
    resolved_url: str = ""
    status: Literal["complete", "partial", "failed", "blocked"] = "failed"
    campaign_count: int = 0
    offer_count: int = 0
    checked_at: datetime | None = None
    message: str = ""
