#!/usr/bin/env python3
"""開發用工具：跑完一個來源並印出診斷摘要。

唯讀 —— 不寫任何檔案、不提交、不部署。用來在接上輸出層之前確認 adapter
與 spec 對真實網站的行為。

    python scripts/run_source.py ubot --limit 3
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from radar.adapters.listing import read_listing  # noqa: E402
from radar.report import describe_offer, describe_source  # noqa: E402
from radar.runner import run_source  # noqa: E402
from radar.spec import load_spec  # noqa: E402
from radar.transport import DEFAULT_TIMEOUT, Fetcher, HttpCache  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec_id")
    parser.add_argument("--today", default=date.today().isoformat())
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 筆，0 表示全部")
    parser.add_argument("--offers", type=int, default=6, help="最多印出幾個子活動")
    args = parser.parse_args()

    spec = load_spec(ROOT / "sources" / f"{args.spec_id}.toml")
    today = date.fromisoformat(args.today)
    cache = HttpCache.load(ROOT / "var" / "http_cache.json")

    with Fetcher(
        spec.domains,
        cache=cache,
        user_agent=spec.user_agent or None,
        timeout=spec.timeout_seconds or DEFAULT_TIMEOUT,
    ) as fetcher:
        if args.limit:
            # 只取前 N 筆時，用一次性的假清單包裝真實 fetcher，避免整批抓取
            items = read_listing(spec, fetcher)[: args.limit]
            print(f"清單共讀到 {len(items)} 筆（已限制為前 {args.limit} 筆）")
            for item in items:
                print(f"  {item.title[:50]:50s} {item.url}")
            print()
        result = run_source(spec, fetcher, today=today)

    assert result.health is not None
    for line in describe_source(result.health, result.campaigns):
        print(line)
    print(f"  統計 {result.stats.as_dict()}")
    if result.alerts:
        print(f"  警示 {len(result.alerts)} 則")
        for alert in result.alerts[:5]:
            print(f"    [{alert.type}] {alert.message[:110]}")
    print()

    shown = 0
    for campaign in result.campaigns:
        for offer in campaign.offers:
            if shown >= args.offers:
                return 0
            print("─" * 78)
            for line in describe_offer(offer):
                print(line)
            shown += 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
