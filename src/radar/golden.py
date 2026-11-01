"""以 golden 集（官方原文人工核對的登錄時點）評分目前的 state。

比對單位是「時點」：開放時刻到分鐘、截止時刻到分鐘（兩邊都沒有截止也算相符）。
精確率＝產出的時點中正確的比例 —— 這是發布門檻（≥98%），因為時間軸上
一個錯的時點會讓使用者白跑一趟；召回率衡量漏了多少。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from .models import Activity
from .pipeline import STATE, previous

GOLDEN = Path(__file__).resolve().parents[2] / "golden" / "golden.yaml"
Key = tuple[str | None, str | None]


def _minute(value: Any) -> str | None:
    if value in (None, ""):
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return moment.strftime("%Y-%m-%dT%H:%M")


def _in_window(key: Key, start: date, end: date) -> bool:
    anchor = key[0] or key[1]
    return anchor is not None and start.isoformat() <= anchor[:10] <= end.isoformat()


def _find(entry: dict[str, Any], pool: list[Activity]) -> Activity | None:
    url = str(entry["url"]).rstrip("/")
    for activity in pool:
        if activity.url.rstrip("/") == url:
            return activity
    title = str(entry.get("title", ""))
    for activity in pool:
        if title and (activity.title == title or title in activity.aliases):
            return activity
    return None


def score(window: tuple[date, date] = (date(2026, 11, 1), date(2026, 12, 31))) -> dict[str, Any]:
    entries: list[dict[str, Any]] = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    banks = {str(e["bank"]) for e in entries}
    pools = {
        bank: previous(bank) for bank in banks if (STATE / "activities" / f"{bank}.json").exists()
    }
    tp = fp = fn = 0
    missing: list[str] = []
    diffs: list[dict[str, Any]] = []
    for entry in entries:
        activity = _find(entry, pools.get(str(entry["bank"]), []))
        expected: set[Key] = {
            (_minute(slot.get("start")), _minute(slot.get("end")))
            for sub in entry.get("subs") or []
            for slot in sub.get("slots") or []
        }
        expected = {k for k in expected if _in_window(k, *window)}
        if activity is None:
            if expected:
                missing.append(str(entry["id"]))
                fn += len(expected)
            continue
        produced: set[Key] = {
            (_minute(slot.start), _minute(slot.end)) for sub in activity.subs for slot in sub.slots
        }
        produced = {k for k in produced if _in_window(k, *window)}
        hit = expected & produced
        tp += len(hit)
        fp += len(produced - expected)
        fn += len(expected - produced)
        if produced != expected:
            diffs.append(
                {
                    "id": entry["id"],
                    "wrong": sorted(produced - expected, key=str),
                    "missed": sorted(expected - produced, key=str),
                }
            )
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {
        "entries": len(entries),
        "matched_slots": tp,
        "wrong_slots": fp,
        "missed_slots": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "not_found": missing,
        "diffs": diffs,
    }
