"""網站資料輸出：行事曆事件必須與時間軸一致（曾因變數被覆寫而輸出 0 個事件）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from radar.emit import write
from radar.models import Activity, Evidence, Slot, SourceHealth, SubActivity

TPE = ZoneInfo("Asia/Taipei")


def test_ics_has_one_event_per_minute_slot(tmp_path: Path) -> None:
    generated = datetime(2026, 11, 5, 9, 0, tzinfo=TPE)
    slot = Slot(
        start=datetime(2026, 11, 6, 10, 0, tzinfo=TPE), end=None, kind="opens_at",
        origin="rule", evidence=Evidence(quote="每月6號10:00開放登錄", url="https://x.example/a"),
    )
    activity = Activity(
        id="ctbc-1", bank_id="ctbc", bank_name="中國信託", title="Uber Eats",
        url="https://x.example/a", registration="required",
        subs=[SubActivity(slots=[slot])], observed_at=generated,
    )
    health = SourceHealth(bank_id="ctbc", bank_name="中國信託", status="ok", entry_url="https://x")
    write({"ctbc": [activity], "obank": []}, [health], generated, out=tmp_path)
    ics = (tmp_path / "calendar" / "registration.ics").read_text(encoding="utf-8")
    assert ics.count("BEGIN:VEVENT") == 1
    assert "DTSTART:20261106T020000Z" in ics
