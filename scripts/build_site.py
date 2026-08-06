#!/usr/bin/env python3
"""抓取並產出網站資料。

退出碼刻意與前身一致的語意分工：

* ``0`` 全部來源正常，已寫出網站資料
* ``2`` 部分來源不完整，但已通過發布防護並寫出
* ``4`` 發布防護阻擋 —— 只寫診斷報告，保留上一版網站資料，不得提交或部署

    python scripts/build_site.py --only ubot
    python scripts/build_site.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from radar.emit import (  # noqa: E402
    build_index,
    dedupe_campaigns,
    describe_dedupe,
    portals_of,
    write_site,
)
from radar.guard import assess, describe  # noqa: E402
from radar.models import Alert, Campaign, SourceHealth  # noqa: E402
from radar.pagestore import PageStore  # noqa: E402
from radar.report import describe_source  # noqa: E402
from radar.runner import SourceResult, run_source  # noqa: E402
from radar.spec import SourceSpec, load_specs  # noqa: E402
from radar.transport import DEFAULT_TIMEOUT, Fetcher, HttpCache  # noqa: E402

SITE = ROOT / "web" / "public"
INDEX_PATH = SITE / "data" / "index.json"
REPORT_PATH = ROOT / "reports" / "latest.json"


def _load_previous() -> dict[str, object] | None:
    if not INDEX_PATH.exists():
        return None
    try:
        payload = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", default=None, help="只跑指定的來源 id")
    parser.add_argument("--today", default=date.today().isoformat())
    parser.add_argument("--dry-run", action="store_true", help="不寫任何檔案")
    args = parser.parse_args()

    today = date.fromisoformat(args.today)
    now = datetime.now(UTC)
    specs = [
        spec
        for spec in load_specs(ROOT / "sources")
        if args.only is None or spec.id in set(args.only)
    ]
    if not specs:
        print("沒有符合條件的來源", file=sys.stderr)
        return 1

    previous_index = _load_previous()
    pages = PageStore(ROOT / "var" / "pages")

    def run_one(spec: SourceSpec) -> tuple[SourceSpec, SourceResult, HttpCache]:
        # 每家銀行各自的 HttpCache，避免平行執行時共寫同一個檔案。
        cache = HttpCache.load(ROOT / "var" / "http" / f"{spec.id}.json")
        with Fetcher(
            spec.domains,
            cache=cache,
            user_agent=spec.user_agent or None,
            timeout=spec.timeout_seconds or DEFAULT_TIMEOUT,
        ) as fetcher:
            return spec, run_source(spec, fetcher, today=today, now=now, pages=pages), cache

    # 按銀行平行執行。不同銀行是不同主機，per-host 節流仍然成立，
    # wall clock 從「所有銀行相加」變成「最慢的單一銀行」。
    with ThreadPoolExecutor(max_workers=min(6, len(specs))) as pool:
        results = list(pool.map(run_one, specs))

    campaigns: list[Campaign] = []
    health: list[SourceHealth] = []
    alerts: list[Alert] = []
    stats: dict[str, dict[str, int]] = {}

    for spec, result, cache in results:
        assert result.health is not None
        campaigns.extend(result.campaigns)
        health.append(result.health)
        alerts.extend(result.alerts)
        stats[spec.id] = result.stats.as_dict()
        for line in describe_source(result.health, result.campaigns):
            print(line)
        print(f"  統計 {result.stats.as_dict()}")
        if not args.dry_run:
            cache.save()

    # 去重在 build_index 之前，但 health 保持未去重 —— 涵蓋率防護的比較基準
    # 必須是「這次真的從官方頁讀到幾筆」，否則去重造成的一次性下降會被記成
    # 抓取退步（實測星展 68→43，掉 36.8%，逐來源門檻是 40%）。
    campaigns, dedupe = dedupe_campaigns(campaigns)

    index = build_index(
        campaigns,
        health=health,
        alerts=alerts,
        generated_at=now,
        portals=portals_of(campaigns),
        dedupe=dedupe,
    )
    guard = assess(
        health=health,
        # counts.offers 是去重前的筆數，與上一版 index.json 的同名欄位同語意
        current_offers=index["counts"]["offers"],
        previous_index=previous_index if args.only is None else None,
    )

    print()
    for line in describe_dedupe(dedupe):
        print(line)

    print()
    for line in describe(guard):
        print(line)

    if not args.dry_run:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(
            json.dumps(
                {
                    "generated_at": now.isoformat(),
                    "guard": guard.model_dump(),
                    "dedupe": dedupe.model_dump(),
                    "counts": index["counts"],
                    "sources": index["sources"],
                    "alerts": index["alerts"],
                    "stats": stats,
                },
                ensure_ascii=False,
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )

    if guard.blocked:
        return 4

    if not args.dry_run:
        written = write_site(SITE, index, campaigns, now=now)
        print()
        for path in written:
            size = path.stat().st_size
            print(f"寫出 {path.relative_to(ROOT)}  {size:,} bytes")

    return 0 if all(item.status == "complete" for item in health) else 2


if __name__ == "__main__":
    raise SystemExit(main())
