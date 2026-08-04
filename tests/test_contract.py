from __future__ import annotations

from datetime import date, datetime

from radar.models import Period, Recurrence, RegistrationWindow
from radar.parse.contract import derive, spend_window


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


AUGUST = Period(start=date(2026, 8, 1), end=date(2026, 8, 31), confidence=0.9)


def test_text_signal_wins_register_before_spend() -> None:
    contract = derive(
        period=AUGUST,
        windows=[
            RegistrationWindow(
                kind="opens_at", start=_dt("2026-08-07T17:00:00+08:00"), confidence=0.8
            )
        ],
        recurrence=Recurrence(),
        raw_text="成功登錄活動一，於2026/8/2刷玉山Unicard可獲得回饋",
    )
    assert contract.kind == "register_before_spend"
    # 消費起算日是登錄開放時間，不是活動起始日 —— 這是使用者最容易白刷的地方
    assert contract.spend_counts_from == _dt("2026-08-07T17:00:00+08:00")


def test_geometry_gives_retroactive_when_registration_outlasts_period() -> None:
    contract = derive(
        period=AUGUST,
        windows=[
            RegistrationWindow(
                kind="range",
                start=_dt("2026-08-07T17:00:00+08:00"),
                end=_dt("2026-09-10T23:59:00+08:00"),
                confidence=0.95,
            )
        ],
        recurrence=Recurrence(),
        raw_text="登錄期間至9/10",
    )
    assert contract.kind == "retroactive_ok"
    assert contract.spend_days_left_after_registering == -10


def test_geometry_gives_closes_early_when_registration_ends_first() -> None:
    contract = derive(
        period=AUGUST,
        windows=[
            RegistrationWindow(
                kind="range",
                start=_dt("2026-08-01T00:00:00+08:00"),
                end=_dt("2026-08-10T23:59:00+08:00"),
                confidence=0.95,
            )
        ],
        recurrence=Recurrence(),
        raw_text="登錄期間 8/1~8/10",
    )
    assert contract.kind == "registration_closes_early"
    # 登錄成功後還有 21 天可以消費
    assert contract.spend_days_left_after_registering == 21


def test_recurrence_takes_precedence() -> None:
    contract = derive(
        period=AUGUST,
        windows=[
            RegistrationWindow(
                kind="opens_at", start=_dt("2026-08-01T10:00:00+08:00"), confidence=0.8
            )
        ],
        recurrence=Recurrence(kind="monthly", note="每月1日開放登錄", confidence=0.85),
        raw_text="每月1日上午10:00開放登錄，需每月重新登錄",
    )
    assert contract.kind == "per_period_reregister"


def test_unknown_when_official_end_date_missing() -> None:
    """官方沒公告活動結束日就無法判斷時序 —— 誠實留 unknown，不猜。"""
    contract = derive(
        period=Period(start=date(2026, 8, 1), end=None, confidence=0.6),
        windows=[
            RegistrationWindow(
                kind="opens_at", start=_dt("2026-08-07T17:00:00+08:00"), confidence=0.8
            )
        ],
        recurrence=Recurrence(),
        raw_text="8/7 17:00 開放登錄",
    )
    assert contract.kind == "unknown"
    assert contract.confidence == 0.0


def test_spend_window_is_the_overlap() -> None:
    """前端雙軌時序帶要畫的重疊區：真正刷了會被計入的那一段。"""
    contract = derive(
        period=AUGUST,
        windows=[
            RegistrationWindow(
                kind="opens_at", start=_dt("2026-08-07T17:00:00+08:00"), confidence=0.8
            )
        ],
        recurrence=Recurrence(),
        raw_text="完成登錄後之消費始計入",
    )
    start, end = spend_window(AUGUST, contract)
    assert start == date(2026, 8, 7)
    assert end == date(2026, 8, 31)
