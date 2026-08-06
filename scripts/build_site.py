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
from typing import Any

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



def _write_snapshot(
    path: Path,
    results: list[tuple[SourceSpec, SourceResult, HttpCache]],
    now: datetime,
) -> None:
    """把原始抓取結果寫成快照，供另一個 runner 合併。

    刻意存 Campaign 而不是已組好的網站檔案：解析與去重都要看**全部**來源
    才能做對（鏡射頁去重要比較整頁清單，涵蓋率防護要看全站筆數），所以
    「抓取」與「組裝」必須分開，只有抓取需要分流到不同網路位置。
    """
    payload = {
        "generated_at": now.isoformat(),
        "campaigns": [campaign.model_dump(mode="json") for _, r, _ in results
                      for campaign in r.campaigns],
        "health": [r.health.model_dump(mode="json") for _, r, _ in results if r.health],
        "alerts": [alert.model_dump(mode="json") for _, r, _ in results for alert in r.alerts],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    for _, result, cache in results:
        assert result.health is not None
        for line in describe_source(result.health, result.campaigns):
            print(line)
        cache.save()
    print(f"\n快照寫出 {path}（{len(payload['campaigns'])} 個活動頁）")


def _read_snapshot(
    path: Path | None, delegated: list[SourceSpec], now: datetime
) -> tuple[list[Campaign], list[SourceHealth], list[Alert]]:
    """讀入另一個 runner 的快照。缺席時把那批來源記成 failed。

    「缺席」是預期會發生的：自架 runner 是使用者的機器，關機、睡眠、網路斷線
    都會讓它沒回報。那時其餘來源必須照樣發布（見 guard 的 UNUSABLE_SHARE_LIMIT），
    但這幾家要在網站的「來源狀態」面板上看得見。
    """
    raw: dict[str, Any] | None = None
    if path is not None and path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            raw = loaded if isinstance(loaded, dict) else None
        except (json.JSONDecodeError, OSError):
            raw = None

    if raw is None:
        missing = "、".join(spec.bank_name for spec in delegated)
        print(f"\n自架 runner 沒有回報，以 failed 記錄：{missing}")
        return (
            [],
            [
                SourceHealth(
                    bank_id=spec.id,
                    bank_name=spec.bank_name,
                    requested_url=spec.listing.entry_url,
                    status="failed",
                    message="自架 runner 未回報（機器離線或該次執行失敗）",
                )
                for spec in delegated
            ],
            [
                Alert(
                    type="source_failed",
                    bank_id=spec.id,
                    bank_name=spec.bank_name,
                    message="自架 runner 未回報，本次沒有這家銀行的資料",
                )
                for spec in delegated
            ],
        )

    campaigns = [Campaign.model_validate(item) for item in raw.get("campaigns", [])]
    health = [SourceHealth.model_validate(item) for item in raw.get("health", [])]
    alerts = [Alert.model_validate(item) for item in raw.get("alerts", [])]
    return campaigns, health, alerts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", default=None, help="只跑指定的來源 id")
    parser.add_argument("--today", default=date.today().isoformat())
    parser.add_argument("--dry-run", action="store_true", help="不寫任何檔案")
    parser.add_argument(
        "--runner",
        choices=("cloud", "self-hosted", "all"),
        default="all",
        help="只跑指定網路位置的來源（見 spec 的 runner 欄位）",
    )
    parser.add_argument(
        "--snapshot-out",
        default="",
        help="把本次抓到的原始結果寫成快照後結束，不寫網站檔案。"
        "用於自架 runner 先抓、雲端 runner 再合併發布。",
    )
    parser.add_argument(
        "--snapshot-in",
        default="",
        help="合併另一個網路位置產生的快照。檔案不存在時，那一批來源會以 "
        "failed 誠實回報（不會從來源清單裡消失）。",
    )
    args = parser.parse_args()

    today = date.fromisoformat(args.today)
    now = datetime.now(UTC)
    all_specs = [
        spec
        for spec in load_specs(ROOT / "sources")
        if args.only is None or spec.id in set(args.only)
    ]
    specs = [
        spec for spec in all_specs if args.runner == "all" or spec.runner == args.runner
    ]
    # 這一批沒跑到、但要合併進來的來源。快照缺席時它們要以 failed 出現在來源清單裡
    # —— 從清單裡靜默消失比顯示失敗更糟（ADR 0002 的核心教訓）。
    delegated = [spec for spec in all_specs if spec not in specs]
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

    if args.snapshot_out:
        _write_snapshot(Path(args.snapshot_out), results, now)
        return 0

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

    if delegated:
        merged, merged_health, merged_alerts = _read_snapshot(
            Path(args.snapshot_in) if args.snapshot_in else None, delegated, now
        )
        campaigns.extend(merged)
        health.extend(merged_health)
        alerts.extend(merged_alerts)
        for item in merged_health:
            print(f"{item.bank_name}（{item.bank_id}）  {item.status}（來自另一個 runner）")

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
