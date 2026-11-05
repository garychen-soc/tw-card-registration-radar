"""LLM token 用量：從 claude -p 回應取數、每輪累計、寫入 var/usage.jsonl。"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from radar import cli, pipeline
from radar.extract import locate as locate_mod
from radar.extract.locate import Meter, Request, envelope_usage

# 2026-11-06 實測 claude -p --output-format json 的回應形狀（節錄）
ENVELOPE: dict[str, Any] = {
    "total_cost_usd": 0.012972,
    "duration_ms": 1251,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 3112,
        "cache_read_input_tokens": 0,
        "output_tokens": 52,
    },
    "modelUsage": {
        "claude-sonnet-5-5": {
            "inputTokens": 2,
            "outputTokens": 52,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 3112,
            "costUSD": 0.012972,
        },
        "claude-haiku-5-5": {
            "inputTokens": 10,
            "outputTokens": 5,
            "cacheReadInputTokens": 100,
            "cacheCreationInputTokens": 0,
        },
    },
}


def test_envelope_usage_sums_all_models() -> None:
    assert envelope_usage(ENVELOPE) == {
        "input": 12, "output": 57, "cache_creation": 3112, "cache_read": 100,
    }


def test_envelope_usage_falls_back_to_usage_block() -> None:
    envelope = {k: v for k, v in ENVELOPE.items() if k != "modelUsage"}
    assert envelope_usage(envelope) == {
        "input": 2, "output": 52, "cache_creation": 3112, "cache_read": 0,
    }


def test_locate_records_usage_in_meter_and_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = dict(ENVELOPE, structured_output={"registration": {}}, result="")

    def fake_run(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, json.dumps(envelope), "")

    monkeypatch.setattr(locate_mod, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(locate_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(locate_mod, "_claude_bin", lambda: "claude")
    meter = Meter()
    request = Request(title="t", period="", text="每月1日開放登錄")
    locate_mod.locate(request, "sonnet", meter)
    locate_mod.locate(request, "sonnet", meter)  # 第二次是快取命中，不計

    assert meter.as_dict()["calls"] == 1
    assert meter.tokens == 12 + 57 + 3112 + 100
    record = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert record["usage"]["cache_creation"] == 3112


def test_usage_log_roundtrip_and_table(tmp_path: Path) -> None:
    meter = Meter()
    meter.add({"input": 10, "output": 900, "cache_creation": 4000, "cache_read": 20000}, 0.05, 7000)
    meter.add({"input": 5, "output": 800, "cache_creation": 0, "cache_read": 24000}, 0.03, 6000)
    result = pipeline.RunResult(
        datetime.now(ZoneInfo("Asia/Taipei")), {}, [], 2, 0, True, [],
        {"model": "sonnet", **meter.as_dict()},
    )
    log = tmp_path / "usage.jsonl"
    pipeline.log_usage(result, None, path=log)
    pipeline.log_usage(result, ["yuanta"], path=log)
    rows = pipeline.read_usage(log)

    assert [r["scope"] for r in rows] == ["all", "yuanta"]
    assert rows[0]["total_tokens"] == 49715
    table = cli._usage_table(rows, days=1)
    assert "共 2 輪：4 次呼叫、99,430 tokens、約 US$0.16" in table
    assert "49,715" in cli._usage_line(rows[0])
