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
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from radar.emit import build_index, portals_of, write_site  # noqa: E402
from radar.guard import assess, describe  # noqa: E402
from radar.models import Alert, Campaign, SourceHealth  # noqa: E402
from radar.report import describe_source  # noqa: E402
from radar.runner import run_source  # noqa: E402
from radar.spec import load_specs  # noqa: E402
from radar.transport import Fetcher, HttpCache  # noqa: E402

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

    http_cache = HttpCache.load(ROOT / "var" / "http_cache.json")
    previous_index = _load_previous()

    campaigns: list[Campaign] = []
    health: list[SourceHealth] = []
    alerts: list[Alert] = []
    stats: dict[str, dict[str, int]] = {}

    for spec in specs:
        with Fetcher(spec.domains, cache=http_cache) as fetcher:
            result = run_source(spec, fetcher, today=today, now=now)
        assert result.health is not None
        campaigns.extend(result.campaigns)
        health.append(result.health)
        alerts.extend(result.alerts)
        stats[spec.id] = result.stats.as_dict()
        for line in describe_source(result.health, result.campaigns):
            print(line)

    if not args.dry_run:
        http_cache.save()

    index = build_index(
        campaigns,
        health=health,
        alerts=alerts,
        generated_at=now,
        portals=portals_of(campaigns),
    )
    guard = assess(
        health=health,
        current_offers=index["counts"]["offers"],
        previous_index=previous_index if args.only is None else None,
    )

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
