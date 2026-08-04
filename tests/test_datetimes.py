"""登錄時點解析測試。

分兩層：
* golden corpus —— 真實銀行原文，改動解析器時做回歸 diff
* 合成案例 —— 針對個別規則的邊界條件
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from radar.models import Period
from radar.parse.contract import derive
from radar.parse.datetimes import (
    detect_recurrence,
    drop_period_echoes,
    find_period,
    find_windows,
)

GOLDEN = json.loads(
    (Path(__file__).parent / "golden" / "registration_windows.json").read_text(encoding="utf-8")
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@pytest.mark.parametrize(
    "case",
    [c for c in GOLDEN["cases"] if "expect_windows" in c],
    ids=lambda c: c["id"],
)
def test_golden_windows(case: dict) -> None:
    windows = find_windows(
        case["text"],
        default_year=case["default_year"],
        reference=date(case["default_year"], 8, 1),
        source_url=case["source_url"],
    )
    expected = case["expect_windows"]
    actual = [
        {"kind": w.kind, "start": _iso(w.start), "end": _iso(w.end)} for w in windows
    ]
    assert len(actual) == len(expected), f"{case['why']}\n實際: {actual}"
    for got, want in zip(actual, expected, strict=True):
        assert got["kind"] == want["kind"], case["why"]
        assert got["start"] == want["start"], case["why"]
        assert got["end"] == want["end"], case["why"]
    for window, want in zip(windows, expected, strict=True):
        assert window.confidence >= want["min_confidence"], (
            f"{case['id']} confidence {window.confidence} < {want['min_confidence']}"
        )
        assert window.evidence is not None and window.evidence.text


@pytest.mark.parametrize(
    "case",
    [c for c in GOLDEN["cases"] if "expect_windows_after_echo_filter" in c],
    ids=lambda c: c["id"],
)
def test_golden_period_echo_filtered(case: dict) -> None:
    windows = find_windows(
        case["text"],
        default_year=case["default_year"],
        reference=date(case["default_year"], 8, 1),
    )
    start, end = (date.fromisoformat(v) for v in case["period"])
    kept = drop_period_echoes(windows, start, end)
    assert kept == [], f"{case['why']}\n未被剔除: {[w.model_dump() for w in kept]}"


@pytest.mark.parametrize(
    "case",
    [c for c in GOLDEN["cases"] if "expect_contract" in c],
    ids=lambda c: c["id"],
)
def test_golden_contract(case: dict) -> None:
    windows = find_windows(case["text"], default_year=case["default_year"])
    contract = derive(
        period=Period(start=date(2026, 8, 1), end=date(2026, 8, 31), confidence=0.9),
        windows=windows,
        recurrence=detect_recurrence(case["text"]),
        raw_text=case["text"],
    )
    assert contract.kind == case["expect_contract"], case["why"]


# ── 合成案例：個別規則的邊界條件 ──────────────────────────────


def test_seconds_do_not_break_range() -> None:
    """秒級與分級寫法必須產出相同結果 —— 這是舊實作最大的單一缺陷。"""
    with_seconds = find_windows(
        "登錄期間：2026/8/7 17:00:00~2026/8/31 23:59:00 開放登錄",
        default_year=2026,
    )
    without = find_windows(
        "登錄期間：2026/8/7 17:00~2026/8/31 23:59 開放登錄", default_year=2026
    )
    assert len(with_seconds) == len(without) == 1
    assert with_seconds[0].start == without[0].start
    assert with_seconds[0].end == without[0].end
    assert with_seconds[0].kind == "range"


def test_opens_at_leaves_end_none() -> None:
    """抓不到截止時間就留 None，不得用固定分鐘數編造。"""
    windows = find_windows("8/17 10:00 開放登錄，限量600名", default_year=2026)
    assert len(windows) == 1
    assert windows[0].kind == "opens_at"
    assert windows[0].end is None
    assert not windows[0].end_known


def test_deadline_has_no_start() -> None:
    windows = find_windows("登錄期限：2026/8/31 23:59 止", default_year=2026)
    assert len(windows) == 1
    assert windows[0].kind == "deadline"
    assert windows[0].start is None
    assert windows[0].end == datetime.fromisoformat("2026-08-31T23:59:00+08:00")
    assert windows[0].anchor == windows[0].end


def test_roc_year_and_fullwidth_tilde() -> None:
    windows = find_windows("登錄期間：115/8/7 17:00～115/8/31 23:59", default_year=2026)
    assert len(windows) == 1
    assert windows[0].start == datetime.fromisoformat("2026-08-07T17:00:00+08:00")
    assert windows[0].end == datetime.fromisoformat("2026-08-31T23:59:00+08:00")


def test_afternoon_marker() -> None:
    windows = find_windows("8/15 下午3點 開放登錄", default_year=2026)
    assert windows[0].start is not None
    assert windows[0].start.hour == 15


def test_noon_marker_keeps_twelve() -> None:
    windows = find_windows("8/15 中午12點 開放登錄", default_year=2026)
    assert windows[0].start is not None
    assert windows[0].start.hour == 12


def test_negative_wording_is_not_registration() -> None:
    assert find_windows("本活動無需登錄，2026/8/7 17:00 起自動享優惠", default_year=2026) == []


def test_spend_period_label_is_rejected() -> None:
    """『活動期間』比任何登錄字樣更靠近時，不得當成登錄視窗。"""
    assert (
        find_windows(
            "活動期間：2026/8/1~2026/8/31，完成登錄後享回饋", default_year=2026
        )
        == []
    )


def test_year_rolls_forward_across_new_year() -> None:
    """12 月的清單提到 1/15，指的是隔年。"""
    windows = find_windows(
        "登錄期間：12/20 10:00~1/15 23:59 開放登錄",
        default_year=2026,
        reference=date(2026, 12, 15),
    )
    assert len(windows) == 1
    assert windows[0].start is not None and windows[0].end is not None
    assert windows[0].start.year == 2026
    assert windows[0].end.year == 2027


def test_point_window_subsumed_by_range_is_dropped() -> None:
    """實測玉山活動二：原文同時寫「8/20 17:00開放登錄」與
    「2026/8/20 17:00~2026/8/31 23:59統一開放…登錄」。兩者是同一件事，
    只保留資訊完整的區間，否則 UI 會顯示兩個看似衝突的登錄時間。"""
    windows = find_windows(
        "8/20 17:00開放登錄(限量1,300名) 登錄辦法：2026/8/20 17:00~2026/8/31 23:59統一開放登錄",
        default_year=2026,
        reference=date(2026, 8, 1),
    )
    assert len(windows) == 1
    assert windows[0].kind == "range"
    assert windows[0].end == datetime.fromisoformat("2026-08-31T23:59:00+08:00")


def test_point_outside_any_range_is_kept() -> None:
    windows = find_windows(
        "8/17 10:00開放登錄 登錄辦法：2026/8/20 17:00~2026/8/31 23:59統一開放登錄",
        default_year=2026,
        reference=date(2026, 8, 1),
    )
    assert [w.kind for w in windows] == ["opens_at", "range"]


def test_same_day_range() -> None:
    windows = find_windows("8/15 開放登錄 10:00~18:00", default_year=2026)
    assert len(windows) == 1
    assert windows[0].kind == "range"
    assert windows[0].start is not None and windows[0].end is not None
    assert windows[0].start.date() == windows[0].end.date()


def test_invalid_date_is_skipped() -> None:
    assert find_windows("登錄期間：2026/2/30 10:00 開放登錄", default_year=2026) == []


def test_recurrence_monthly_day() -> None:
    recurrence = detect_recurrence("每月1日上午10:00開放登錄，需每月重新登錄")
    assert recurrence.kind == "monthly"
    assert "10:00" in recurrence.note


def test_recurrence_nth_weekday() -> None:
    recurrence = detect_recurrence("每月第二個星期三 下午2點 開放登錄")
    assert recurrence.kind == "monthly"


def test_recurrence_per_period() -> None:
    assert detect_recurrence("每期需重新登錄").kind == "per_campaign_period"


def test_recurrence_default_is_once() -> None:
    assert detect_recurrence("完成登錄一次即可").kind == "once"


def test_find_period_range() -> None:
    start, end, confidence, evidence = find_period(
        "活動期間：2026/8/1~2026/8/31", default_year=2026
    )
    assert start == date(2026, 8, 1)
    assert end == date(2026, 8, 31)
    assert confidence >= 0.8
    assert "活動期間" in evidence


def test_find_period_single_date_does_not_infer_end() -> None:
    """單一日期不推定結束日 —— 官方沒說就是沒說。"""
    start, end, _, _ = find_period("活動期間：2026/8/1 起", default_year=2026)
    assert start == date(2026, 8, 1)
    assert end is None
