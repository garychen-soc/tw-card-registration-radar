#!/usr/bin/env python3
"""開發用工具：跑全部來源並輸出可 diff 的品質指標 JSON。

唯讀（除了寫出指定的 JSON 檔）。改動解析器前後各跑一次再 diff，是本專案
唯一能證明「這次改動沒有把別的來源弄壞」的方法 —— 曾經有三次改動在單一
來源上有效、卻讓另外兩家的子活動數掉一半。

第一次執行會抓取全部頁面並填滿 ``var/pages``；之後的執行走條件式 GET，
伺服器回 304 時直接用本機存的頁面文字，因此前後兩次比較的是**同一份內容**。

    python scripts/metrics.py --out before.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from radar.pagestore import PageStore  # noqa: E402
from radar.runner import run_source  # noqa: E402
from radar.spec import load_spec  # noqa: E402
from radar.transport import Fetcher, HttpCache  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--today", default=date.today().isoformat())
    args = parser.parse_args()

    today = date.fromisoformat(args.today)
    cache = HttpCache.load(ROOT / "var" / "http_cache.json")
    pages = PageStore(ROOT / "var" / "pages")
    paths = sorted((ROOT / "sources").glob("*.toml"))
    if args.only:
        paths = [p for p in paths if p.stem in set(args.only)]

    report: dict[str, dict[str, object]] = {}
    for path in paths:
        spec = load_spec(path)
        with Fetcher(spec.domains, cache=cache) as fetcher:
            result = run_source(spec, fetcher, today=today, pages=pages)
        offers = [offer for campaign in result.campaigns for offer in campaign.offers]
        codes: Counter[str] = Counter()
        for offer in offers:
            codes.update(offer.review_codes)
        assert result.health is not None
        report[spec.id] = {
            "status": result.health.status,
            "campaigns": len(result.campaigns),
            "offers": len(offers),
            "with_period": sum(1 for o in offers if o.period.start),
            "with_period_end": sum(1 for o in offers if o.period.end),
            "registration_required": sum(1 for o in offers if o.registration.required),
            "with_window": sum(1 for o in offers if o.registration.windows),
            "needs_review": sum(1 for o in offers if o.needs_review),
            "contracts": dict(
                Counter(o.registration.timing_contract.kind for o in offers).most_common()
            ),
            "codes": dict(codes.most_common()),
        }
        line = report[spec.id]
        print(
            f"{spec.id:14s} {line['status']:9s} offers={line['offers']:4d} "
            f"win={line['with_window']:4d} review={line['needs_review']:4d}",
            flush=True,
        )

    cache.save()
    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )
    total = {
        key: sum(int(src[key]) for src in report.values())  # type: ignore[call-overload]
        for key in ("offers", "with_window", "registration_required", "needs_review")
    }
    print(f"合計 {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
