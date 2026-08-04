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
    "offer_boundary_missing": "本頁應含多個活動但未能切出邊界，請至官方頁確認對應的登錄時間",
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

# 這幾類直接動搖時序契約的可信度：契約是由登錄視窗與活動期間的幾何關係推導出來的，
# 若視窗本身落在期間之外或互相重疊，推導結果不該再掛著高信心。
# 實測案例：聯邦某頁留著去年的登錄期間（2025-07-01~2026-06-30），與清單期間
# （2026-07-01~2026-12-31）不符，卻推導出「登錄先截止，還有 184 天可消費」。
_TIMING_UNDERMINING = frozenset({"window_outside_period", "windows_overlap", "period_missing"})
MAX_CONFIDENCE_WHEN_INCONSISTENT = 0.3


def apply(offer: Offer, *, extra_codes: tuple[str, ...] = ()) -> Offer:
    """把檢查結果寫回 offer 的 needs_review / review_reasons。

    ``extra_codes`` 供呼叫端把自己偵測到的問題（例如切不出活動邊界）併入，
    讓「是否需要人工確認」的判斷只有這一處，不會被覆寫。
    """
    codes = [*check(offer), *extra_codes]
    offer.review_reasons = [VIOLATION_MESSAGES.get(code, code) for code in codes]
    offer.needs_review = any(code not in _INFORMATIONAL for code in codes)
    if codes:
        contract = offer.registration.timing_contract
        contract.consistency = codes
        if any(code in _TIMING_UNDERMINING for code in codes):
            contract.confidence = min(contract.confidence, MAX_CONFIDENCE_WHEN_INCONSISTENT)
    return offer
