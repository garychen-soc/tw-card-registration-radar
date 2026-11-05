"""局部讀取失敗時沿用上一輪活動（審查 🔴-1：台新分類 403、國泰明細逾時讓有效活動消失）。"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from radar import pipeline
from radar.models import Activity, SourceHealth, Stub
from radar.pipeline import Fetched, _carry_missing

TAIPEI = ZoneInfo("Asia/Taipei")
OBSERVED = datetime(2026, 11, 6, 8, 20, tzinfo=TAIPEI)


def act(aid: str, url: str, *, digest: str = "", end: date | None = date(2026, 12, 31)) -> Activity:
    return Activity(
        id=aid, bank_id="taishin", bank_name="台新銀行", title=aid, url=url,
        period_end=end, content_hash=digest or aid, observed_at=OBSERVED,
    )


def fetched(*, failed_urls: set[str] | None = None, category_failed: bool = False) -> Fetched:
    health = SourceHealth(bank_id="taishin", bank_name="台新銀行", status="ok", entry_url="https://x")
    return Fetched(
        "taishin", health, failed_urls=failed_urls or set(), category_failed=category_failed
    )


def test_complete_run_carries_nothing() -> None:
    built = [act("a", "https://x/a")]
    old = [act("a", "https://x/a"), act("gone", "https://x/gone")]
    out, carried = _carry_missing(built, old, fetched())
    assert carried == 0
    assert [a.id for a in out] == ["a"]  # 官方下架的活動照常移除


def test_detail_failure_carries_only_failed_urls() -> None:
    built = [act("a", "https://x/a")]
    old = [act("a", "https://x/a"), act("timeout", "https://x/t"), act("gone", "https://x/gone")]
    out, carried = _carry_missing(built, old, fetched(failed_urls={"https://x/t"}))
    assert carried == 1
    assert {a.id: a.stale for a in out} == {"a": False, "timeout": True}


def test_category_failure_carries_all_missing() -> None:
    built = [act("a", "https://x/a")]
    old = [act("a", "https://x/a"), act("b", "https://x/b"), act("c", "https://x/c")]
    out, carried = _carry_missing(built, old, fetched(category_failed=True))
    assert carried == 2
    assert sorted(a.id for a in out if a.stale) == ["b", "c"]


def test_same_content_under_new_id_is_not_duplicated() -> None:
    # 主標題換了（id 變）但條款相同：本輪已有，不重複沿用
    built = [act("new-id", "https://x/n", digest="same")]
    old = [act("old-id", "https://x/o", digest="same")]
    out, carried = _carry_missing(built, old, fetched(category_failed=True))
    assert carried == 0
    assert len(out) == 1


def test_run_marks_partial_and_drops_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """整輪：分類失敗時沿用仍有效的舊活動、過期的不沿用，來源標 partial。"""
    stub = Stub(bank_id="taishin", key="k1", title="新活動", url="https://x/new",
                period_end=date(2026, 12, 31))
    item = fetched(category_failed=True)
    item.health.message = "分類失敗：I 其他優惠（HTTP 403）"
    item.health.status = "partial"
    item.docs = [(stub, "刷卡回饋 3%，無須其他手續。" * 20)]
    old = [
        act("still-valid", "https://x/v"),
        act("expired", "https://x/e", end=date(2026, 10, 1)),
    ]
    monkeypatch.setattr(pipeline, "load_docs", lambda bank_id: item)
    monkeypatch.setattr(pipeline, "previous", lambda bank_id: old)

    result = pipeline.run(["taishin"], reuse_docs=True, observed=OBSERVED)

    rows = {a.title: a.stale for a in result.activities["taishin"]}
    assert rows == {"新活動": False, "still-valid": True}
    health = result.health[0]
    assert health.status == "partial"
    assert health.carried == 1
    assert "沿用上一輪 1 筆" in health.message
    assert result.llm_calls == 0
