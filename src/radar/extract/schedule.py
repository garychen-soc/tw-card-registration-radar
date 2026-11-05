"""把 LLM 定位出的登錄規則驗證後換算成具體時點。

每條規則必須通過三道檢查才會產生時點：

1. 引句逐字出現在條款原文（忽略空白差異）。
2. LLM 填的每個數字（月、日、時、分、每月第幾日…）都能在引句裡找到。
3. 換算出的時點落在活動期間內（開放日不晚於活動結束日、不早於活動開始前 60 天）。

沒寫年份的日期以「活動期間」定年，而不是以「今天」—— v1 用今天定年，
把條款裡「舉例：1月1日」推成隔年 1/1。條款寫「遇假日順延」才查辦公日曆
順延；條款同時列舉了實際日期時，以列舉為準。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..models import Evidence, Order, Registration, Reregister, Slot, SubActivity
from ..textnorm import normalize
from .holidays import MissingCalendar, next_business_day, previous_business_day

TAIPEI = ZoneInfo("Asia/Taipei")
EARLY_GRACE = timedelta(days=60)
KEEP_PAST = timedelta(days=31)
MAX_MONTHS = 15
_WEEKDAY_NAMES = "一二三四五六日"
_CN_DIGITS = {
    "一": 1,
    "二": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_PM = re.compile(r"下午|晚上|晚間|傍晚|PM|pm|p\.m\.")


def squash(text: str) -> str:
    return re.sub(r"\s+", "", normalize(text))


def numbers_in(quote: str) -> set[int]:
    value = normalize(quote)
    found = {int(n) for n in re.findall(r"\d+", value)}
    found |= {_CN_DIGITS[c] for c in re.findall(r"第([一二三四五])", value)}
    return found


@dataclass
class Context:
    text: str
    url: str
    period_start: date | None
    period_end: date | None
    observed: datetime
    _squashed: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self._squashed = squash(self.text)

    def quote_ok(self, quote: str) -> bool:
        needle = squash(quote)
        return len(needle) >= 4 and needle in self._squashed


class Rejected(ValueError):
    pass


# ---- 數字佐證 --------------------------------------------------------------


def _check_hour(hour: int, minute: int, quote: str, nums: set[int]) -> None:
    if hour == 23 and minute == 59 and (24 in nums or 23 in nums):
        return
    supported = (
        hour in nums
        or (hour >= 12 and (hour - 12) in nums and bool(_PM.search(quote)))
        or (hour == 12 and "中午" in quote)
    )
    if not supported:
        raise Rejected(f"時刻 {hour} 點在引句中找不到")
    if minute and minute not in nums:
        raise Rejected(f"分鐘 {minute} 在引句中找不到")


def _check_point(point: dict[str, Any] | None, quote: str, nums: set[int]) -> None:
    if not point:
        return
    for key in ("year", "month", "day"):
        value = point.get(key)
        if value is not None and value not in nums:
            raise Rejected(f"{key}={value} 在引句中找不到")
    if point.get("hour") is not None:
        _check_hour(point["hour"], point.get("minute") or 0, quote, nums)


def _check_rule(rule: dict[str, Any]) -> None:
    quote = rule["quote"]
    nums = numbers_in(quote)
    _check_point(rule.get("start"), quote, nums)
    _check_point(rule.get("end"), quote, nums)
    for point in rule.get("dates") or []:
        _check_point(point, quote, nums)
    for day in rule.get("days_of_month") or []:
        if day not in nums:
            raise Rejected(f"每月 {day} 日在引句中找不到")
    for key in ("open_time", "close_time"):
        clock = rule.get(key)
        if clock:
            _check_hour(clock["hour"], clock.get("minute") or 0, quote, nums)
    if rule["kind"] == "monthly_nth_weekday":
        nth, weekday = rule.get("nth"), rule.get("weekday")
        if not nth or not weekday or not 1 <= weekday <= 7:
            raise Rejected("每月第 N 個週 X 缺少 N 或 X")
        if nth not in nums:
            raise Rejected(f"第 {nth} 個在引句中找不到")
        if not re.search(rf"(?:週|周|星期|禮拜){_WEEKDAY_NAMES[weekday - 1]}", normalize(quote)):
            raise Rejected(f"週{_WEEKDAY_NAMES[weekday - 1]}在引句中找不到")


# ---- 換算 ----------------------------------------------------------------


def _clock(value: dict[str, Any] | None) -> time | None:
    if not value or value.get("hour") is None:
        return None
    hour, minute = int(value["hour"]), int(value.get("minute") or 0)
    if hour >= 24:
        hour, minute = 23, 59
    return time(hour, minute)


def _resolve_year(month: int, day: int, ctx: Context) -> int:
    years = sorted(
        {
            y
            for y in (
                ctx.period_start.year if ctx.period_start else None,
                ctx.period_end.year if ctx.period_end else None,
                ctx.observed.year,
                ctx.observed.year + 1,
            )
            if y
        }
    )
    low = (ctx.period_start - EARLY_GRACE) if ctx.period_start else None
    for year in years:
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if (low is None or candidate >= low) and (
            ctx.period_end is None or candidate <= ctx.period_end
        ):
            return year
    return ctx.period_end.year if ctx.period_end else ctx.observed.year


def _point_dt(
    point: dict[str, Any] | None, ctx: Context, *, end: bool
) -> tuple[datetime | None, bool]:
    """回傳 (datetime, 是否缺時刻)。"""
    if not point or point.get("month") is None or point.get("day") is None:
        return None, False
    month, day = int(point["month"]), int(point["day"])
    year = int(point["year"]) if point.get("year") else _resolve_year(month, day, ctx)
    clock = _clock(point)
    missing = clock is None
    if clock is None:
        clock = time(23, 59) if end else time(0, 0)
    try:
        return datetime.combine(date(year, month, day), clock, TAIPEI), missing
    except ValueError as exc:
        raise Rejected(f"日期不存在：{year}/{month}/{day}") from exc


def _months(ctx: Context) -> list[tuple[int, int]]:
    begin = (ctx.observed - KEEP_PAST).date().replace(day=1)
    if ctx.period_start and ctx.period_start > begin:
        begin = ctx.period_start.replace(day=1)
    stop = ctx.period_end or (ctx.observed.date() + timedelta(days=62))
    out: list[tuple[int, int]] = []
    year, month = begin.year, begin.month
    while (year, month) <= (stop.year, stop.month) and len(out) < MAX_MONTHS:
        out.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def _nth_weekday(year: int, month: int, nth: int, weekday: int) -> date | None:
    first = date(year, month, 1)
    offset = (weekday - 1 - first.weekday()) % 7
    day = 1 + offset + (nth - 1) * 7
    return date(year, month, day) if day <= calendar.monthrange(year, month)[1] else None


def _shift(day: date, policy: str) -> tuple[date, str]:
    if policy == "next_business_day":
        moved = next_business_day(day)
    elif policy == "previous_business_day":
        moved = previous_business_day(day)
    else:
        return day, ""
    if moved == day:
        return day, ""
    word = "順延" if moved > day else "提前"
    return moved, (
        f"{day.month}/{day.day}（週{_WEEKDAY_NAMES[day.weekday()]}）為放假日，"
        f"依條款{word}至 {moved.month}/{moved.day}"
    )


@dataclass
class Draft:
    start: datetime | None
    end: datetime | None
    kind: str
    origin: str
    note: str = ""
    shifted: bool = False
    precision: str = "minute"


def _monthly_points(rule: dict[str, Any]) -> bool:
    """「每月 1日早上9點至每月 5日下午6點止」：有日沒有月，是每月重複的區間。"""
    start, end = rule.get("start") or {}, rule.get("end") or {}
    has_day = start.get("day") is not None or end.get("day") is not None
    return has_day and start.get("month") is None and end.get("month") is None


def _monthly_range(rule: dict[str, Any], ctx: Context, policy: str) -> list[Draft]:
    start, end = rule.get("start") or {}, rule.get("end") or {}
    drafts: list[Draft] = []
    for year, month in _months(ctx):
        last = calendar.monthrange(year, month)[1]
        open_day = start.get("day")
        if open_day is None or open_day > last:
            continue
        moved, note = _shift(date(year, month, open_day), policy)
        open_clock = _clock(start)
        begin = datetime.combine(moved, open_clock or time(0, 0), TAIPEI)
        finish = None
        if end.get("day") is not None:
            close_day = min(int(end["day"]), last)
            close = date(year, month, close_day)
            if close < moved:
                continue
            finish = datetime.combine(close, _clock(end) or time(23, 59), TAIPEI)
        elif rule.get("end_of_month"):
            finish = datetime.combine(date(year, month, last), time(23, 59), TAIPEI)
        drafts.append(
            Draft(
                begin,
                finish,
                "range" if finish else "opens_at",
                "rule",
                note,
                bool(note),
                "minute" if open_clock else "day",
            )
        )
    return drafts


def _expand(rule: dict[str, Any], ctx: Context) -> list[Draft]:
    kind = rule["kind"]
    policy = rule.get("holiday") or "none"
    open_at = _clock(rule.get("open_time")) or time(0, 0)
    close_at = _clock(rule.get("close_time"))
    drafts: list[Draft] = []

    def at(day: date, origin: str, missing_clock: bool) -> Draft:
        moved, note = _shift(day, policy)
        start = datetime.combine(moved, open_at, TAIPEI)
        # 「至月底」優先於當日截止時刻：LLM 常把月底的 23:59 也填進 close_time
        if rule.get("end_of_month"):
            last = calendar.monthrange(moved.year, moved.month)[1]
            end = datetime.combine(moved.replace(day=last), close_at or time(23, 59), TAIPEI)
        elif close_at:
            end = datetime.combine(moved, close_at, TAIPEI)
        else:
            end = None
        return Draft(
            start,
            end,
            "range" if end else "opens_at",
            origin,
            note,
            bool(note),
            "day" if missing_clock else "minute",
        )

    missing_open = _clock(rule.get("open_time")) is None
    if kind == "monthly":
        for year, month in _months(ctx):
            for day in sorted(set(rule.get("days_of_month") or [])):
                if 1 <= day <= calendar.monthrange(year, month)[1]:
                    drafts.append(at(date(year, month, day), "rule", missing_open))
    elif kind == "monthly_nth_weekday":
        for year, month in _months(ctx):
            day_ = _nth_weekday(year, month, int(rule["nth"]), int(rule["weekday"]))
            if day_:
                drafts.append(at(day_, "rule", missing_open))
    elif kind == "enumerated":
        for point in rule.get("dates") or []:
            if point.get("hour") is not None:
                open_at = _clock(point) or open_at
                missing_open = False
            stamp, _ = _point_dt(point, ctx, end=False)
            if stamp:
                drafts.append(at(stamp.date(), "enumerated", missing_open))
    elif kind in ("range", "opens_at", "deadline") and _monthly_points(rule):
        drafts.extend(_monthly_range(rule, ctx, policy))
    elif kind in ("range", "opens_at", "deadline"):
        start, start_missing = _point_dt(rule.get("start"), ctx, end=False)
        end, _ = _point_dt(rule.get("end"), ctx, end=True)
        note = ""
        if start is not None:
            moved, note = _shift(start.date(), policy)
            start = datetime.combine(moved, start.timetz().replace(tzinfo=None), TAIPEI)
            if end is None and close_at:
                end = datetime.combine(moved, close_at, TAIPEI)
            elif end is None and rule.get("end_of_month"):
                last = calendar.monthrange(moved.year, moved.month)[1]
                end = datetime.combine(moved.replace(day=last), time(23, 59), TAIPEI)
        if start is None and end is None:
            raise Rejected("沒有可用的日期")
        if start and end and end < start:
            raise Rejected("截止早於開始")
        slot_kind = "deadline" if start is None else ("range" if end else "opens_at")
        drafts.append(
            Draft(
                start,
                end,
                slot_kind,
                "explicit",
                note,
                bool(note),
                "day" if start_missing else "minute",
            )
        )
    return drafts


# ---- 組裝 ----------------------------------------------------------------


def _claim(raw: dict[str, Any] | None, allowed: tuple[str, ...], ctx: Context) -> tuple[str, str]:
    if not raw:
        return "unknown", ""
    value, quote = raw.get("value", ""), raw.get("quote", "")
    if value in allowed and quote and ctx.quote_ok(quote):
        return value, quote
    return "unknown", ""


@dataclass
class Result:
    registration: Registration
    registration_quote: str
    subs: list[SubActivity]
    review: list[str]


def build(output: dict[str, Any], ctx: Context) -> Result:
    review: list[str] = []
    reg_raw = output.get("registration") or {}
    reg_value, reg_quote = _claim(reg_raw, ("required", "not_required"), ctx)
    subs: list[SubActivity] = []
    for raw in output.get("subs") or []:
        sub_review: list[str] = []
        drafts: list[tuple[Draft, str]] = []
        rules: list[str] = []
        month_end_close = False
        for rule in raw.get("rules") or []:
            quote = rule.get("quote", "")
            if rule.get("is_example"):
                continue
            if not ctx.quote_ok(quote):
                sub_review.append(f"引句不在原文中：{quote[:40]}")
                continue
            rules.append(quote)
            if rule["kind"] == "other":
                sub_review.append("登錄時間為相對規則（例如消費後幾日內），沒有固定時點")
                continue
            if (
                rule["kind"] in ("range", "opens_at", "deadline")
                and not rule.get("start")
                and not rule.get("end")
            ):
                # 「須於消費當月月底前完成登錄」這類沒有具體日期的截止：
                # 套到同一子活動每月開放的時點上，當作該月的截止。
                month_end_close = month_end_close or bool(rule.get("end_of_month"))
                continue
            try:
                _check_rule(rule)
                drafts.extend((draft, quote) for draft in _expand(rule, ctx))
            except Rejected as exc:
                sub_review.append(f"規則無法驗證（{exc}）：{quote[:40]}")
            except MissingCalendar as exc:
                sub_review.append(f"無法套用假日順延：{exc}")
        if month_end_close:
            for draft, _ in drafts:
                if draft.start and draft.end is None and draft.origin in ("rule", "enumerated"):
                    last = calendar.monthrange(draft.start.year, draft.start.month)[1]
                    draft.end = datetime.combine(
                        draft.start.date().replace(day=last), time(23, 59), TAIPEI
                    )
                    draft.kind = "range"
        slots = _finalize(drafts, ctx, sub_review)
        reregister, rereg_quote = _claim(raw.get("reregister"), ("each_period", "once"), ctx)
        order, order_quote = _claim(
            raw.get("order"), ("register_before_spend", "retroactive_ok"), ctx
        )
        quota_raw = raw.get("quota") or {}
        seats, quota_quote = quota_raw.get("seats"), quota_raw.get("quote", "")
        if seats is not None and not (
            quota_quote and ctx.quote_ok(quota_quote) and _seats_supported(seats, quota_quote)
        ):
            seats, quota_quote = None, ""
        name = (raw.get("name") or "").strip()
        if name and not ctx.quote_ok(name):
            sub_review.append(f"子活動名稱不在原文中：{name[:30]}")
            name = ""
        subs.append(
            SubActivity(
                name=name,
                slots=slots,
                rules=rules,
                reregister=_as_rereg(reregister),
                reregister_quote=rereg_quote,
                order=_as_order(order),
                order_quote=order_quote,
                quota=seats,
                quota_quote=quota_quote,
                review=sub_review,
            )
        )
    subs = _merge_same_name(subs)
    has_slots = any(sub.slots for sub in subs)
    registration: Registration
    if has_slots or reg_value == "required":
        registration = "required"
    elif reg_value == "not_required":
        registration = "not_required"
    else:
        registration = "unknown"
    if registration == "required" and not has_slots and not any(sub.rules for sub in subs):
        review.append("需登錄但官方未寫登錄時點")
    return Result(registration, reg_quote, subs, review)


def _merge_same_name(subs: list[SubActivity]) -> list[SubActivity]:
    """LLM 偶爾把同一個子活動輸出兩次（同名），合併時點與規則、去重。"""
    merged: dict[str, SubActivity] = {}
    out: list[SubActivity] = []
    for sub in subs:
        if sub.name not in merged:
            out.append(sub)
            merged[sub.name] = sub
            continue
        base = merged[sub.name]
        seen = {(s.start, s.end) for s in base.slots}
        base.slots.extend(s for s in sub.slots if (s.start, s.end) not in seen)
        base.slots.sort(key=lambda s: s.start or s.end or datetime.min.replace(tzinfo=TAIPEI))
        base.rules.extend(r for r in sub.rules if r not in base.rules)
        base.review.extend(r for r in sub.review if r not in base.review)
        base.quota = base.quota or sub.quota
        if base.reregister == "unknown":
            base.reregister, base.reregister_quote = sub.reregister, sub.reregister_quote
        if base.order == "unknown":
            base.order, base.order_quote = sub.order, sub.order_quote
    return out


def _seats_supported(seats: int, quote: str) -> bool:
    return seats in {int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", normalize(quote))}


def _as_rereg(value: str) -> Reregister:
    return "each_period" if value == "each_period" else "once" if value == "once" else "unknown"


def _as_order(value: str) -> Order:
    if value == "register_before_spend":
        return "register_before_spend"
    return "retroactive_ok" if value == "retroactive_ok" else "unknown"


def _month(draft: Draft) -> tuple[int, int] | None:
    return (draft.start.year, draft.start.month) if draft.start else None


def _reconcile(drafts: list[tuple[Draft, str]]) -> list[tuple[Draft, str]]:
    """條款列舉的日期與每月規則推算的時點對帳。

    同月份兩者都有時：日期一致就保留規則的時點（它帶時刻），並註明與列舉一致；
    日期不同以列舉為準，沿用規則的時刻。列舉本身沒寫時刻、又沒有規則可借時，
    維持「原文無時刻」。
    """
    rules = {_month(d): (d, q) for d, q in drafts if d.origin == "rule" and _month(d)}
    replaced: set[tuple[int, int] | None] = set()
    merged: list[tuple[Draft, str]] = []
    for draft, quote in drafts:
        if draft.origin != "enumerated" or draft.start is None:
            continue
        month = _month(draft)
        pair = rules.get(month)
        if pair is None:
            merged.append((draft, quote))
            continue
        rule_draft, rule_quote = pair
        assert rule_draft.start is not None
        replaced.add(month)
        if rule_draft.start.date() == draft.start.date():
            note = "；".join(filter(None, [rule_draft.note, "與條款列舉日期一致"]))
            merged.append(
                (
                    Draft(
                        rule_draft.start,
                        rule_draft.end,
                        rule_draft.kind,
                        "rule",
                        note,
                        rule_draft.shifted,
                        rule_draft.precision,
                    ),
                    rule_quote,
                )
            )
        else:
            start = datetime.combine(draft.start.date(), rule_draft.start.timetz())
            end = None
            if rule_draft.end is not None:
                end = start + (rule_draft.end - rule_draft.start)
            note = (
                f"條款列舉 {draft.start.month}/{draft.start.day}，與規則推算的 "
                f"{rule_draft.start.month}/{rule_draft.start.day} 不同，以列舉為準"
            )
            merged.append(
                (
                    Draft(
                        start,
                        end,
                        "range" if end else "opens_at",
                        "enumerated",
                        note,
                        False,
                        rule_draft.precision,
                    ),
                    quote,
                )
            )
    merged.extend(
        (d, q)
        for d, q in drafts
        if d.origin not in ("rule", "enumerated")
        or (d.origin == "rule" and _month(d) not in replaced)
    )
    return merged


def _finalize(drafts: list[tuple[Draft, str]], ctx: Context, review: list[str]) -> list[Slot]:
    drafts = _reconcile(drafts)
    # 同一開放時刻同時有「只寫開放」與「寫明起訖」兩種來源時，保留較完整的那筆
    bounded = {d.start for d, _ in drafts if d.start and d.end}
    drafts = [(d, q) for d, q in drafts if d.end is not None or d.start not in bounded]
    # 同理，「只寫截止」與「寫明起訖」截止相同時，截止已含在範圍內
    closes = {d.end for d, _ in drafts if d.start and d.end}
    drafts = [(d, q) for d, q in drafts if d.start is not None or d.end not in closes]
    floor = ctx.observed - KEEP_PAST
    low = (ctx.period_start - EARLY_GRACE) if ctx.period_start else None
    seen: set[tuple[datetime | None, datetime | None]] = set()
    slots: list[Slot] = []
    for draft, quote in sorted(drafts, key=lambda item: item[0].start or item[0].end or floor):
        anchor = draft.start or draft.end
        assert anchor is not None
        if ctx.period_end and draft.start and draft.start.date() > ctx.period_end:
            if draft.origin != "rule":
                review.append(
                    f"登錄時點 {draft.start:%Y/%m/%d} 晚於活動結束日 {ctx.period_end:%Y/%m/%d}"
                )
            continue
        if low and anchor.date() < low:
            review.append(f"登錄時點 {anchor:%Y/%m/%d} 遠早於活動開始日")
            continue
        if (draft.end or draft.start or anchor) < floor:
            continue
        key = (draft.start, draft.end)
        if key in seen:
            continue
        seen.add(key)
        slots.append(
            Slot(
                start=draft.start,
                end=draft.end,
                kind=draft.kind,  # type: ignore[arg-type]
                origin=draft.origin,  # type: ignore[arg-type]
                precision=draft.precision,  # type: ignore[arg-type]
                holiday_shifted=draft.shifted,
                note=draft.note,
                evidence=Evidence(quote=quote, url=ctx.url),
            )
        )
    return slots
