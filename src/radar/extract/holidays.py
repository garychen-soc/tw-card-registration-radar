"""行政院人事行政總處「政府行政機關辦公日曆表」。

銀行條款的「遇假日順延至次一營業日」以此為準：``is_holiday=1`` 的日子
（含週末、國定假日、補假）都不是營業日。資料檔在 ``data/holidays/<西元年>.csv``，
來源 data.gov.tw 資料集 14718，每年 6 月左右公布隔年。

缺年份時不猜 —— ``is_business_day`` 拋 ``MissingCalendar``，呼叫端把該時點
轉人工確認，而不是假設「只有週末放假」。
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from functools import cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "holidays"


class MissingCalendar(LookupError):
    pass


@cache
def _year(year: int) -> dict[date, bool]:
    path = DATA_DIR / f"{year}.csv"
    if not path.exists():
        raise MissingCalendar(f"缺少 {year} 年辦公日曆（{path}）")
    with path.open(encoding="utf-8") as handle:
        return {
            date.fromisoformat(row["date"]): row["is_holiday"] == "1"
            for row in csv.DictReader(handle)
        }


def is_business_day(day: date) -> bool:
    calendar = _year(day.year)
    if day not in calendar:
        raise MissingCalendar(f"{day} 不在辦公日曆中")
    return not calendar[day]


def next_business_day(day: date) -> date:
    """``day`` 本身是營業日就回傳它，否則往後找第一個營業日。"""
    current = day
    for _ in range(40):
        if is_business_day(current):
            return current
        current += timedelta(days=1)
    raise MissingCalendar(f"{day} 起 40 天內找不到營業日")


def previous_business_day(day: date) -> date:
    current = day
    for _ in range(40):
        if is_business_day(current):
            return current
        current -= timedelta(days=1)
    raise MissingCalendar(f"{day} 起往前 40 天內找不到營業日")
