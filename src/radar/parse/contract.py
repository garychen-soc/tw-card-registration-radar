"""時序契約推導 —— 回答「我會不會白刷」。

舊 schema 用 ``registration_required: bool`` 把「登錄與消費的先後關係」壓成
一個布林值。實測 242 筆需登錄活動中有 90.5% 無法回答「登錄期間 vs 消費期間」，
而這正是使用者最怕搞錯的地方：登錄成功但消費期已過、或先刷了才登錄導致不計入。

四種型態（geometry 加上文字訊號共同判定）：

* ``register_before_spend``     先登錄後消費，登錄前的消費不計入
* ``retroactive_ok``            可事後補登錄，較安全
* ``registration_closes_early`` 登錄先截止，錯過就整檔白刷
* ``per_period_reregister``     每期／每月需重新登錄
"""

from __future__ import annotations

import re
from datetime import date, datetime

from ..models import ContractKind, Period, Recurrence, RegistrationWindow, TimingContract
from .normalize import normalize

# 「登錄之後的消費才算」—— 最危險的一型，先刷就白刷
_BEFORE_SPEND = (
    re.compile(r"登錄後.{0,16}?消費"),
    re.compile(r"成功登錄.{0,24}?(?:刷|消費|使用)"),
    re.compile(r"消費前.{0,12}?(?:完成)?登錄"),
    re.compile(r"(?:未|非)登錄.{0,16}?(?:不予|不列|不計|無法)"),
    re.compile(r"完成登錄後.{0,16}?(?:之)?消費"),
    re.compile(r"先.{0,4}登錄.{0,8}再.{0,4}(?:消費|刷)"),
)
# 「消費完再補登錄也算」
_RETROACTIVE = (
    re.compile(r"消費後.{0,12}?登錄"),
    re.compile(r"補登錄"),
    re.compile(r"回溯"),
    re.compile(r"登錄前.{0,12}?(?:之)?消費.{0,12}?(?:亦|也|均)?.{0,6}(?:計入|列入|享)"),
)


def _last_chance(windows: list[RegistrationWindow]) -> datetime | None:
    candidates = [w.end for w in windows if w.end is not None]
    if candidates:
        return max(candidates)
    starts = [w.start for w in windows if w.start is not None]
    return max(starts) if starts else None


def _first_open(windows: list[RegistrationWindow]) -> datetime | None:
    starts = [w.start for w in windows if w.start is not None]
    if starts:
        return min(starts)
    ends = [w.end for w in windows if w.end is not None]
    return min(ends) if ends else None


def derive(
    *,
    period: Period,
    windows: list[RegistrationWindow],
    recurrence: Recurrence,
    raw_text: str = "",
) -> TimingContract:
    text = normalize(raw_text)
    before_spend = any(pattern.search(text) for pattern in _BEFORE_SPEND)
    retroactive = any(pattern.search(text) for pattern in _RETROACTIVE)

    first_open = _first_open(windows)
    last_chance = _last_chance(windows)

    kind: ContractKind = "unknown"
    confidence = 0.0

    if recurrence.kind != "once":
        kind = "per_period_reregister"
        confidence = recurrence.confidence
    elif before_spend:
        kind = "register_before_spend"
        confidence = 0.85
    elif retroactive:
        kind = "retroactive_ok"
        confidence = 0.80
    elif period.end is not None and last_chance is not None:
        # 純幾何判定：登錄能撐到活動結束之後就是可補登錄，否則就是登錄先截止
        if last_chance.date() >= period.end:
            kind = "retroactive_ok"
            confidence = 0.65
        else:
            kind = "registration_closes_early"
            confidence = 0.70
    elif windows and period.end is None:
        # 官方沒公告活動結束日，無法判斷時序 —— 誠實留 unknown
        kind = "unknown"
        confidence = 0.0

    # 只有明確是「先登錄後消費」時，消費起算日才是登錄開放時間；
    # 其餘型態的消費起算日就是活動起始日。
    spend_from: datetime | None = None
    if kind == "register_before_spend" and first_open is not None:
        spend_from = first_open
    elif period.start is not None:
        spend_from = datetime.combine(period.start, datetime.min.time())

    days_left: int | None = None
    if period.end is not None and last_chance is not None:
        days_left = (period.end - last_chance.date()).days

    return TimingContract(
        kind=kind,
        spend_counts_from=spend_from,
        last_chance_to_register=last_chance,
        spend_days_left_after_registering=days_left,
        confidence=round(confidence, 2),
    )


def spend_window(period: Period, contract: TimingContract) -> tuple[date | None, date | None]:
    """實際有效的消費區間 —— 活動期間與登錄時序的交集。

    這是前端雙軌時序帶要畫的灰色軌與彩色軌的重疊區：使用者真正能刷、
    且刷了會被計入的那一段。
    """
    start = period.start
    if contract.kind == "register_before_spend" and contract.spend_counts_from is not None:
        candidate = contract.spend_counts_from.date()
        start = candidate if start is None else max(start, candidate)
    return start, period.end
