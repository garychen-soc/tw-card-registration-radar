#!/usr/bin/env python3
"""開發用工具：把單一官方明細頁跑完整條解析鏈並印出結果。

用途是在寫 adapter 之前先驗證解析層對真實頁面的行為，也用來為 golden corpus
挑選節錄。唯讀，不寫任何檔案。

    python scripts/inspect_detail.py esun "https://www.esunbank.com/zh-tw/..."
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from radar import invariants  # noqa: E402
from radar.models import Evidence, Offer, Period, Registration  # noqa: E402
from radar.parse import conditions as cond  # noqa: E402
from radar.parse.contract import derive, spend_window  # noqa: E402
from radar.parse.datetimes import (  # noqa: E402
    detect_recurrence,
    drop_period_echoes,
    find_period,
    find_windows,
)
from radar.segment import registration_text, sections, split_offers, table_rows  # noqa: E402
from radar.spec import load_spec  # noqa: E402
from radar.transport import Fetcher  # noqa: E402


def build_offers(
    spec_id: str,
    url: str,
    html: str,
    text: str,
    *,
    today: date,
    known_cards: tuple[str, ...],
    boundary: str,
    table_tiers: bool,
) -> list[Offer]:
    headers, rows = table_rows(html) if table_tiers else ([], [])
    offers: list[Offer] = []
    for index, chunk in enumerate(split_offers(text, pattern=boundary or None)):
        start, end, period_confidence, evidence = find_period(
            chunk.text, default_year=today.year, reference=today
        )
        period = Period(
            start=start,
            end=end,
            confidence=period_confidence,
            evidence=Evidence(text=evidence, source_url=url) if evidence else None,
        )
        windows = drop_period_echoes(
            find_windows(
                chunk.text, default_year=today.year, reference=today, source_url=url
            ),
            start,
            end,
        )
        recurrence = detect_recurrence(chunk.text)
        contract = derive(
            period=period, windows=windows, recurrence=recurrence, raw_text=chunk.text
        )
        offer = Offer(
            id=f"{spec_id}-{index}",
            title=chunk.title,
            period=period,
            registration=Registration(
                required=bool(windows) or "登錄" in chunk.text,
                windows=windows,
                recurrence=recurrence,
                timing_contract=contract,
                raw_text=registration_text(chunk.text)[:1200],
            ),
            conditions=cond.extract(
                chunk.text,
                known_cards=known_cards,
                table_headers=headers or None,
                table_rows=rows or None,
            ),
        )
        if not chunk.split:
            offer.review_reasons.append("本頁未能切出子活動邊界")
        offers.append(invariants.apply(offer))
    return offers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec_id")
    parser.add_argument("url")
    parser.add_argument("--today", default=date.today().isoformat())
    args = parser.parse_args()

    spec = load_spec(ROOT / "sources" / f"{args.spec_id}.toml")
    today = date.fromisoformat(args.today)

    with Fetcher(spec.domains) as fetcher:
        response = fetcher.get(args.url, conditional=False)

    from selectolax.parser import HTMLParser

    tree = HTMLParser(response.text)
    for tag in tree.css("script, style, noscript"):
        tag.decompose()
    text = tree.body.text(separator="\n") if tree.body else response.text

    print(f"來源 {spec.id}（{spec.bank_name}）  HTTP {response.status_code}")
    print(f"最終 URL {response.final_url}")
    print(f"純文字 {len(text):,} 字  |  段落 {sorted(sections(text))}")
    print()

    offers = build_offers(
        spec.id,
        response.final_url,
        response.text,
        text,
        today=today,
        known_cards=tuple(spec.conditions.known_cards),
        boundary=spec.detail.boundary,
        table_tiers=spec.detail.table_tiers,
    )
    print(f"切出 {len(offers)} 個子活動")
    for offer in offers:
        print("─" * 78)
        print(f"▸ {offer.title[:70]}")
        period = offer.period
        print(f"  活動期間  {period.start} ~ {period.end}  (c={period.confidence})")
        contract = offer.registration.timing_contract
        print(f"  時序契約  {contract.kind}  (c={contract.confidence})")
        if contract.last_chance_to_register:
            print(f"            最晚登錄 {contract.last_chance_to_register:%Y-%m-%d %H:%M}")
        if contract.spend_days_left_after_registering is not None:
            print(f"            登錄後可消費 {contract.spend_days_left_after_registering} 天")
        spend_start, spend_end = spend_window(period, contract)
        print(f"  有效消費  {spend_start} ~ {spend_end}")
        for window in offer.registration.windows:
            end = f"{window.end:%m-%d %H:%M}" if window.end else "未確認"
            start = f"{window.start:%m-%d %H:%M}" if window.start else "未確認"
            print(f"  登錄視窗  [{window.kind:9s} c={window.confidence}] {start} → {end}")
        conditions = offer.conditions
        if conditions.threshold_tiers:
            tiers = " / ".join(
                f"{t.spend_twd:,}→{t.reward_twd or '?'}元"
                + (f"(分期{t.reward_if_installment}元)" if t.reward_if_installment else "")
                + (f"限{t.quota_seats}名" if t.quota_seats else "")
                for t in conditions.threshold_tiers
            )
            print(f"  階梯門檻  [{conditions.threshold_kind}] {tiers}")
        if conditions.quota.limited:
            print(f"  名額      限量 {conditions.quota.seats or '未公告數量'}")
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
            print(f"  資格      {'、'.join(flags)}  卡別 {eligibility.cards or '未指定'}")
        if conditions.installment.required or conditions.installment.periods:
            print(
                f"  分期      需分期={conditions.installment.required} "
                f"期數={conditions.installment.periods} {conditions.installment.rate}"
            )
        if offer.needs_review:
            print(f"  ⚠ 需人工確認  {'；'.join(offer.review_reasons)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
