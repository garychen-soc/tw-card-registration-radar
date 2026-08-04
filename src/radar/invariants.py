"""後置一致性檢查。

舊實作缺這一層，因此公開資料裡有 5 筆活動的登錄時點落在活動結束日之後
（玉山、國泰世華、凱基 ×2、元大），而 ``review_required`` 只涵蓋
「需登錄但抓不到時點」，反向的「抓到時點但自相矛盾」完全沒人檢查。

原則：違反 invariant 的資料**不得靜默輸出**。標記 ``needs_review`` 讓它進
「需人工確認」分頁並附官方頁連結，而不是混在主列表裡假裝可以直接行動。
"""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise

from .models import Offer

# 登錄時點允許略微超出活動期間的寬限。銀行常把登錄截止設在活動結束後幾天
# 供補登錄，這是合法的；超過就是解析出錯或活動粒度錯誤。
BOUNDARY_GRACE = timedelta(days=31)

VIOLATION_MESSAGES = {
    "window_outside_period": "登錄時點落在活動期間之外，可能是本頁含多個活動而被合併",
    "windows_overlap": "同一活動的登錄視窗互相重疊",
    "registration_without_window": "標記為需登錄，但抓不到任何登錄時點",
    "registration_end_unknown": "抓到登錄開放時間，但截止時間未確認",
    "contract_unknown": "無法判斷登錄與消費的先後關係",
    "threshold_not_monotonic": "階梯門檻的消費金額未遞增，可能解析錯位",
    "period_missing": "抓不到活動期間",
    "low_confidence_window": "登錄時點的解析信心不足",
}

MIN_WINDOW_CONFIDENCE = 0.6


def check(offer: Offer) -> list[str]:
    """回傳違反的 invariant 代碼，不修改 offer。"""
    codes: list[str] = []
    period = offer.period
    registration = offer.registration
    windows = registration.windows

    if period.start is None and period.end is None:
        codes.append("period_missing")

    for window in windows:
        anchor = window.anchor
        if period.start is not None and anchor.date() < period.start - BOUNDARY_GRACE:
            codes.append("window_outside_period")
            break
        if period.end is not None and anchor.date() > period.end + BOUNDARY_GRACE:
            codes.append("window_outside_period")
            break

    spans = sorted(
        ((w.start or w.end, w.end or w.start) for w in windows if w.kind == "range"),
        key=lambda pair: pair[0],  # type: ignore[arg-type,return-value]
    )
    for earlier, later in pairwise(spans):
        if earlier[1] is not None and later[0] is not None and later[0] < earlier[1]:
            codes.append("windows_overlap")
            break

    if registration.required and not windows:
        codes.append("registration_without_window")
    if windows and all(not w.end_known for w in windows):
        codes.append("registration_end_unknown")
    if windows and any(w.confidence < MIN_WINDOW_CONFIDENCE for w in windows):
        codes.append("low_confidence_window")
    if registration.required and registration.timing_contract.kind == "unknown":
        codes.append("contract_unknown")

    tiers = offer.conditions.threshold_tiers
    if len(tiers) > 1:
        amounts = [tier.spend_twd for tier in tiers]
        if amounts != sorted(amounts):
            codes.append("threshold_not_monotonic")

    return codes


# 這幾類只是資訊不完整，仍可讓使用者行動（顯示「截止時間未確認」即可），
# 不需要整筆退到「需人工確認」分頁。
_INFORMATIONAL = frozenset({"registration_end_unknown"})


def apply(offer: Offer) -> Offer:
    """把檢查結果寫回 offer 的 needs_review / review_reasons。"""
    codes = check(offer)
    offer.review_reasons = [VIOLATION_MESSAGES.get(code, code) for code in codes]
    offer.needs_review = any(code not in _INFORMATIONAL for code in codes)
    if codes:
        offer.registration.timing_contract.consistency = codes
    return offer
