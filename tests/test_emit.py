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


def _uids(ics: str) -> list[str]:
    return [line for line in ics.splitlines() if line.startswith("UID:")]


def test_duplicate_rows_keep_same_uid_regardless_of_input_order(tmp_path: Path) -> None:
    """同名活動不同網址給出相同時點時只留一列；留哪列不能隨輸入順序變（玉山曾每輪換 UID）。"""
    generated = datetime(2026, 11, 5, 9, 0, tzinfo=TPE)
    slot = Slot(
        start=datetime(2026, 11, 17, 15, 0, tzinfo=TPE), end=None, kind="opens_at",
        origin="explicit", evidence=Evidence(quote="11/17 15:00 開放登錄", url="https://x/a"),
    )
    a, b = (
        Activity(
            id=aid, bank_id="ubot", bank_name="聯邦銀行", title="同名活動", url=f"https://x/{aid}",
            registration="required", subs=[SubActivity(slots=[slot])], observed_at=generated,
        )
        for aid in ("ubot-aaa", "ubot-zzz")
    )
    health = SourceHealth(bank_id="ubot", bank_name="聯邦銀行", status="ok", entry_url="x")
    write({"ubot": [a, b]}, [health], generated, out=tmp_path / "1")
    write({"ubot": [b, a]}, [health], generated, out=tmp_path / "2")
    first = (tmp_path / "1" / "calendar" / "registration.ics").read_text(encoding="utf-8")
    second = (tmp_path / "2" / "calendar" / "registration.ics").read_text(encoding="utf-8")
    assert _uids(first) == _uids(second)
    assert len(_uids(first)) == 1


def test_stale_activity_stays_in_ics_with_same_uid(tmp_path: Path) -> None:
    """來源一時讀取失敗沿用上一輪時，訂閱者的事件不能消失或換 UID（否則會被刪掉重建）。"""
    generated = datetime(2026, 11, 5, 9, 0, tzinfo=TPE)
    slot = Slot(
        start=datetime(2026, 11, 6, 10, 0, tzinfo=TPE), end=None, kind="opens_at",
        origin="rule", evidence=Evidence(quote="每月6號10:00開放登錄", url="https://x.example/a"),
    )
    fresh = Activity(
        id="ctbc-1", bank_id="ctbc", bank_name="中國信託", title="Uber Eats",
        url="https://x.example/a", registration="required",
        subs=[SubActivity(slots=[slot])], observed_at=generated,
    )
    health = SourceHealth(bank_id="ctbc", bank_name="中國信託", status="partial", entry_url="x")

    write({"ctbc": [fresh]}, [health], generated, out=tmp_path / "a")
    write({"ctbc": [fresh.model_copy(update={"stale": True})]}, [health], generated,
          out=tmp_path / "b")
    before = (tmp_path / "a" / "calendar" / "registration.ics").read_text(encoding="utf-8")
    after = (tmp_path / "b" / "calendar" / "registration.ics").read_text(encoding="utf-8")

    assert after.count("BEGIN:VEVENT") == 1
    assert _uids(after) == _uids(before)
    assert "讀取失敗" in after.replace("\r\n ", "")
    assert "讀取失敗" not in before.replace("\r\n ", "")
