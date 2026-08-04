"""人可讀的診斷輸出。

給開發腳本與 CI job summary 用。刻意與資料模型分開：模型只放事實，
呈現方式（包含「登錄後還有幾天可消費」這種讀法）屬於呈現層。
"""

from __future__ import annotations

from .models import Campaign, Offer, SourceHealth

CONTRACT_LABELS = {
    "register_before_spend": "先登錄後消費（登錄前的消費不計入）",
    "retroactive_ok": "可事後補登錄",
    "registration_closes_early": "登錄先截止（錯過就整檔白刷）",
    "per_period_reregister": "每期需重新登錄",
    "unknown": "時序未確認",
}


def describe_offer(offer: Offer) -> list[str]:
    lines = [f"▸ {offer.title[:70]}"]
    period = offer.period
    lines.append(
        f"  活動期間  {period.start or '未確認'} ~ {period.end or '未公告'}"
        f"  (c={period.confidence})"
    )

    contract = offer.registration.timing_contract
    lines.append(
        f"  時序契約  {CONTRACT_LABELS.get(contract.kind, contract.kind)}"
        f"  (c={contract.confidence})"
    )
    if contract.last_chance_to_register:
        lines.append(f"            最晚登錄 {contract.last_chance_to_register:%Y-%m-%d %H:%M}")
    if contract.spend_days_left_after_registering is not None:
        lines.append(
            f"            登錄截止後還有 {contract.spend_days_left_after_registering} 天可消費"
        )
    if contract.grace_days_after_period_end is not None:
        lines.append(
            f"            活動結束後還能補登錄 {contract.grace_days_after_period_end} 天"
        )

    for window in offer.registration.windows:
        # 一律顯示年份。省略年份會讓「2025-07-01 → 2026-06-30」這種跨年區間
        # 看起來像結束早於開始，實測在聯邦的頁面上就誤導過一次。
        start = f"{window.start:%Y-%m-%d %H:%M}" if window.start else "未確認"
        end = f"{window.end:%Y-%m-%d %H:%M}" if window.end else "未確認"
        lines.append(f"  登錄視窗  [{window.kind:9s} c={window.confidence}] {start} → {end}")

    recurrence = offer.registration.recurrence
    if recurrence.kind != "once":
        lines.append(f"  重複登錄  {recurrence.note or recurrence.kind}")

    conditions = offer.conditions
    if conditions.threshold_tiers:
        tiers = " / ".join(_tier_label(tier) for tier in conditions.threshold_tiers)
        lines.append(f"  階梯門檻  [{conditions.threshold_kind}] {tiers}")
    if conditions.quota.limited:
        lines.append(f"  名額      限量 {conditions.quota.seats or '未公告數量'}（要搶）")

    eligibility = conditions.eligibility
    flags = [
        label
        for label, value in (
            ("新戶限定", eligibility.new_customer_only),
            ("首刷限定", eligibility.first_swipe_only),
            ("限正卡人", eligibility.primary_card_only),
        )
        if value
    ]
    if flags or eligibility.cards:
        cards = "、".join(eligibility.cards) if eligibility.cards else "未指定"
        lines.append(f"  資格      {'、'.join(flags) or '無限制'}  卡別 {cards}")

    installment = conditions.installment
    if installment.required or installment.periods:
        rate = f" {installment.rate}" if installment.rate else ""
        lines.append(f"  分期      需分期={installment.required} 期數={installment.periods}{rate}")

    portal = offer.registration.portal
    if portal.url:
        lines.append(f"  登錄入口  [{portal.kind}] {portal.hint or portal.url}")

    if offer.needs_review:
        lines.append(f"  ⚠ 需人工確認  {'；'.join(offer.review_reasons)}")
    return lines


def _tier_label(tier: object) -> str:
    from .models import ThresholdTier

    assert isinstance(tier, ThresholdTier)
    parts = [f"滿{tier.spend_twd:,}"]
    if tier.reward_twd is not None:
        parts.append(f"→{tier.reward_twd:,}元")
    if tier.reward_if_installment is not None:
        parts.append(f"(分期{tier.reward_if_installment:,}元)")
    if tier.quota_seats is not None:
        parts.append(f"限{tier.quota_seats}名")
    return "".join(parts)


def describe_source(health: SourceHealth, campaigns: list[Campaign]) -> list[str]:
    needs_review = sum(
        1 for campaign in campaigns for offer in campaign.offers if offer.needs_review
    )
    lines = [
        f"{health.bank_name}（{health.bank_id}）  {health.status}",
        f"  活動頁 {health.campaign_count} 個 / 子活動 {health.offer_count} 個"
        f" / 需人工確認 {needs_review} 個",
    ]
    if health.message:
        lines.append(f"  {health.message}")
    return lines
