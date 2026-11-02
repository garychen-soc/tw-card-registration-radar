"""登錄規則驗證與換算。LLM 輸出以手寫 dict 模擬，案例取自 2026-11 官方原文。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from radar.extract.schedule import Context, build

TPE = ZoneInfo("Asia/Taipei")
OBSERVED = datetime(2026, 11, 1, 8, 0, tzinfo=TPE)


def rule(quote: str, kind: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "quote": quote,
        "kind": kind,
        "is_example": False,
        "holiday": "none",
        "start": None,
        "end": None,
        "days_of_month": [],
        "nth": None,
        "weekday": None,
        "dates": [],
        "open_time": None,
        "close_time": None,
        "end_of_month": False,
    }
    base.update(fields)
    return base


def point(
    month: int,
    day: int,
    hour: int | None = None,
    minute: int | None = None,
    year: int | None = None,
) -> dict[str, Any]:
    return {"year": year, "month": month, "day": day, "hour": hour, "minute": minute}


def output(*rules: dict[str, Any], name: str = "", **claims: Any) -> dict[str, Any]:
    sub = {
        "name": name,
        "rules": list(rules),
        "reregister": claims.get("reregister", {"value": "unclear", "quote": ""}),
        "order": claims.get("order", {"value": "unclear", "quote": ""}),
        "quota": claims.get("quota", {"seats": None, "quote": ""}),
    }
    return {"registration": {"value": "required", "quote": ""}, "subs": [sub]}


def ctx(
    text: str, start: date | None = date(2026, 10, 1), end: date | None = date(2026, 12, 31)
) -> Context:
    return Context(
        text=text,
        url="https://example.bank/a",
        period_start=start,
        period_end=end,
        observed=OBSERVED,
    )


def stamps(result: Any, index: int = 0) -> list[tuple[str, str | None]]:
    return [
        (
            s.start.strftime("%m/%d %H:%M") if s.start else None,
            s.end.strftime("%m/%d %H:%M") if s.end else None,
        )
        for s in result.subs[index].slots
    ]


OBANK = (
    "本活動於每月01日中午12:00起開放登錄(遇假日順延至次一營業日)，活動登錄須於消費當月完成始享有回饋，"
    "每月限額3,000名，額滿將於活動登錄頁顯示。 (登錄日期如: 10月01日、11月02日、12月01日)\n"
    "持卡人須於刷卡消費之當月月底前完成活動登錄，始符合回饋資格。"
)


def test_holiday_shift_matches_enumerated_dates_and_month_end_close() -> None:
    out = output(
        rule(
            "本活動於每月01日中午12:00起開放登錄(遇假日順延至次一營業日)",
            "monthly",
            holiday="next_business_day",
            days_of_month=[1],
            open_time={"hour": 12, "minute": 0},
        ),
        rule(
            "(登錄日期如: 10月01日、11月02日、12月01日)",
            "enumerated",
            dates=[point(10, 1), point(11, 2), point(12, 1)],
        ),
        rule("持卡人須於刷卡消費之當月月底前完成活動登錄", "deadline", end_of_month=True),
        quota={"seats": 3000, "quote": "每月限額3,000名"},
    )
    result = build(out, ctx(OBANK))
    assert stamps(result) == [
        ("10/01 12:00", "10/31 23:59"),
        ("11/02 12:00", "11/30 23:59"),
        ("12/01 12:00", "12/31 23:59"),
    ]
    november = result.subs[0].slots[1]
    assert november.holiday_shifted
    assert "順延至 11/2" in november.note
    assert result.subs[0].quota == 3000


FIRST = (
    "登錄時間：活動期間內，每月1日、11日、21日早上9點開放當月登錄，每次開放登錄期間限量12,500卡\n"
    "每卡每月限參加登錄1次；舉例：1月1日早上9點已登錄成功，後於1月11日再次進行登錄，系統將回覆「本月份已登錄」"
)


def test_multiple_days_per_month_and_example_sentence_ignored() -> None:
    out = output(
        rule(
            "每月1日、11日、21日早上9點開放當月登錄",
            "monthly",
            days_of_month=[1, 11, 21],
            open_time={"hour": 9, "minute": 0},
        ),
        rule("舉例：1月1日早上9點已登錄成功", "opens_at", is_example=True, start=point(1, 1, 9, 0)),
    )
    result = build(out, ctx(FIRST, start=date(2026, 1, 1)))
    starts = [s for s, _ in stamps(result)]
    assert starts == [
        "10/01 09:00",
        "10/11 09:00",
        "10/21 09:00",
        "11/01 09:00",
        "11/11 09:00",
        "11/21 09:00",
        "12/01 09:00",
        "12/11 09:00",
        "12/21 09:00",
    ]
    # 條款沒寫順延：11/1 週日照原日期，不得自行順延
    assert not any(s.holiday_shifted for s in result.subs[0].slots)


def test_yearless_date_is_anchored_to_activity_period_not_today() -> None:
    text = "登錄期間：1月1日09:00起開放登錄"
    out = output(rule("1月1日09:00起開放登錄", "opens_at", start=point(1, 1, 9, 0)))
    result = build(out, ctx(text, start=date(2026, 1, 1)))
    # v1 把它推成 2027/01/01；正確是 2026/01/01（已過，不上時間軸）
    assert result.subs[0].slots == []
    assert result.subs[0].review == []


def test_quote_not_in_text_is_rejected() -> None:
    out = output(
        rule(
            "每月5日10:00開放登錄",
            "monthly",
            days_of_month=[5],
            open_time={"hour": 10, "minute": 0},
        )
    )
    result = build(out, ctx("每月6日10:00開放登錄"))
    assert result.subs[0].slots == []
    assert any("引句不在原文" in reason for reason in result.subs[0].review)


def test_number_not_supported_by_quote_is_rejected() -> None:
    text = "每月1日早上9點開放登錄"
    out = output(
        rule(
            "每月1日早上9點開放登錄",
            "monthly",
            days_of_month=[1],
            open_time={"hour": 10, "minute": 0},
        )
    )
    result = build(out, ctx(text))
    assert result.subs[0].slots == []
    assert any("10 點" in reason for reason in result.subs[0].review)


def test_explicit_window_after_activity_end_goes_to_review() -> None:
    text = "登錄期間：2026/11/30 15:00起"
    out = output(rule("2026/11/30 15:00起", "opens_at", start=point(11, 30, 15, 0, year=2026)))
    result = build(out, ctx(text, start=date(2026, 11, 1), end=date(2026, 11, 12)))
    assert result.subs[0].slots == []
    assert any("晚於活動結束日" in reason for reason in result.subs[0].review)


def test_nth_weekday() -> None:
    text = "每月第一個週三16:00開放登錄"
    out = output(
        rule(text, "monthly_nth_weekday", nth=1, weekday=3, open_time={"hour": 16, "minute": 0})
    )
    result = build(out, ctx(text))
    assert [s for s, _ in stamps(result)] == ["10/07 16:00", "11/04 16:00", "12/02 16:00"]


def test_same_day_close_time() -> None:
    text = "每月1日08:00─24:00開放登錄"
    out = output(
        rule(
            text,
            "monthly",
            days_of_month=[1],
            open_time={"hour": 8, "minute": 0},
            close_time={"hour": 23, "minute": 59},
        )
    )
    result = build(out, ctx(text))
    assert stamps(result)[1] == ("11/01 08:00", "11/01 23:59")


def test_date_only_range_has_day_precision() -> None:
    text = "活動暨登錄期間:2026/11/1-12/31"
    out = output(
        rule(text, "range", start=point(11, 1, year=2026), end=point(12, 31)), name="三星加碼"
    )
    result = build(out, ctx(text + "\n三星加碼"))
    slot = result.subs[0].slots[0]
    assert slot.precision == "day"
    assert stamps(result) == [("11/01 00:00", "12/31 23:59")]
    assert result.subs[0].name == "三星加碼"


def test_pm_hour() -> None:
    text = "每月15日下午3點開放登錄至當月月底，額滿為止"
    out = output(
        rule(
            text,
            "monthly",
            days_of_month=[15],
            open_time={"hour": 15, "minute": 0},
            end_of_month=True,
        )
    )
    result = build(out, ctx(text))
    assert stamps(result)[1] == ("11/15 15:00", "11/30 23:59")


def test_monthly_range_without_month_and_bounded_slot_wins() -> None:
    text = "每月1日、11日、21日早上9點開放當月登錄\n每月 1日早上9點至每月 5日下午6點止"
    out = output(
        rule("每月1日、11日、21日早上9點開放當月登錄", "monthly", days_of_month=[1, 11, 21],
             open_time={"hour": 9, "minute": 0}),
        rule("每月 1日早上9點至每月 5日下午6點止", "range",
             start=point(None, 1, 9, 0), end=point(None, 5, 18, 0)),
    )
    result = build(out, ctx(text, start=date(2026, 1, 1)))
    slots = stamps(result)
    assert ("11/01 09:00", "11/05 18:00") in slots
    assert ("11/01 09:00", None) not in slots
    assert ("11/11 09:00", None) in slots


def test_end_of_month_beats_close_time() -> None:
    text = "開放登錄時間:當月15日早上10:00~當月最後一日23:59止"
    out = output(rule(text, "monthly", days_of_month=[15], open_time={"hour": 10, "minute": 0},
                      close_time={"hour": 23, "minute": 59}, end_of_month=True))
    result = build(out, ctx(text))
    assert ("11/15 10:00", "11/30 23:59") in stamps(result)
