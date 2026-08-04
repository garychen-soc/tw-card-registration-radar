"""runner 測試：逐筆容錯、活動粒度、明細快取。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from conftest import FakeFetcher

from radar.runner import run_source
from radar.spec import SourceSpec
from radar.transport import BlockedURL, FetchFailed

TODAY = date(2026, 8, 4)
NOW = datetime(2026, 8, 4, 10, 0, tzinfo=UTC)
LIST_URL = "https://www.example.com/api/list.json"

DETAIL_ONE = """
<html><body>
<h1>夏日網購回饋</h1>
<p>活動期間：2026/8/1~2026/8/31</p>
<p>登錄期間：2026/8/7 17:00:00~2026/8/20 23:59:00 開放登錄</p>
<p>單筆滿10,000元享5%回饋，限量登錄1,000名，限正卡人登錄。</p>
</body></html>
"""

DETAIL_MULTI = """
<html><body>
<p>【活動一】首購回饋 活動期間：2026/8/1~2026/8/10
登錄期間：2026/8/1 10:00~2026/8/10 23:59 開放登錄 限量300名</p>
<p>【活動二】加碼回饋 活動期間：2026/8/11~2026/8/31
登錄期間：2026/8/11 10:00~2026/8/31 23:59 開放登錄 限量500名</p>
</body></html>
"""


def _spec(**overrides: object) -> SourceSpec:
    payload: dict[str, object] = {
        "id": "demo",
        "bank_name": "示範銀行",
        "domains": ["example.com"],
        "listing": {
            "kind": "json_api",
            "entry_url": "https://www.example.com/list",
            "data_url": LIST_URL,
            "fields": {"title": "title", "url": "url"},
        },
        "registration": {
            "portal_url": "https://www.example.com/register",
            "portal_kind": "bank_portal",
            "portal_hint": "登入後找到對應活動",
        },
    }
    payload.update(overrides)
    return SourceSpec.model_validate(payload)


def _listing(*urls: str) -> str:
    return json.dumps([{"title": f"活動 {index}", "url": url} for index, url in enumerate(urls)])


def test_happy_path_builds_campaign_with_parsed_offer() -> None:
    fetcher = FakeFetcher(
        pages={
            LIST_URL: _listing("/promo/1"),
            "https://www.example.com/promo/1": DETAIL_ONE,
        }
    )
    result = run_source(_spec(), fetcher, today=TODAY, now=NOW)

    assert result.health is not None
    assert result.health.status == "complete"
    assert len(result.campaigns) == 1
    offer = result.campaigns[0].offers[0]
    assert offer.period.start == date(2026, 8, 1)
    assert offer.period.end == date(2026, 8, 31)
    window = offer.registration.windows[0]
    assert window.kind == "range"
    assert window.end == datetime.fromisoformat("2026-08-20T23:59:00+08:00")
    assert offer.registration.timing_contract.kind == "registration_closes_early"
    assert offer.registration.timing_contract.spend_days_left_after_registering == 11
    assert offer.conditions.quota.seats == 1000
    assert offer.conditions.eligibility.primary_card_only is True
    assert offer.registration.portal.kind == "bank_portal"


def test_blocked_detail_link_skips_one_item_and_keeps_the_rest() -> None:
    """核心回歸：前身在單一筆連結被拒時整次更新 exit 1，其餘來源一起失敗。"""
    fetcher = FakeFetcher(
        pages={
            LIST_URL: _listing("/promo/bad", "/promo/1"),
            "https://www.example.com/promo/1": DETAIL_ONE,
        },
        failures={
            "https://www.example.com/promo/bad": BlockedURL(
                "官方頁導向不可信任的位址：http://10.100.6.38/frontend/bonusDetail.jsp?id=3450"
            )
        },
    )
    result = run_source(_spec(), fetcher, today=TODAY, now=NOW)

    assert len(result.campaigns) == 1, "好的那一筆必須存活"
    assert result.stats.detail_blocked == 1
    assert [alert.type for alert in result.alerts] == ["source_emitted_invalid_url"]
    assert "10.100.6.38" in result.alerts[0].message
    assert result.health is not None
    assert result.health.status == "partial", "來源本身沒壞，不該判定為 failed"


def test_unreadable_detail_falls_back_to_listing_text() -> None:
    fetcher = FakeFetcher(
        pages={LIST_URL: _listing("/promo/1")},
        failures={"https://www.example.com/promo/1": FetchFailed("HTTP 503")},
    )
    result = run_source(_spec(), fetcher, today=TODAY, now=NOW)

    assert result.stats.detail_failed == 1
    assert [alert.type for alert in result.alerts] == ["detail_unreadable"]
    # 仍然產出活動（用清單資訊），只是資訊不完整
    assert len(result.campaigns) == 1


def test_listing_failure_reports_failed_without_campaigns() -> None:
    fetcher = FakeFetcher(failures={LIST_URL: FetchFailed("連線逾時")})
    result = run_source(_spec(), fetcher, today=TODAY, now=NOW)

    assert result.campaigns == []
    assert result.health is not None
    assert result.health.status == "failed"
    assert result.alerts[0].type == "source_failed"


def test_listing_blocked_is_distinct_from_failed() -> None:
    fetcher = FakeFetcher(failures={LIST_URL: BlockedURL("非官方網域")})
    result = run_source(_spec(), fetcher, today=TODAY, now=NOW)

    assert result.health is not None
    assert result.health.status == "blocked"
    assert result.alerts[0].type == "source_emitted_invalid_url"


def test_multi_offer_page_splits_into_independent_offers() -> None:
    """一頁兩個子活動，各自持有自己的期間與登錄視窗 —— 不互相污染。"""
    fetcher = FakeFetcher(
        pages={
            LIST_URL: _listing("/promo/multi"),
            "https://www.example.com/promo/multi": DETAIL_MULTI,
        }
    )
    spec = _spec(detail={"source": "html", "cardinality": "many"})
    result = run_source(spec, fetcher, today=TODAY, now=NOW)

    offers = result.campaigns[0].offers
    assert len(offers) == 2
    assert offers[0].period.end == date(2026, 8, 10)
    assert offers[1].period.start == date(2026, 8, 11)
    assert offers[0].conditions.quota.seats == 300
    assert offers[1].conditions.quota.seats == 500
    # 每個子活動的登錄視窗都落在自己的期間內
    for offer in offers:
        assert not offer.needs_review, offer.review_reasons


def test_cardinality_many_without_boundary_is_flagged() -> None:
    """spec 說這家是單頁多活動卻切不出邊界時，必須標記而非假裝切好了。"""
    fetcher = FakeFetcher(
        pages={
            LIST_URL: _listing("/promo/1"),
            "https://www.example.com/promo/1": DETAIL_ONE,
        }
    )
    spec = _spec(detail={"source": "html", "cardinality": "many"})
    result = run_source(spec, fetcher, today=TODAY, now=NOW)

    offer = result.campaigns[0].offers[0]
    assert offer.needs_review is True
    assert any("未能切出邊界" in reason for reason in offer.review_reasons)


def test_cache_reuse_skips_the_detail_request() -> None:
    fetcher = FakeFetcher(
        pages={
            LIST_URL: _listing("/promo/1"),
            "https://www.example.com/promo/1": DETAIL_ONE,
        }
    )
    spec = _spec()
    first = run_source(spec, fetcher, today=TODAY, now=NOW)
    assert first.stats.detail_fetched == 1

    previous = {campaign.source_url: campaign for campaign in first.campaigns}
    fetcher.requested.clear()
    # 活動起訖日離「今天」都超過 3 天，才會真的命中快取
    later = run_source(spec, fetcher, today=date(2026, 8, 20), now=NOW, previous=previous)

    assert later.stats.detail_reused == 1
    assert later.stats.detail_fetched == 0
    assert "https://www.example.com/promo/1" not in fetcher.requested


def test_cache_is_bypassed_near_activity_boundary() -> None:
    """活動起訖日前後 3 天內強制重讀 —— 這時的資料最容易變動。"""
    fetcher = FakeFetcher(
        pages={
            LIST_URL: _listing("/promo/1"),
            "https://www.example.com/promo/1": DETAIL_ONE,
        }
    )
    spec = _spec()
    first = run_source(spec, fetcher, today=TODAY, now=NOW)
    previous = {campaign.source_url: campaign for campaign in first.campaigns}

    # 8/30 距活動結束日 8/31 只有一天
    later = run_source(spec, fetcher, today=date(2026, 8, 30), now=NOW, previous=previous)
    assert later.stats.detail_reused == 0
    assert later.stats.detail_fetched == 1


def test_cache_is_bypassed_when_listing_changed() -> None:
    spec = _spec()
    fetcher = FakeFetcher(
        pages={
            LIST_URL: _listing("/promo/1"),
            "https://www.example.com/promo/1": DETAIL_ONE,
        }
    )
    first = run_source(spec, fetcher, today=TODAY, now=NOW)
    previous = {campaign.source_url: campaign for campaign in first.campaigns}

    fetcher.pages[LIST_URL] = json.dumps(
        [{"title": "活動 0 已改標題", "url": "/promo/1"}]
    )
    later = run_source(spec, fetcher, today=date(2026, 8, 20), now=NOW, previous=previous)
    assert later.stats.detail_reused == 0


def test_api_detail_source_uses_listing_props() -> None:
    """國泰世華式：明細頁是 SPA，內容只在清單 API 的附加欄位裡。"""
    payload = [
        {
            "campaignPath": "/promo/spa",
            "campaignProps": {
                "blocks": [
                    {"text": "活動期間：2026/8/1~2026/8/31"},
                    {"text": "登錄期間：2026/8/5 10:00~2026/8/25 23:59 開放登錄"},
                ]
            },
        }
    ]
    spec = _spec(
        listing={
            "kind": "json_api",
            "entry_url": "https://www.example.com/list",
            "data_url": LIST_URL,
            "fields": {"url": "campaignPath", "props": "campaignProps"},
        },
        detail={"source": "api", "cardinality": "one"},
    )
    fetcher = FakeFetcher(pages={LIST_URL: json.dumps(payload)})
    result = run_source(spec, fetcher, today=TODAY, now=NOW)

    assert fetcher.requested == [LIST_URL], "detail.source=api 不應再抓明細頁"
    offer = result.campaigns[0].offers[0]
    assert offer.period.end == date(2026, 8, 31)
    assert offer.registration.windows[0].end == datetime.fromisoformat(
        "2026-08-25T23:59:00+08:00"
    )
