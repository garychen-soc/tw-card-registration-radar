"""命令列入口。

    radar run [--banks a,b] [--model opus] [--llm-budget 200]   完整一輪並產生網站資料
    radar emit                                                   只從 state 重產網站資料
    radar probe <bank>                                           單一銀行擷取體檢
    radar score                                                  以 golden 集評分目前 state

退出碼：0 成功；2 參數錯誤；4 來源大量失敗、本輪不發布。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime

from . import pipeline
from .emit import write
from .extract.locate import DEFAULT_MODEL
from .models import Activity, SourceHealth


def _load_state() -> tuple[dict[str, list[Activity]], list[SourceHealth], datetime]:
    run_info = json.loads((pipeline.STATE / "run.json").read_text(encoding="utf-8"))
    health = [SourceHealth.model_validate(row) for row in run_info["sources"]]
    activities = {h.bank_id: pipeline.previous(h.bank_id) for h in health}
    return activities, health, datetime.fromisoformat(run_info["observed_at"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="radar")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--banks", default="")
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--llm-budget", type=int, default=200)
    run.add_argument("--llm-workers", type=int, default=3)
    sub.add_parser("emit")
    probe = sub.add_parser("probe")
    probe.add_argument("bank")
    sub.add_parser("score")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "run":
        bank_ids = [b for b in args.banks.split(",") if b] or None
        result = pipeline.run(
            bank_ids, model=args.model, llm_budget=args.llm_budget, llm_workers=args.llm_workers
        )
        for health in result.health:
            print(
                f"{health.bank_id:13s} {health.status:8s} stubs={health.stubs:4d} "
                f"activities={health.activities:4d} carried={health.carried:3d} {health.message}"
            )
        print(f"LLM 呼叫 {result.llm_calls} 次，失敗 {result.llm_failures}")
        for message in result.messages:
            print(message)
        if not result.publishable:
            return 4
        if bank_ids:
            # 只跑部分銀行時，其餘銀行沿用 state，避免把它們從網站移除
            activities, health, _ = (
                _load_state()
                if (pipeline.STATE / "run.json").exists()
                else ({}, [], result.observed)
            )
            activities.update(result.activities)
            merged = {h.bank_id: h for h in health}
            merged.update({h.bank_id: h for h in result.health})
            result.activities, result.health = activities, list(merged.values())
        pipeline.save(result)
        print(
            json.dumps(write(result.activities, result.health, result.observed), ensure_ascii=False)
        )
        return 0
    if args.command == "emit":
        activities, health, generated = _load_state()
        print(json.dumps(write(activities, health, generated), ensure_ascii=False))
        return 0
    if args.command == "probe":
        from .sources import probe as run_probe

        print(json.dumps(run_probe(args.bank), ensure_ascii=False, indent=1))
        return 0
    if args.command == "score":
        from .golden import score

        report = score()
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
