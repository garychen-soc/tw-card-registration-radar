"""發布前閘門（審查 🟡-1：只看精確率擋不住漏資料、golden 過期後恆為 100%）。"""

from __future__ import annotations

from typing import Any

from radar.gate import evaluate


def score(precision: float = 1.0, recall: float = 1.0, matched: int = 68, wrong: int = 0,
          missed: int = 0, not_found: list[str] | None = None) -> dict[str, Any]:
    return {"precision": precision, "recall": recall, "matched_slots": matched,
            "wrong_slots": wrong, "missed_slots": missed, "not_found": not_found or []}


def test_healthy_run_passes_silently() -> None:
    verdict = evaluate(score(), {"台新銀行": (198, 0)})
    assert verdict.block == "" and verdict.warnings == []


def test_low_precision_blocks() -> None:
    assert "不發布" in evaluate(score(precision=0.85)).block


def test_missing_data_warns_even_when_precision_is_perfect() -> None:
    verdict = evaluate(score(recall=0.8, missed=14, not_found=["a", "b"]))
    assert verdict.block == ""
    assert any("召回" in w for w in verdict.warnings)
    assert any("找不到" in w for w in verdict.warnings)


def test_expired_golden_window_is_flagged_not_trusted() -> None:
    # 窗口過期：沒有可評分的時點時精確率、召回都是 1.0，不能當成通過
    verdict = evaluate(score(matched=0))
    assert any("快失效" in w for w in verdict.warnings)


def test_still_valid_activities_vanishing_warns() -> None:
    # 11-05 實例：台新仍有效的 198 筆中消失 19 筆、國泰消失 1 筆（逾時）
    verdict = evaluate(score(), {"台新銀行": (198, 19), "國泰世華": (146, 1)})
    assert verdict.warnings == ["仍在期間內的活動消失：台新銀行 少 19/198 筆"]


def test_small_source_losing_a_large_share_warns() -> None:
    verdict = evaluate(score(), {"陽信銀行": (24, 4)})
    assert any("陽信銀行" in w for w in verdict.warnings)


def test_suspect_golden_entries_are_quarantined_and_published() -> None:
    # 11-14 實例：精確率 0.88，錯誤全來自 golden 漏看原文的條目
    report = {**score(precision=0.88, wrong=4),
              "suspects": [{"id": "dbs-dutyfree"}, {"id": "dbs-domestictravel"}],
              "trusted": {"precision": 1.0, "recall": 1.0, "entries": 45}}
    verdict = evaluate(report)
    assert verdict.block == ""
    assert any("自我修復" in w and "dbs-dutyfree" in w for w in verdict.warnings)


def test_too_many_suspects_still_blocks() -> None:
    report = {**score(precision=0.6, wrong=30),
              "suspects": [{"id": f"x{i}"} for i in range(8)],
              "trusted": {"precision": 1.0, "recall": 1.0, "entries": 39}}
    assert "不發布" in evaluate(report).block


def test_suspects_that_do_not_explain_errors_still_block() -> None:
    report = {**score(precision=0.8, wrong=10),
              "suspects": [{"id": "a"}],
              "trusted": {"precision": 0.85, "recall": 1.0, "entries": 46}}
    assert "不發布" in evaluate(report).block
