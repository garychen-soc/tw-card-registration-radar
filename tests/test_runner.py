"""runner 測試：逐筆容錯、活動粒度、明細快取。"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

from conftest import FakeFetcher

from radar.runner import build_offers, run_source
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


def test_incomplete_listing_is_reported_instead_of_looking_complete() -> None:
    """清單少收了分頁時，不可以長得跟全部收完一樣。

    這一路沒有任何一筆明細會失敗 —— 那些活動根本沒進到清單裡。所以如果清單
    層級不出聲，輸出上完全看不出來，逐來源覆蓋率退步防護也只會看到筆數變少、
    判成內容真的減少（實測富邦一次執行只收到 16 頁中的資料）。
    """
    from radar.adapters import listing as listing_module

    fetcher = FakeFetcher(
        pages={
            LIST_URL: _listing("/promo/1"),
            "https://www.example.com/promo/1": DETAIL_ONE,
        }
    )
    original = listing_module.READERS["json_api"]

    def truncated(spec, fetch, warnings=None):  # type: ignore[no-untyped-def]
        if warnings is not None:
            warnings.append("清單分頁未走訪完整：第 11, 12 頁在 348 次請求內都拿不到")
        return original(spec, fetch, warnings)

    listing_module.READERS["json_api"] = truncated  # type: ignore[assignment]
    try:
        result = run_source(_spec(), fetcher, today=TODAY, now=NOW)
    finally:
        listing_module.READERS["json_api"] = original

    assert len(result.campaigns) == 1, "拿到的那一筆仍然要留著"
    assert result.stats.listing_incomplete == 1
    assert [alert.type for alert in result.alerts] == ["listing_page_unreadable"]
    assert "第 11, 12 頁" in result.alerts[0].message
    assert result.health is not None
    assert result.health.status == "partial", "少收分頁不可以回報 complete"
    assert "清單層級缺漏" in result.health.message


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


def test_single_offer_page_is_not_flagged_even_when_declared_many() -> None:
    """宣告 many 的來源裡有很多頁其實只有一個活動（實測玉山 182 頁中 170 頁如此）。
    一律標記會把需人工確認的量灌到失去意義 —— 只有在有多活動證據時才標記。
    """
    fetcher = FakeFetcher(
        pages={
            LIST_URL: _listing("/promo/1"),
            "https://www.example.com/promo/1": DETAIL_ONE,
        }
    )
    spec = _spec(detail={"source": "html", "cardinality": "many"})
    result = run_source(spec, fetcher, today=TODAY, now=NOW)

    offer = result.campaigns[0].offers[0]
    assert "offer_boundary_missing" not in offer.review_codes


def test_multi_offer_evidence_without_boundary_is_informational() -> None:
    """兩組活動期間卻切不出邊界 —— 要標註，但不該把整筆藏進「需人工確認」。

    這一筆的期間、登錄時點與條件都解析成功了，藏起來反而讓使用者看不到
    本來有效的活動。實測玉山有 65 頁如此，而兩次「硬切開」的嘗試都讓
    活動數從 243 膨脹到 606／610，結果更糟 —— 所以接受切不開，誠實標註。
    """
    body = """
    <html><body>
    <p>活動期間：2026/8/1~2026/8/10</p>
    <p>登錄期間：2026/8/1 10:00~2026/8/10 23:59 開放登錄</p>
    <p>單筆滿1,000元享5%回饋。</p>
    <p>活動期間：2026/8/11~2026/8/31 加碼享8%回饋。</p>
    </body></html>
    """
    fetcher = FakeFetcher(
        pages={LIST_URL: _listing("/promo/1"), "https://www.example.com/promo/1": body}
    )
    spec = _spec(detail={"source": "html", "cardinality": "many"})
    result = run_source(spec, fetcher, today=TODAY, now=NOW)

    offer = result.campaigns[0].offers[0]
    assert "offer_boundary_missing" in offer.review_codes
    assert offer.needs_review is False, offer.review_reasons
    assert any("未能完全分開" in reason for reason in offer.review_reasons)


def test_cardinality_one_is_never_split() -> None:
    """宣告 one 就不切。實測教訓：一律套用通用邊界會讓元大的頁面被樣板文字裡的
    【】切成 4 塊（57 頁產出 227 筆），多數塊不含活動期間而被誤標。
    """
    body = """
    <html><body>
    <p>【專區】信用卡優惠</p><p>活動期間：2026/8/1~2026/8/31</p>
    <p>登錄期間：2026/8/5 10:00~2026/8/25 23:59 開放登錄</p>
    <p>【注意事項】本行保留變更權利</p>
    </body></html>
    """
    fetcher = FakeFetcher(
        pages={LIST_URL: _listing("/promo/1"), "https://www.example.com/promo/1": body}
    )
    result = run_source(_spec(), fetcher, today=TODAY, now=NOW)

    offers = result.campaigns[0].offers
    assert len(offers) == 1, "cardinality=one 不得被【】切開"
    assert offers[0].period.end == date(2026, 8, 31)


def test_registration_required_needs_positive_evidence() -> None:
    """「文字裡有登錄二字」不足以判定需要登錄 —— 頁尾的「活動登錄查詢」連結
    會讓大量不需登錄的活動被標成「需登錄但抓不到時點」（實測 274 筆）。
    """
    body = """
    <html><body>
    <p>活動期間：2026/8/1~2026/8/31 單筆滿1,000元享 5% 回饋，自動享有無需登錄。</p>
    <p>相關連結：信用卡帳單查詢</p>
    </body></html>
    """
    fetcher = FakeFetcher(
        pages={LIST_URL: _listing("/promo/1"), "https://www.example.com/promo/1": body}
    )
    result = run_source(_spec(), fetcher, today=TODAY, now=NOW)

    offer = result.campaigns[0].offers[0]
    assert offer.registration.required is False
    assert "registration_without_window" not in offer.review_codes


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


def test_page_store_serves_content_when_server_reports_not_modified() -> None:
    """304 不帶 body。若沒有本機存檔就無事可做 —— 本專案第一版因此在第二次
    執行時把 223 筆掉成 92 筆、行事曆變空。存了 HTML，304 就變成
    「用存檔重新推導」，而且用的是當下的解析器。
    """
    from radar.pagestore import PageStore
    from radar.transport import Response

    detail_url = "https://www.example.com/promo/1"
    with tempfile.TemporaryDirectory() as directory:
        pages = PageStore(Path(directory))
        pages.put(detail_url, DETAIL_ONE)

        fetcher = FakeFetcher(pages={LIST_URL: _listing("/promo/1")})
        original = fetcher.get

        def get(url: str, **kwargs: object) -> Response:
            if url == detail_url:
                return Response(
                    requested_url=url,
                    final_url=url,
                    status_code=304,
                    text="",
                    content_type="text/html",
                    content_hash="unchanged",
                    not_modified=True,
                )
            return original(url, **kwargs)  # type: ignore[arg-type]

        fetcher.get = get  # type: ignore[method-assign]
        result = run_source(_spec(), fetcher, today=TODAY, now=NOW, pages=pages)

    assert result.stats.detail_not_modified == 1
    assert result.stats.detail_fetched == 0
    offer = result.campaigns[0].offers[0]
    assert offer.registration.windows, "登錄時點不得因 304 而消失"
    assert offer.conditions.quota.seats == 1000


def test_parser_changes_always_apply_because_output_is_never_cached() -> None:
    """同一份 HTML 跑兩次必須得到同樣的結果，且第二次仍是重新推導而非沿用。

    這是「快取輸入不快取輸出」的核心保證：解析器改了，全部頁面立即受益。
    """
    from radar.pagestore import PageStore

    with tempfile.TemporaryDirectory() as directory:
        pages = PageStore(Path(directory))
        fetcher = FakeFetcher(
            pages={LIST_URL: _listing("/promo/1"), "https://www.example.com/promo/1": DETAIL_ONE}
        )
        spec = _spec()
        first = run_source(spec, fetcher, today=TODAY, now=NOW, pages=pages)
        second = run_source(spec, fetcher, today=TODAY, now=NOW, pages=pages)

    assert second.stats.detail_fetched == 1, "沒有驗證標頭時不快取，每次都重抓"
    assert [offer.id for offer in first.campaigns[0].offers] == [
        offer.id for offer in second.campaigns[0].offers
    ]
    assert (
        first.campaigns[0].offers[0].registration.windows[0].end
        == second.campaigns[0].offers[0].registration.windows[0].end
    )


def test_page_level_period_is_inherited_by_sub_offers() -> None:
    """單頁多活動時，活動期間常寫在第一個子活動之前的前言裡。

    split_offers 只保留各邊界之後的內容，所以子活動看不到它 —— 星展的頁面
    正是如此，68 筆全都抓不到期間。子活動沒寫自己的期間時要繼承頁面層級的。
    """
    body = """
    <html><body>
    <p>活動期間：2026/8/1~2026/8/31</p>
    <p>於指定通路刷卡並完成登錄即可享回饋。</p>
    <p>活動一、單筆滿5,000元享500點，限量400名，登錄期間：2026/8/1 15:00~2026/8/31 23:59</p>
    <p>活動二、單筆滿20,000元享5,000點，限量250名，登錄期間：2026/8/1 15:00~2026/8/31 23:59</p>
    </body></html>
    """
    fetcher = FakeFetcher(
        pages={LIST_URL: _listing("/promo/1"), "https://www.example.com/promo/1": body}
    )
    spec = _spec(detail={"source": "html", "cardinality": "many"})
    result = run_source(spec, fetcher, today=TODAY, now=NOW)

    offers = result.campaigns[0].offers
    assert len(offers) == 2
    for offer in offers:
        assert offer.period.start == date(2026, 8, 1), offer.title
        assert offer.period.end == date(2026, 8, 31), offer.title
        assert "period_missing" not in offer.review_codes
        # 繼承來的期間信心要低於子活動自己寫的
        assert offer.period.confidence <= 0.6


def test_access_denied_is_reported_as_blocked_not_a_transient_failure() -> None:
    """403 不是暫時性故障，重試不會好 —— 它需要換執行環境或等官方解除限制。

    實測陽信與第一銀行從 GitHub Actions 的 datacenter IP 存取會被拒，
    但從住宅 IP 正常。混在「暫時無法讀取」裡會讓人誤判為短暫問題。
    """
    from radar.transport import AccessDenied

    fetcher = FakeFetcher(failures={LIST_URL: AccessDenied("HTTP 403")})
    result = run_source(_spec(), fetcher, today=TODAY, now=NOW)

    assert result.health is not None
    assert result.health.status == "blocked"
    assert [alert.type for alert in result.alerts] == ["source_access_blocked"]


def test_access_denied_on_a_detail_page_skips_only_that_item() -> None:
    from radar.transport import AccessDenied

    fetcher = FakeFetcher(
        pages={
            LIST_URL: _listing("/promo/blocked", "/promo/1"),
            "https://www.example.com/promo/1": DETAIL_ONE,
        },
        failures={"https://www.example.com/promo/blocked": AccessDenied("HTTP 403")},
    )
    result = run_source(_spec(), fetcher, today=TODAY, now=NOW)

    assert len(result.campaigns) == 1, "沒被拒的那一筆必須存活"
    assert result.stats.detail_blocked == 1
    assert [alert.type for alert in result.alerts] == ["source_access_blocked"]


def test_source_with_every_item_denied_is_blocked_not_failed() -> None:
    """全被拒絕存取時要說「被拒」而不是「失敗」。

    前者換執行環境能解，後者是來源本身有問題 —— 混在一起就失去了
    「該不該改走 self-hosted runner」的判斷依據（實測陽信在 CI 上如此）。
    """
    from radar.transport import AccessDenied

    fetcher = FakeFetcher(
        pages={LIST_URL: _listing("/promo/1")},
        failures={"https://www.example.com/promo/1": AccessDenied("HTTP 403")},
    )
    result = run_source(_spec(), fetcher, today=TODAY, now=NOW)

    assert result.campaigns == []
    assert result.health is not None
    assert result.health.status == "blocked"


def test_source_with_unreadable_items_is_still_failed() -> None:
    """讀取失敗（5xx／逾時）仍是 failed —— 那不是換 IP 能解的。"""
    fetcher = FakeFetcher(
        pages={LIST_URL: _listing("/promo/1")},
        failures={"https://www.example.com/promo/1": FetchFailed("HTTP 503")},
    )
    result = run_source(_spec(), fetcher, today=TODAY, now=NOW)
    assert result.health is not None
    assert result.health.status in {"partial", "failed"}


def test_sibling_offers_do_not_inherit_registration_windows() -> None:
    """一個子活動寫了登錄時點，不得擴散到同頁其他子活動。

    實測聯邦 MWorldcard：頁面共用區塊裡有別的子活動的期間
    （2026/6/17-2026/12/31），一度讓 56 筆掛上錯的登錄時間。使用者會照著錯的
    時間去登錄，那比「抓不到時點」更糟 —— 留白由 needs_review 誠實標示出來。
    """
    body = """
    <html><body>
    <p>活動期間：2026/8/1~2026/8/31</p>
    <p>活動一、登錄期間：2026/8/5 10:00~2026/8/10 23:59，於登錄後以本行信用卡
    單筆消費滿5,000元，享500點紅利回饋。</p>
    <p>活動二、於活動期間內以本行信用卡單筆消費滿20,000元，完成登錄後
    享5,000點紅利回饋，每月限量250名，額滿為止。</p>
    </body></html>
    """
    fetcher = FakeFetcher(
        pages={LIST_URL: _listing("/promo/1"), "https://www.example.com/promo/1": body}
    )
    spec = _spec(detail={"source": "html", "cardinality": "many"})
    result = run_source(spec, fetcher, today=TODAY, now=NOW)

    offers = result.campaigns[0].offers
    assert len(offers) == 2
    own = offers[0].registration.windows
    assert len(own) == 1
    assert own[0].start == datetime.fromisoformat("2026-08-05T10:00:00+08:00")
    assert own[0].end == datetime.fromisoformat("2026-08-10T23:59:00+08:00")
    # 活動二自己沒寫時點：留白，並誠實標記
    assert offers[1].registration.windows == []
    assert offers[1].registration.required
    assert "registration_without_window" in offers[1].review_codes


def test_page_level_recurrence_is_inherited() -> None:
    """「每期需重新登錄」整頁只寫一次，切分後只會留在最後一個子活動上。

    循環規則不帶具體時點，誤植的代價遠低於登錄視窗，所以這一項取全頁。
    """
    body = """
    <html><body>
    <p>活動期間：2026/8/1~2026/8/31</p>
    <p>活動一、於活動期間內以本行信用卡單筆消費滿5,000元，享500點紅利回饋，
    每月限量400名，額滿為止。</p>
    <p>活動二、於活動期間內以本行信用卡單筆消費滿20,000元，享5,000點紅利回饋，
    每月限量250名，額滿為止。</p>
    <p>注意事項：本活動每期需重新登錄，未登錄者不予補登。</p>
    </body></html>
    """
    fetcher = FakeFetcher(
        pages={LIST_URL: _listing("/promo/1"), "https://www.example.com/promo/1": body}
    )
    spec = _spec(detail={"source": "html", "cardinality": "many"})
    result = run_source(spec, fetcher, today=TODAY, now=NOW)

    offers = result.campaigns[0].offers
    assert len(offers) == 2
    for offer in offers:
        assert offer.registration.recurrence.kind == "per_campaign_period", offer.title
        assert offer.registration.timing_contract.kind == "per_period_reregister"
    # 注意事項落在最後一個區塊裡，所以活動二是自己抓到的、活動一是繼承的。
    # 繼承的那筆信心降一級並在 note 上註明來源。
    assert "頁面共用敘述" in offers[0].registration.recurrence.note
    assert offers[0].registration.recurrence.confidence == 0.6
    assert offers[1].registration.recurrence.note == "每期需重新登錄"
    assert offers[1].registration.recurrence.confidence == 0.7


def test_listing_registration_field_feeds_the_window_parser() -> None:
    """銀行在清單層直接公告的登錄期間要用得上。

    實測第一銀行的端點有 loginDate 欄位，16 筆有值、其中 8 筆的登錄資訊只存在
    那裡（明細頁內文完全沒寫）。做法是加上「登錄期間：」標籤前綴再交給同一套
    解析器，不另寫規則。
    """
    body = "<html><body><p>活動期間：2026/8/1~2026/8/31</p><p>單筆滿千享3%</p></body></html>"
    fetcher = FakeFetcher(
        pages={LIST_URL: _listing("/promo/1"), "https://www.example.com/promo/1": body}
    )
    offers = build_offers(
        _spec(),
        url="https://www.example.com/promo/1",
        html=body,
        text="活動期間：2026/8/1~2026/8/31\n單筆滿千享3%",
        today=TODAY,
        listing_registration="2026.8.5~2026.8.20",
    )
    assert len(offers) == 1
    windows = offers[0].registration.windows
    assert len(windows) == 1
    assert windows[0].start == datetime.fromisoformat("2026-08-05T00:00:00+08:00")
    assert windows[0].end == datetime.fromisoformat("2026-08-20T23:59:00+08:00")
    # 頁面層級的來源 —— 信心要低於子活動自己寫的
    assert windows[0].confidence <= 0.8
    assert fetcher.requested == []


def test_listing_registration_recurrence_without_dates() -> None:
    """「每月22日上午10點起(逐月登錄，額滿即關閉)」沒有具體日期，
    但循環規則本身就是使用者最需要知道的事。"""
    text = "活動期間：2026/1/1~2026/12/31\n單筆滿千享3%"
    offers = build_offers(
        _spec(),
        url="https://www.example.com/promo/1",
        html=f"<html><body>{text}</body></html>",
        text=text,
        today=TODAY,
        listing_registration="每月22日上午10點起(逐月登錄，額滿即關閉)",
    )
    recurrence = offers[0].registration.recurrence
    assert recurrence.kind == "monthly"
    assert "22 日" in recurrence.note and "10:00" in recurrence.note
    assert offers[0].registration.timing_contract.kind == "per_period_reregister"


def test_offer_windows_win_over_listing_registration() -> None:
    """子活動自己寫了登錄時點時，清單層的欄位不得覆蓋它。"""
    text = "活動期間：2026/8/1~2026/8/31\n登錄期間：2026/8/7 17:00~2026/8/10 23:59 開放登錄"
    offers = build_offers(
        _spec(),
        url="https://www.example.com/promo/1",
        html=f"<html><body>{text}</body></html>",
        text=text,
        today=TODAY,
        listing_registration="2026.8.20~2026.8.25",
    )
    windows = offers[0].registration.windows
    assert len(windows) == 1
    assert windows[0].start == datetime.fromisoformat("2026-08-07T17:00:00+08:00")
