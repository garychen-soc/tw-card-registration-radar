"""輸出層測試。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from radar.emit import (
    build_catalog,
    build_detail,
    build_ics,
    build_index,
    carry_forward,
    portals_of,
    prune,
    write_site,
)
from radar.models import (
    Alert,
    Campaign,
    Conditions,
    Evidence,
    Offer,
    Period,
    Portal,
    Quota,
    Recurrence,
    Registration,
    RegistrationWindow,
    SourceHealth,
    ThresholdTier,
    TimingContract,
)

NOW = datetime(2026, 8, 4, 2, 0, tzinfo=UTC)

RANGE_WINDOW = RegistrationWindow(
    kind="range",
    start=datetime.fromisoformat("2026-08-07T17:00:00+08:00"),
    end=datetime.fromisoformat("2026-08-20T23:59:00+08:00"),
    confidence=0.95,
    evidence=Evidence(text="登錄期間：2026/8/7 17:00~2026/8/20 23:59"),
)
OPENS_AT_WINDOW = RegistrationWindow(
    kind="opens_at",
    start=datetime.fromisoformat("2026-08-17T10:00:00+08:00"),
    confidence=0.8,
    evidence=Evidence(text="8/17 10:00 開放登錄，限量600名"),
)


def _offer(
    offer_id: str,
    *,
    window: RegistrationWindow | None = None,
    recurrence: Recurrence | None = None,
    quota: Quota | None = None,
    needs_review: bool = False,
) -> Offer:
    return Offer(
        id=offer_id,
        title="夏日網購回饋",
        period=Period(
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            confidence=0.9,
            evidence=Evidence(text="活動期間：2026/8/1~2026/8/31"),
        ),
        registration=Registration(
            required=True,
            windows=[window] if window else [],
            recurrence=recurrence or Recurrence(),
            portal=Portal(
                url="https://www.example.com/register",
                kind="bank_portal",
                hint="登入後找到對應活動",
            ),
            timing_contract=TimingContract(
                kind="registration_closes_early",
                last_chance_to_register=window.end if window and window.end else None,
                spend_days_left_after_registering=11,
                confidence=0.7,
            ),
            raw_text="登錄辦法：2026/8/7 17:00~2026/8/20 23:59",
        ),
        conditions=Conditions(
            threshold_tiers=[ThresholdTier(spend_twd=10000, reward_twd=500)],
            quota=quota or Quota(),
        ),
        needs_review=needs_review,
        review_codes=["registration_without_window"] if needs_review else [],
        review_reasons=["標記為需登錄，但抓不到任何登錄時點"] if needs_review else [],
    )


def _campaign(*offers: Offer) -> Campaign:
    return Campaign(
        id="demo-campaign",
        bank_id="demo",
        bank_name="示範銀行",
        title="夏日網購",
        source_url="https://www.example.com/promo/1",
        observed_at=NOW,
        offers=list(offers),
        terms_raw="活動辦法全文……" * 20,
        content_hash="abc123",
    )


def _health() -> list[SourceHealth]:
    return [
        SourceHealth(
            bank_id="demo",
            bank_name="示範銀行",
            requested_url="https://www.example.com/list",
            status="complete",
            campaign_count=1,
            offer_count=1,
        )
    ]


def _index(*offers: Offer) -> dict[str, object]:
    campaigns = [_campaign(*offers)]
    return build_index(
        campaigns,
        health=_health(),
        alerts=[],
        generated_at=NOW,
        portals=portals_of(campaigns),
    )


# ── prune ────────────────────────────────────────────────


def test_prune_drops_defaults_but_keeps_zero() -> None:
    """0 是有意義的值（例如「登錄截止後還有 0 天可消費」），不能被當成空值移除。"""
    cleaned = prune(
        {"a": None, "b": "", "c": [], "d": {}, "e": False, "f": 0, "g": "x", "h": 0.0}
    )
    assert cleaned == {"f": 0, "g": "x", "h": 0.0}


# ── index ────────────────────────────────────────────────


def test_index_contains_no_time_derived_state() -> None:
    """lifecycle / high_return 之類的欄位一律不進資料檔 —— 它們是「今天」的函數。"""
    serialised = json.dumps(_index(_offer("demo-1", window=RANGE_WINDOW)), ensure_ascii=False)
    for forbidden in ("lifecycle", "high_return", "featured", "is_active", "days_until"):
        assert forbidden not in serialised, forbidden


def test_index_agenda_only_carries_offers_with_windows() -> None:
    """首屏時間軸只需要有登錄時點的活動。實測聯邦 223 筆裡只有 31 筆有視窗。"""
    index = _index(
        _offer("demo-1", window=RANGE_WINDOW),
        _offer("demo-2"),
    )
    assert index["counts"]["offers"] == 2  # type: ignore[index]
    assert index["counts"]["with_window"] == 1  # type: ignore[index]
    assert [entry["id"] for entry in index["agenda"]] == ["demo-1"]  # type: ignore[index]


def test_index_agenda_excludes_condition_details() -> None:
    """條件細節留在 catalog，首屏不載。"""
    index = _index(_offer("demo-1", window=RANGE_WINDOW))
    entry = index["agenda"][0]  # type: ignore[index]
    assert "conditions" not in entry
    assert "threshold_tiers" not in json.dumps(entry, ensure_ascii=False)


def test_index_hoists_portal_to_source_level() -> None:
    """實測 223 筆活動共用同一個 portal，逐筆輸出是純粹的重複。"""
    index = _index(_offer("demo-1", window=RANGE_WINDOW), _offer("demo-2", window=OPENS_AT_WINDOW))
    assert index["sources"][0]["portal"]["kind"] == "bank_portal"  # type: ignore[index]
    for entry in index["agenda"]:  # type: ignore[index]
        assert "portal" not in entry


def test_index_excludes_raw_text_and_evidence() -> None:
    serialised = json.dumps(_index(_offer("demo-1", window=RANGE_WINDOW)), ensure_ascii=False)
    assert "terms_raw" not in serialised
    assert "活動辦法全文" not in serialised
    assert "evidence" not in serialised
    assert "content_hash" not in serialised


def test_index_keeps_alerts() -> None:
    campaigns = [_campaign(_offer("demo-1", window=RANGE_WINDOW))]
    index = build_index(
        campaigns,
        health=_health(),
        alerts=[
            Alert(type="detail_unreadable", bank_id="demo", bank_name="示範銀行", message="逾時")
        ],
        generated_at=NOW,
    )
    assert len(index["alerts"]) == 1


# ── catalog / detail ─────────────────────────────────────


def test_catalog_carries_full_conditions() -> None:
    catalog = build_catalog("demo", [_campaign(_offer("demo-1", window=RANGE_WINDOW))])
    offer = catalog["offers"][0]
    assert offer["conditions"]["threshold_tiers"][0]["spend_twd"] == 10000
    assert offer["registration"]["contract"]["kind"] == "registration_closes_early"


def test_catalog_omits_default_values() -> None:
    """絕大多數活動的資格旗標與卡別都是空的，逐筆輸出純粹浪費。"""
    catalog = build_catalog("demo", [_campaign(_offer("demo-1", window=RANGE_WINDOW))])
    eligibility = catalog["offers"][0]["conditions"].get("eligibility", {})
    assert "new_customer_only" not in eligibility
    assert "cards" not in eligibility


def test_catalog_filters_by_bank() -> None:
    assert build_catalog("other", [_campaign(_offer("demo-1"))])["offers"] == []


def test_detail_carries_evidence_but_not_the_whole_page() -> None:
    """展開一筆活動不該載入整頁原文。第一版這樣做，聯邦一家就 1.18MB。"""
    detail = build_detail(_campaign(_offer("demo-1", window=RANGE_WINDOW)))
    serialised = json.dumps(detail, ensure_ascii=False)
    assert "terms_raw" not in serialised
    assert "活動辦法全文" not in serialised

    offer = detail["offers"][0]
    assert offer["period_evidence"].startswith("活動期間")
    assert offer["window_evidence"][0].startswith("登錄期間")
    assert offer["registration_raw"].startswith("登錄辦法")


def test_detail_carries_human_readable_review_reasons() -> None:
    """代碼進資料檔，中文句子只在 detail 供展開時顯示。"""
    detail = build_detail(_campaign(_offer("demo-1", needs_review=True)))
    assert detail["offers"][0]["review_reasons"] == ["標記為需登錄，但抓不到任何登錄時點"]


# ── ics ──────────────────────────────────────────────────


def test_ics_folds_long_lines_at_75_octets() -> None:
    text = build_ics([_campaign(_offer("demo-1", window=RANGE_WINDOW))], now=NOW)
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, line


def test_ics_unfolds_back_to_original_content() -> None:
    unfolded = build_ics([_campaign(_offer("demo-1", window=RANGE_WINDOW))], now=NOW).replace(
        "\r\n ", ""
    )
    assert "SUMMARY:[開放登錄] 示範銀行｜夏日網購回饋" in unfolded
    assert "登錄至 2026-08-20 23:59" in unfolded


def test_ics_notes_unknown_end_instead_of_inventing_one() -> None:
    unfolded = build_ics([_campaign(_offer("demo-1", window=OPENS_AT_WINDOW))], now=NOW).replace(
        "\r\n ", ""
    )
    assert "官方未公告登錄截止時間" in unfolded
    # 事件仍需長度（15 分鐘），但那是呈現；資料層的 end 依然是 None
    assert "DTSTART:20260817T020000Z" in unfolded
    assert "DTEND:20260817T021500Z" in unfolded


def test_ics_uses_rrule_for_monthly_recurrence() -> None:
    unfolded = build_ics(
        [
            _campaign(
                _offer(
                    "demo-1",
                    window=OPENS_AT_WINDOW,
                    recurrence=Recurrence(kind="monthly", note="每月1日開放登錄"),
                )
            )
        ],
        now=NOW,
    ).replace("\r\n ", "")
    assert "RRULE:FREQ=MONTHLY;UNTIL=20260831T235900Z" in unfolded


def test_ics_gives_limited_quota_a_longer_lead() -> None:
    plain = build_ics([_campaign(_offer("demo-1", window=OPENS_AT_WINDOW))], now=NOW)
    limited = build_ics(
        [
            _campaign(
                _offer("demo-1", window=OPENS_AT_WINDOW, quota=Quota(limited=True, seats=600))
            )
        ],
        now=NOW,
    )
    assert "TRIGGER:-PT15M" in plain
    assert "TRIGGER:-PT30M" in limited
    assert "限量 600" in limited.replace("\r\n ", "")


def test_ics_omits_offers_needing_review() -> None:
    """讓使用者依據未確認的時間去搶登錄，比不提醒更糟。"""
    campaigns = [_campaign(_offer("demo-1", window=OPENS_AT_WINDOW, needs_review=True))]
    text = build_ics(campaigns, now=NOW)
    assert "BEGIN:VEVENT" not in text


def test_ics_deadline_window_is_labelled_as_deadline() -> None:
    deadline = RegistrationWindow(
        kind="deadline",
        end=datetime.fromisoformat("2026-08-31T23:59:00+08:00"),
        confidence=0.8,
    )
    unfolded = build_ics([_campaign(_offer("demo-1", window=deadline))], now=NOW).replace(
        "\r\n ", ""
    )
    assert "SUMMARY:[登錄截止]" in unfolded
    assert "此為登錄截止時間" in unfolded


# ── write_site ───────────────────────────────────────────


def test_write_site_emits_layers(tmp_path: Path) -> None:
    campaigns = [_campaign(_offer("demo-1", window=RANGE_WINDOW))]
    index = build_index(
        campaigns, health=_health(), alerts=[], generated_at=NOW, portals=portals_of(campaigns)
    )
    written = write_site(tmp_path, index, campaigns, now=NOW)

    assert {path.relative_to(tmp_path).as_posix() for path in written} == {
        "data/index.json",
        "data/catalog/demo.json",
        "data/detail/demo/demo-campaign.json",
        "calendar/registration.ics",
    }
    reloaded = json.loads((tmp_path / "data" / "index.json").read_text(encoding="utf-8"))
    assert reloaded["schema_version"] == 1
    assert reloaded["generated_at"] == NOW.isoformat()


def test_written_json_is_compact(tmp_path: Path) -> None:
    """機器讀的檔案不縮排。實測縮排讓聯邦的輸出多了 24%。"""
    campaigns = [_campaign(_offer("demo-1", window=RANGE_WINDOW))]
    index = build_index(campaigns, health=_health(), alerts=[], generated_at=NOW)
    write_site(tmp_path, index, campaigns, now=NOW)
    body = (tmp_path / "data" / "index.json").read_text(encoding="utf-8")
    assert '": ' not in body
    assert body.count("\n") == 1


# ── 來源失敗時沿用上一版 ──────────────────────────────────


def _previous_site(
    tmp: Path,
    bank_id: str,
    offers: list[dict[str, object]],
    generated_at: str = "2026-08-15T04:00:00+00:00",
) -> None:
    catalog = tmp / "data" / "catalog"
    catalog.mkdir(parents=True, exist_ok=True)
    (catalog / f"{bank_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bank_id": bank_id,
                "generated_at": generated_at,
                "offers": offers,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _blocked(bank_id: str = "sunny") -> SourceHealth:
    return SourceHealth(
        bank_id=bank_id,
        bank_name="陽信銀行",
        requested_url="https://www.sunnybank.com.tw/",
        status="blocked",
        campaign_count=0,
        offer_count=0,
    )


def test_blocked_source_carries_forward_marked_as_stale(tmp_path: Path) -> None:
    """來源被擋時沿用上一版並標記 —— 原本是整家歸零，使用者什麼都看不到。"""
    _previous_site(tmp_path, "sunny", [{"id": "sunny-1", "title": "刷卡享優惠"}])
    now = datetime(2026, 8, 16, 4, 0, tzinfo=UTC)
    carried, report = carry_forward(
        site_root=tmp_path,
        health=[_blocked()],
        previous_index={"generated_at": "2026-08-15T04:00:00+00:00"},
        now=now,
    )
    assert list(carried) == ["sunny"]
    assert carried["sunny"][0]["stale_since"] == "2026-08-15T04:00:00+00:00"
    assert report[0].offers == 1
    assert report[0].stale_hours == 24.0


def test_carry_forward_stops_after_the_age_limit(tmp_path: Path) -> None:
    """放太久就不再沿用 —— 活動清單超過兩週，裡面多半有已結束、已下架的，
    那時「有資料」比「沒資料」更誤導。"""
    _previous_site(
        tmp_path, "sunny", [{"id": "sunny-1"}], generated_at="2026-07-01T04:00:00+00:00"
    )
    carried, report = carry_forward(
        site_root=tmp_path,
        health=[_blocked()],
        previous_index={"generated_at": "2026-08-15T04:00:00+00:00"},
        now=datetime(2026, 8, 16, 4, 0, tzinfo=UTC),
    )
    assert carried == {} and report == []


def test_partial_sources_are_not_topped_up_from_the_previous_version(tmp_path: Path) -> None:
    """partial 代表大部分讀到了。混入舊資料會讓新舊兩批並存，
    使用者無從分辨哪一筆是今天的。"""
    _previous_site(tmp_path, "esun", [{"id": "esun-old", "title": "舊活動"}])
    health = SourceHealth(
        bank_id="esun",
        bank_name="玉山銀行",
        requested_url="https://www.esunbank.com/",
        status="partial",
        campaign_count=180,
        offer_count=250,
    )
    carried, _ = carry_forward(
        site_root=tmp_path,
        health=[health],
        previous_index={"generated_at": "2026-08-15T04:00:00+00:00"},
        now=datetime(2026, 8, 16, 4, 0, tzinfo=UTC),
    )
    assert carried == {}


def test_carried_offers_never_inflate_the_coverage_baseline(tmp_path: Path) -> None:
    """offer_count 必須一直是「這次真的讀到幾筆」—— 那是涵蓋率防護的基準。
    沿用的筆數放在獨立欄位，否則防護分不清「來源掛了」與「抓取退步」。"""
    _previous_site(tmp_path, "sunny", [{"id": "sunny-1"}, {"id": "sunny-2"}])
    carried, _ = carry_forward(
        site_root=tmp_path,
        health=[_blocked()],
        previous_index={"generated_at": "2026-08-15T04:00:00+00:00"},
        now=datetime(2026, 8, 16, 4, 0, tzinfo=UTC),
    )
    index = build_index(
        [], health=[_blocked()], alerts=[], generated_at=NOW, carried=carried
    )
    source = index["sources"][0]
    assert isinstance(source, dict)
    assert source["offer_count"] == 0
    assert source["carried_offer_count"] == 2
    counts = index["counts"]
    assert isinstance(counts, dict)
    assert counts["offers"] == 0


def test_catalog_without_a_recorded_time_is_not_carried_forward(tmp_path: Path) -> None:
    """不知道多舊的資料不該掛上一個看起來很新的時間。

    這個 bug 我原本寫出來了：``stale_since`` 取整份 index 的時間，而某家的
    catalog 可能是十天前留下的，會被標成「6 小時前」—— 正是本專案在反對的
    靜默過期。改成每家 catalog 記自己的時間，沒有記錄的就不沿用。
    """
    catalog = tmp_path / "data" / "catalog"
    catalog.mkdir(parents=True, exist_ok=True)
    (catalog / "sunny.json").write_text(
        json.dumps({"schema_version": 1, "bank_id": "sunny", "offers": [{"id": "sunny-1"}]}),
        encoding="utf-8",
    )
    carried, report = carry_forward(
        site_root=tmp_path,
        health=[_blocked()],
        previous_index={"generated_at": "2026-08-15T04:00:00+00:00"},
        now=datetime(2026, 8, 16, 4, 0, tzinfo=UTC),
    )
    assert carried == {} and report == []
