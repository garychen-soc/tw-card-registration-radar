"""發布防護。

沿用前身最好的一個設計 —— 「寧可留舊資料也不覆蓋」—— 並補上它的破口。

前身的 guard 只看三條：系統性 DNS 失敗、80% 來源掛掉、全站活動數跌幅 ≥50%
且 ≥2 來源失敗。破口在於**沒有逐來源檢查**：台北富邦當時有 212 筆（全站最大
來源），整個歸零時全站跌幅僅 19.8%，低於 50% 門檻，會靜默發布，只有網站
健康面板看得出來。

另一個實測到的問題：前身的 fallback 機制會把上一版仍在效期內的活動撈回來，
使跌幅指標失真 —— 2026-08-04 那次 16/17 來源全掛，activity_drop_percent
卻是 0.0%，完全靠 systemic_dns_failure 這條擋下。因此本實作的跌幅一律用
「本次真正讀到的」數量計算，不含任何沿用資料。
"""

from __future__ import annotations

from math import ceil
from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import SourceHealth

# 逐來源跌幅門檻。低於這個規模的來源不做回歸判斷，避免小數字的雜訊。
PER_SOURCE_MIN_BASELINE = 5
PER_SOURCE_DROP_LIMIT = 0.4
# failed／blocked 的來源合計掉了全站多少比例才擋下發布。
#
# 這條門檻是在兩個實測案例之間取的：
#
# * 前身 2026-08-01：台北富邦 212 筆（全站最大來源，1,071 筆的 19.8%）整個歸零。
#   那必須擋 —— 使用者會少掉五分之一的活動，而網站看起來完全正常。
# * 本專案 2026-08-06 的 CI 執行：華南逾時（30 筆）、陽信被 datacenter IP 封鎖
#   （24 筆），合計 54 筆、佔 1,538 筆的 3.5%。那不該擋 —— 擋下的代價是其餘
#   15 家正常來源的資料一起停止更新，而陽信的封鎖是持續性的，等於永久停更。
#   那正是 ADR 0002 記錄的前身病灶：防護正確擋下、警示有發，但使用者看到的是
#   一個看起來正常的過期網站。
#
# 對「回報 complete 卻筆數崩掉」的來源不套用這條門檻 —— 那種流失是靜默的、
# 沒有別的訊號，一律照 PER_SOURCE_DROP_LIMIT 擋。
UNUSABLE_SHARE_LIMIT = 0.15
TOTAL_DROP_LIMIT = 0.5
SYSTEMIC_FAILURE_RATIO = 0.5
CATASTROPHIC_FAILURE_RATIO = 0.8

ReasonCode = Literal[
    "systemic_source_failure",
    "catastrophic_source_failure",
    "per_source_coverage_regression",
    "total_coverage_regression",
]


class SourceRegression(BaseModel):
    bank_id: str
    bank_name: str
    previous_offers: int
    current_offers: int
    drop_percent: float


class PublishGuard(BaseModel):
    status: Literal["passed", "blocked"]
    reason_codes: list[ReasonCode] = Field(default_factory=list)
    source_total: int = 0
    source_failed: int = 0
    source_blocked: int = 0
    current_offers: int = 0
    previous_offers: int = 0
    total_drop_percent: float = 0.0
    regressions: list[SourceRegression] = Field(default_factory=list)
    published_snapshot_preserved: bool = False

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"


def _offers_by_source(index: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source in index.get("sources", []):
        if isinstance(source, dict) and isinstance(source.get("bank_id"), str):
            counts[source["bank_id"]] = int(source.get("offer_count") or 0)
    return counts


def assess(
    *,
    health: list[SourceHealth],
    current_offers: int,
    previous_index: dict[str, Any] | None,
) -> PublishGuard:
    total = len(health)
    failed = sum(1 for item in health if item.status == "failed")
    blocked_sources = sum(1 for item in health if item.status == "blocked")
    unusable = failed + blocked_sources

    reasons: list[ReasonCode] = []
    if total and unusable >= max(3, ceil(total * SYSTEMIC_FAILURE_RATIO)):
        reasons.append("systemic_source_failure")
    if total and unusable >= ceil(total * CATASTROPHIC_FAILURE_RATIO):
        reasons.append("catastrophic_source_failure")

    previous_offers = 0
    regressions: list[SourceRegression] = []
    if previous_index is not None:
        previous_offers = int(previous_index.get("counts", {}).get("offers") or 0)
        baseline = _offers_by_source(previous_index)
        current = {item.bank_id: item.offer_count for item in health}
        names = {item.bank_id: item.bank_name for item in health}
        # 兩類退步分開處理，因為它們的訊號強度不同（見 UNUSABLE_SHARE_LIMIT）。
        unusable_ids = {
            item.bank_id for item in health if item.status in {"failed", "blocked"}
        }
        silent: list[SourceRegression] = []
        loud: list[SourceRegression] = []
        unusable_lost = 0
        for bank_id, before in baseline.items():
            if before < PER_SOURCE_MIN_BASELINE:
                continue
            after = current.get(bank_id, 0)
            if after >= before:
                continue
            drop = 1 - (after / before)
            if drop <= PER_SOURCE_DROP_LIMIT:
                continue
            item = SourceRegression(
                bank_id=bank_id,
                bank_name=names.get(bank_id, bank_id),
                previous_offers=before,
                current_offers=after,
                drop_percent=round(drop * 100, 1),
            )
            if bank_id in unusable_ids:
                loud.append(item)
                unusable_lost += before - after
            else:
                silent.append(item)

        # 回報 complete 卻筆數崩掉 —— 靜默流失，一律擋。
        regressions.extend(silent)
        # 已經以 failed／blocked 大聲回報的來源：只有合計流失達到全站一定比例
        # 才擋。它們已經有來源健康度、警示與網站「來源狀態」面板三個出口。
        if previous_offers and unusable_lost / previous_offers >= UNUSABLE_SHARE_LIMIT:
            regressions.extend(loud)
    if regressions:
        reasons.append("per_source_coverage_regression")

    total_drop = 0.0
    if previous_offers:
        total_drop = max(0.0, 1 - (current_offers / previous_offers))
        if total_drop >= TOTAL_DROP_LIMIT:
            reasons.append("total_coverage_regression")

    return PublishGuard(
        status="blocked" if reasons else "passed",
        reason_codes=reasons,
        source_total=total,
        source_failed=failed,
        source_blocked=blocked_sources,
        current_offers=current_offers,
        previous_offers=previous_offers,
        total_drop_percent=round(total_drop * 100, 1),
        regressions=regressions,
        published_snapshot_preserved=bool(reasons) and previous_index is not None,
    )


def describe(guard: PublishGuard) -> list[str]:
    """給 CI job summary 與 Slack 用的說明。"""
    if not guard.blocked:
        return [
            f"發布防護通過：{guard.current_offers} 個子活動"
            f"（上一版 {guard.previous_offers}）",
            f"來源 {guard.source_total} 個，失敗 {guard.source_failed}、"
            f"被阻擋 {guard.source_blocked}",
        ]
    lines = [
        "發布防護阻擋，未覆寫網站資料",
        f"原因：{'、'.join(guard.reason_codes)}",
        f"來源失敗 {guard.source_failed}+被阻擋 {guard.source_blocked} / {guard.source_total}",
        f"本次子活動 {guard.current_offers}，上一版 {guard.previous_offers}"
        f"（跌幅 {guard.total_drop_percent}%）",
    ]
    for regression in guard.regressions:
        lines.append(
            f"  逐來源回歸：{regression.bank_name} "
            f"{regression.previous_offers} → {regression.current_offers}"
            f"（跌 {regression.drop_percent}%）"
        )
    if guard.published_snapshot_preserved:
        lines.append("已保留上一版網站資料，未提交、未部署。")
    return lines
