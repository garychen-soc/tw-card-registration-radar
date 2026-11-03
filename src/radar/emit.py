"""從 state 產生網站資料。

``web/public/data/index.json``
    首屏：來源健康、統計、未來 120 天（含過去 2 天）的登錄時點。前端用它畫
    「今日／明日／7 天」與行事曆。
``web/public/data/banks/<id>.json``
    各銀行完整活動，瀏覽活動目錄時才載入。
``web/public/calendar/registration.ics``
    可訂閱的登錄提醒（只含通過驗證、精確到分鐘的開放時點）。

只放事實：「今天是否進行中」這類隨時間變動的狀態由前端計算。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Activity, SourceHealth

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web" / "public"
SCHEMA_VERSION = 2
HORIZON = timedelta(days=120)
LOOKBACK = timedelta(days=2)
SITE_URL = "https://garychen-soc.github.io/tw-card-registration-radar/"


def timeline(activities: list[Activity], generated: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for activity in activities:
        for index, sub in enumerate(activity.subs):
            for slot in sub.slots:
                anchor = slot.start or slot.end
                if anchor is None:
                    continue
                last = slot.end or slot.start or anchor
                if last < generated - LOOKBACK or anchor > generated + HORIZON:
                    continue
                rows.append(
                    {
                        "aid": activity.id,
                        "bank_id": activity.bank_id,
                        "bank": activity.bank_name,
                        "title": activity.title,
                        "sub": sub.name,
                        "sub_index": index,
                        "start": slot.start.isoformat() if slot.start else None,
                        "end": slot.end.isoformat() if slot.end else None,
                        "kind": slot.kind,
                        "precision": slot.precision,
                        "origin": slot.origin,
                        "shifted": slot.holiday_shifted,
                        "note": slot.note,
                        "quote": slot.evidence.quote,
                        "url": activity.url,
                        "portal": activity.portal_url,
                        "portal_hint": activity.portal_hint,
                        "quota": sub.quota,
                        "reregister": sub.reregister,
                        "order": sub.order,
                        "stale": activity.stale,
                    }
                )
    rows.sort(key=lambda r: (r["start"] or r["end"], r["bank_id"], r["title"]))
    # 同一銀行同名活動的不同分頁網址（聯邦 ?p=）常給出相同時點：時間軸只留一列
    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = (row["bank_id"], row["title"], row["sub"], row["start"], row["end"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def counts(activities: list[Activity]) -> dict[str, int]:
    required = [a for a in activities if a.registration == "required"]
    with_slots = [a for a in required if any(s.slots for s in a.subs)]
    return {
        "activities": len(activities),
        "registration_required": len(required),
        "with_slots": len(with_slots),
        "without_slots": len(required) - len(with_slots),
        "review": sum(1 for a in activities if a.review or any(s.review for s in a.subs)),
        "stale": sum(1 for a in activities if a.stale),
    }


def write(
    activities: dict[str, list[Activity]],
    health: list[SourceHealth],
    generated: datetime,
    *,
    out: Path = WEB,
) -> dict[str, int]:
    every = [a for items in activities.values() for a in items]
    data_dir = out / "data"
    (data_dir / "banks").mkdir(parents=True, exist_ok=True)
    summary = counts(every)
    index = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated.isoformat(),
        "timezone": "Asia/Taipei",
        "counts": summary,
        "sources": [h.model_dump(mode="json") for h in health],
        "timeline": timeline(every, generated),
    }
    _dump(data_dir / "index.json", index)
    for bank_id, items in activities.items():
        rows = [a.model_dump(mode="json", exclude={"content_hash"}) for a in items]
        rows.sort(key=lambda r: (r["period_end"] or "9999", r["title"]))
        _dump(data_dir / "banks" / f"{bank_id}.json", {"bank_id": bank_id, "activities": rows})
    (out / "calendar").mkdir(parents=True, exist_ok=True)
    (out / "calendar" / "registration.ics").write_text(
        ics(index["timeline"], generated), encoding="utf-8", newline=""
    )
    return summary


def _dump(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )


# ---- iCalendar ----------------------------------------------------------------


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545：每行不超過 75 octets，續行以空白開頭。"""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts: list[str] = []
    current = b""
    for char in line:
        encoded = char.encode("utf-8")
        if len(current) + len(encoded) > (75 if not parts else 74):
            parts.append(current.decode("utf-8"))
            current = b""
        current += encoded
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def _utc(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def ics(rows: list[dict[str, Any]], generated: datetime) -> str:
    stamp = generated.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//tw-card-registration-radar//v2//ZH-TW",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:刷卡登錄雷達",
        "X-WR-TIMEZONE:Asia/Taipei",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for row in rows:
        if not row["start"] or row["precision"] != "minute" or row["stale"]:
            continue
        title = row["title"] + (f"｜{row['sub']}" if row["sub"] else "")
        description = "\n".join(
            filter(
                None,
                [
                    f"原文：{row['quote']}",
                    row["note"],
                    f"截止：{row['end'][:16].replace('T', ' ')}" if row["end"] else "截止：未公告",
                    f"名額：{row['quota']}" if row["quota"] else "",
                    f"活動頁：{row['url']}",
                    f"登錄：{row['portal']}",
                ],
            )
        )
        uid = f"{row['aid']}-{row['sub_index']}-{_utc(row['start'])}@tw-card-registration-radar"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_utc(row['start'])}",
            "DURATION:PT15M",
            "SUMMARY:" + _escape("登錄｜" + row["bank"] + "｜" + title),
            f"DESCRIPTION:{_escape(description)}",
            f"URL:{row['url']}",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "TRIGGER:-PT10M",
            f"DESCRIPTION:{_escape('即將開放登錄：' + title)}",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
