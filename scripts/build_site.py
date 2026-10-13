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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from radar.emit import (  # noqa: E402
    build_index,
    carry_forward,
    dedupe_campaigns,
    describe_carry_forward,
    describe_dedupe,
    portals_of,
    write_site,
)
from radar.fetch import source_fetcher  # noqa: E402
from radar.guard import assess, describe  # noqa: E402
from radar.models import Alert, Campaign, SourceHealth  # noqa: E402
from radar.pagestore import PageStore  # noqa: E402
from radar.report import describe_source  # noqa: E402
from radar.runner import SourceResult, run_source  # noqa: E402
from radar.spec import SourceSpec, load_specs  # noqa: E402
from radar.transport import HttpCache  # noqa: E402

SITE = ROOT / "web" / "public"
INDEX_PATH = SITE / "data" / "index.json"
REPORT_PATH = ROOT / "reports" / "latest.json"
SNAPSHOT_MAX_AGE = timedelta(hours=12)


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
    runner: str,
) -> None:
    """把原始抓取結果寫成快照，供另一個 runner 合併。

    刻意存 Campaign 而不是已組好的網站檔案：解析與去重都要看**全部**來源
    才能做對（鏡射頁去重要比較整頁清單，涵蓋率防護要看全站筆數），所以
    「抓取」與「組裝」必須分開，只有抓取需要分流到不同網路位置。
    """
    payload = {
        "generated_at": now.isoformat(),
        "runner": runner,
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
    path: Path | None, delegated: list[SourceSpec], now: datetime,
    *, runner_label: str = "自架 runner",
) -> tuple[list[Campaign], list[SourceHealth], list[Alert]]:
    """只合併指定抓取位置的新快照；缺席或驗證失敗須保留來源失敗紀錄。"""
    raw: dict[str, Any] | None = None
    if path is not None and path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            raw = loaded if isinstance(loaded, dict) else None
        except (json.JSONDecodeError, OSError):
            raw = None

    problem = f"{runner_label} 未回報（機器離線或該次執行失敗）"
    if raw is not None:
        try:
            stamp = datetime.fromisoformat(str(raw["generated_at"]))
            if stamp.tzinfo is None or now - stamp > SNAPSHOT_MAX_AGE:
                raise ValueError("快照缺少時區或超過 12 小時")
            if stamp - now > timedelta(minutes=5):
                raise ValueError("快照時間在未確認的未來")
            if {raw.get("runner")} != {spec.runner for spec in delegated}:
                raise ValueError("快照抓取位置與來源設定不符")
            campaigns = [Campaign.model_validate(item) for item in raw.get("campaigns", [])]
            health = [SourceHealth.model_validate(item) for item in raw.get("health", [])]
            alerts = [Alert.model_validate(item) for item in raw.get("alerts", [])]
            expected = {spec.id for spec in delegated}
            if len(health) != len(expected) or {item.bank_id for item in health} != expected:
                raise ValueError("快照來源缺漏、重複或不在預期範圍")
            if any(item.bank_id not in expected for item in [*campaigns, *alerts]):
                raise ValueError("快照活動或警示含非預期來源")
            if len({item.id for item in campaigns}) != len(campaigns):
                raise ValueError("快照活動 ID 重複")
            for item in health:
                own = [c for c in campaigns if c.bank_id == item.bank_id]
                if item.campaign_count != len(own) or item.offer_count != sum(
                    len(c.offers) for c in own
                ):
                    raise ValueError("快照來源筆數與實際活動不符")
            return campaigns, health, alerts
        except (ValueError, TypeError, KeyError) as exc:
            problem = f"{runner_label} 快照未通過驗證：{exc}"

    missing = "、".join(spec.bank_name for spec in delegated)
    print(f"\n{problem}，以 failed 記錄：{missing}")
    return (
        [],
        [
            SourceHealth(
                bank_id=spec.id,
                bank_name=spec.bank_name,
                requested_url=spec.listing.entry_url,
                status="failed",
                message=problem,
            )
            for spec in delegated
        ],
        [
            Alert(
                type="source_failed",
                bank_id=spec.id,
                bank_name=spec.bank_name,
                message=f"{problem}，本次沒有這家銀行的新資料",
            )
            for spec in delegated
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", default=None, help="只跑指定的來源 id")
    parser.add_argument("--today", default=date.today().isoformat())
    parser.add_argument("--dry-run", action="store_true", help="不寫任何檔案")
    parser.add_argument(
        "--runner",
        choices=("cloud", "cloud-macos", "self-hosted", "all"),
        default="all",
        help="只跑指定網路位置的來源（見 spec 的 runner 欄位）",
    )
    parser.add_argument(
        "--snapshot-out",
        default="",
        help="把本次抓到的原始結果寫成快照後結束，不寫網站檔案。"
        "用於其他 runner 先抓、雲端 Linux runner 再合併發布。",
    )
    parser.add_argument(
        "--snapshot-in",
        default="",
        help="合併另一個網路位置產生的快照。檔案不存在時，那一批來源會以 "
        "failed 誠實回報（不會從來源清單裡消失）。",
    )
    parser.add_argument("--macos-snapshot-in", default="", help="合併受管 macOS 的來源快照")
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
    if not all_specs or (not specs and args.snapshot_out):
        print("沒有符合條件的來源", file=sys.stderr)
        return 1

    previous_index = _load_previous()
    pages = PageStore(ROOT / "var" / "pages")

    def run_one(spec: SourceSpec) -> tuple[SourceSpec, SourceResult, HttpCache]:
        # 每家銀行各自的 HttpCache，避免平行執行時共寫同一個檔案。
        cache = HttpCache.load(ROOT / "var" / "http" / f"{spec.id}.json")
        with source_fetcher(spec, cache=cache) as fetcher:
            return spec, run_source(spec, fetcher, today=today, now=now, pages=pages), cache

    # 按銀行平行執行。不同銀行是不同主機，per-host 節流仍然成立，
    # wall clock 從「所有銀行相加」變成「最慢的單一銀行」。
    results = []
    if specs:
        with ThreadPoolExecutor(max_workers=min(6, len(specs))) as pool:
            results = list(pool.map(run_one, specs))

    campaigns: list[Campaign] = []
    health: list[SourceHealth] = []
    alerts: list[Alert] = []
    stats: dict[str, dict[str, int]] = {}

    if args.snapshot_out:
        _write_snapshot(Path(args.snapshot_out), results, now, args.runner)
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
        for runner, filename, label in (
            ("self-hosted", args.snapshot_in, "自架 runner"),
            ("cloud-macos", args.macos_snapshot_in, "受管 macOS runner"),
        ):
            group = [spec for spec in delegated if spec.runner == runner]
            if not group:
                continue
            merged, merged_health, merged_alerts = _read_snapshot(
                Path(filename) if filename else None, group, now, runner_label=label
            )
            campaigns.extend(merged)
            health.extend(merged_health)
            alerts.extend(merged_alerts)
            for item in merged_health:
                print(f"{item.bank_name}（{item.bank_id}）  {item.status}（來自{label}）")

    # 去重在 build_index 之前，但 health 保持未去重 —— 涵蓋率防護的比較基準
    # 必須是「這次真的從官方頁讀到幾筆」，否則去重造成的一次性下降會被記成
    # 抓取退步（實測星展 68→43，掉 36.8%，逐來源門檻是 40%）。
    campaigns, dedupe = dedupe_campaigns(campaigns)

    # 本次完全讀不到的來源沿用上一版並標記為舊資料。放在去重之後 —— 沿用的
    # 是「上一版已經去重完的輸出」，再跑一次去重沒有意義。
    carried, carried_report = carry_forward(
        site_root=SITE, health=health, previous_index=previous_index, now=now
    )

    index = build_index(
        campaigns,
        health=health,
        alerts=alerts,
        generated_at=now,
        portals=portals_of(campaigns),
        dedupe=dedupe,
        carried=carried,
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
    for line in describe_carry_forward(carried_report):
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
        written = write_site(SITE, index, campaigns, now=now, carried=carried)
        print()
        for path in written:
            size = path.stat().st_size
            print(f"寫出 {path.relative_to(ROOT)}  {size:,} bytes")

    return 0 if all(item.status == "complete" for item in health) else 2


if __name__ == "__main__":
    raise SystemExit(main())
