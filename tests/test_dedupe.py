"""去重測試。

實測動機：星展「【網購星精彩】7/1~9/30」在網站上出現 6 次 —— 期間、3 個登錄時點、
限量、時序契約完全相同，只有網址不同（mall_08 / mall_08_2 / mall_08_3 / mall_08_5 /
mall_09 / mall_11）。官方把同一個共用活動區塊掛在六個子頁上，一頁一 Campaign 的
抓取模型必然各切出一份。

這個檔案要守住的是**兩個方向**都不能出錯：該合的要合，不該合的一筆都不能少。
後者更要緊 —— 誤併會讓 catalog 直接少掉一個真實活動頁，而使用者永遠不會知道
它存在。全站 17 家 1,077 筆的實測：內容雜湊有 33 組相同，其中只有 3 組是真重複。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from radar.emit import build_catalog, build_index, dedupe_campaigns, offer_content_key
from radar.guard import assess
from radar.models import (
    Campaign,
    Conditions,
    Offer,
    Period,
    Quota,
    Registration,
    RegistrationWindow,
    SourceHealth,
    ThresholdTier,
    TimingContract,
)

NOW = datetime(2026, 8, 6, 2, 0, tzinfo=UTC)

WINDOW = RegistrationWindow(
    kind="opens_at",
    start=datetime.fromisoformat("2026-07-24T16:00:00+08:00"),
    confidence=0.8,
)


def _offer(
    offer_id: str,
    *,
    title: str = "【網購星精彩】7/1~9/30",
    start: date = date(2026, 7, 1),
    end: date = date(2026, 9, 30),
    quota: bool = True,
    tiers: list[ThresholdTier] | None = None,
    windows: list[RegistrationWindow] | None = None,
) -> Offer:
    return Offer(
        id=offer_id,
        title=title,
        period=Period(start=start, end=end, confidence=0.55),
        registration=Registration(
            required=True,
            windows=windows if windows is not None else [WINDOW],
            timing_contract=TimingContract(kind="per_period_reregister", confidence=0.6),
        ),
        conditions=Conditions(
            quota=Quota(limited=quota),
            threshold_tiers=tiers or [],
        ),
    )


def _campaign(
    campaign_id: str, url: str, *offers: Offer, title: str = "星展購物優惠"
) -> Campaign:
    return Campaign(
        id=campaign_id,
        bank_id="dbs",
        bank_name="星展銀行",
        title=title,
        source_url=url,
        observed_at=NOW,
        offers=list(offers),
    )


def _mirror_pages(*urls: str) -> list[Campaign]:
    """把同一份兩筆子活動的內容掛在多個網址上 —— 星展實測型態。"""
    return [
        _campaign(
            f"dbs-{index}",
            url,
            _offer(f"dbs-{index}-0"),
            _offer(f"dbs-{index}-1", title="【指定商店星展日滿萬送千】7/1~9/30"),
        )
        for index, url in enumerate(urls)
    ]


BASE = "https://www.dbs.com.tw/personal-zh/cards/offers/ce"


# ── 該合併的 ──────────────────────────────────────────────


def test_mirror_pages_collapse_to_one() -> None:
    """星展本案：六個網址切出同一份清單，只留一份。"""
    campaigns = _mirror_pages(
        f"{BASE}/mall_08.html",
        f"{BASE}/mall_08_2.html",
        f"{BASE}/mall_08_3.html",
        f"{BASE}/mall_08_5.html",
        f"{BASE}/mall_09.html",
        f"{BASE}/mall_11.html",
    )
    result, report = dedupe_campaigns(campaigns)

    assert len(result) == 1
    assert sum(len(campaign.offers) for campaign in result) == 2
    assert report.merged_offers == 10
    assert report.merged_campaigns == 5
    assert report.per_source == {"dbs": 10}


def test_merged_urls_survive_in_also_at() -> None:
    """直接丟掉會讓使用者失去「這個活動也公告在某幾頁」—— 他可能正是從那一頁找來的。"""
    result, _ = dedupe_campaigns(
        _mirror_pages(f"{BASE}/mall_08.html", f"{BASE}/mall_09.html", f"{BASE}/mall_11.html")
    )
    for offer in result[0].offers:
        assert offer.also_at == [f"{BASE}/mall_09.html", f"{BASE}/mall_11.html"]


def test_kept_url_is_the_shortest_base_page() -> None:
    """保留基底頁而不是抓取順序的第一頁 —— offer id 內含網址 slug，
    換代表就等於換 id，使用者的書籤與「已登錄」記錄會失效。"""
    result, _ = dedupe_campaigns(
        _mirror_pages(f"{BASE}/mall_08_5.html", f"{BASE}/mall_08_2.html", f"{BASE}/mall_08.html")
    )
    assert result[0].source_url == f"{BASE}/mall_08.html"


def test_query_string_variant_of_same_page_merges() -> None:
    """聯邦實測：202607drugstore/index.htm 與 ...index.htm?p=cosmed 是同一頁。"""
    plain = "https://activity.ubot.com.tw/aws_act/2026/202607drugstore/index.htm"
    result, report = dedupe_campaigns(_mirror_pages(plain, f"{plain}?p=cosmed"))
    assert len(result) == 1
    assert result[0].source_url == plain
    assert result[0].offers[0].also_at == [f"{plain}?p=cosmed"]
    assert report.merged_offers == 2


def test_duplicate_offers_within_one_page_collapse() -> None:
    """同一頁上渲染結果相同的兩列，使用者無從分辨，多顯示一列只是雜訊。
    合併的兩筆同屬一頁，不存在「弄丟了另一個活動頁」的風險（實測聯邦 6 筆屬此類）。"""
    campaigns = [
        _campaign(
            "ubot-1",
            "https://activity.ubot.com.tw/x.htm",
            _offer("a", title="【共同注意事項】"),
            _offer("b", title="【共同注意事項】"),
            _offer("c", title="【康是美網購eShop】"),
        )
    ]
    result, report = dedupe_campaigns(campaigns)
    assert [offer.title for offer in result[0].offers] == [
        "【共同注意事項】",
        "【康是美網購eShop】",
    ]
    assert report.merged_offers == 1
    # 同頁合併沒有「別的網址」可記，also_at 保持空的
    assert all(not offer.also_at for offer in result[0].offers)


# ── 不該合併的 ────────────────────────────────────────────


def test_same_title_different_period_never_merges() -> None:
    """同一個活動的 7 月檔與 8 月檔是**不同**活動。

    實測玉山「【活動三】玉山Unicard專屬優惠」有 2026-08-01~08-15（sno=2008_08）
    與 2026-08-17~08-31（sno=2100_08）兩檔，合併會讓使用者看到錯的期間、
    錯過其中一檔的登錄。
    """
    campaigns = _mirror_pages(f"{BASE}/a.html", f"{BASE}/b.html")
    campaigns[1] = _campaign(
        "dbs-1",
        f"{BASE}/b.html",
        _offer("dbs-1-0", start=date(2026, 8, 17), end=date(2026, 8, 31)),
        _offer(
            "dbs-1-1",
            title="【指定商店星展日滿萬送千】7/1~9/30",
            start=date(2026, 8, 17),
            end=date(2026, 8, 31),
        ),
    )
    result, report = dedupe_campaigns(campaigns)

    assert len(result) == 2
    assert report.merged_offers == 0
    periods = {
        (offer.period.start, offer.period.end)
        for campaign in result
        for offer in campaign.offers
    }
    assert periods == {
        (date(2026, 7, 1), date(2026, 9, 30)),
        (date(2026, 8, 17), date(2026, 8, 31)),
    }


def test_same_title_and_period_but_different_conditions_never_merges() -> None:
    """實測玉山同一頁 sno=2008_08 的「【活動四】」兩筆：標題、期間、登錄時點全同，
    門檻與名額不同 —— 若鍵只用標題＋期間＋時點，全站會誤併 21 組。"""
    campaigns = [
        _campaign(
            "esun-1",
            "https://www.esunbank.com/x?sno=2008_08",
            _offer("esun-1-3", tiers=[ThresholdTier(spend_twd=50000)]),
            _offer("esun-1-9", tiers=[ThresholdTier(spend_twd=50000, reward_twd=1000)]),
        )
    ]
    result, report = dedupe_campaigns(campaigns)
    assert len(result[0].offers) == 2
    assert report.merged_offers == 0


def test_single_offer_pages_never_merge_across_urls() -> None:
    """一頁一筆、內容相同的頁面不合併 —— 那幾乎都是「什麼都沒解析出來」。

    實測凱基 5 個不同活動頁（elifemall-a / tk3c / cht-fet-gt-a / tatung-lg-sunfar /
    cashback-studioa）全叫「信用卡活動」、期間都是清單層的整年 fallback、
    沒有登錄時點也沒有條件。合併會讓 catalog 少掉 4 個真實活動頁。
    """
    urls = [
        f"https://www.kgibank.com.tw/zh-tw/personal/promotion/card-campaign/{slug}"
        for slug in ("elifemall-a", "tk3c", "cht-fet-gt-a", "tatung-lg-sunfar", "cashback-studioa")
    ]
    campaigns = [
        _campaign(
            f"kgi-{index}",
            url,
            _offer(
                f"kgi-{index}-0",
                title="信用卡活動",
                start=date(2026, 1, 1),
                end=date(2026, 12, 31),
                quota=False,
                windows=[],
            ),
        )
        for index, url in enumerate(urls)
    ]
    result, report = dedupe_campaigns(campaigns)

    assert len(result) == 5
    assert report.merged_offers == 0
    assert {campaign.source_url for campaign in result} == set(urls)


def test_partially_matching_pages_never_merge() -> None:
    """整頁清單只對上一半就不是鏡射頁 —— 那是兩個共用某段內容的不同活動。"""
    campaigns = _mirror_pages(f"{BASE}/a.html", f"{BASE}/b.html")
    campaigns[1].offers[1] = _offer("dbs-1-1", title="【博客來會員現折優惠】7/1~9/30")
    result, report = dedupe_campaigns(campaigns)
    assert len(result) == 2
    assert report.merged_offers == 0


def test_same_content_across_banks_never_merges() -> None:
    """跨銀行的「同內容」只會是兩家都沒解析出東西，那是巧合而不是同一個活動。"""
    campaigns = _mirror_pages("https://a.example/1", "https://b.example/1")
    campaigns[1] = campaigns[1].model_copy(update={"bank_id": "esun", "bank_name": "玉山銀行"})
    result, report = dedupe_campaigns(campaigns)
    assert len(result) == 2
    assert report.merged_offers == 0


# ── 鍵的定義 ──────────────────────────────────────────────


def test_content_key_ignores_where_it_came_from() -> None:
    """id / url 是「這筆從哪一頁切出來」的紀錄，不是「這是哪一個活動」。"""
    assert offer_content_key(_offer("dbs-a-0")) == offer_content_key(_offer("dbs-b-3"))


def test_content_key_covers_everything_the_catalog_shows() -> None:
    """鍵等於「使用者看得到的全部內容」，才不可能併掉他分辨得出來的差異。"""
    base = _offer("x")
    for variant in (
        _offer("x", title="別的活動"),
        _offer("x", start=date(2026, 8, 1)),
        _offer("x", end=date(2026, 8, 31)),
        _offer("x", quota=False),
        _offer("x", tiers=[ThresholdTier(spend_twd=10000)]),
        _offer("x", windows=[]),
    ):
        assert offer_content_key(variant) != offer_content_key(base)


def test_also_at_is_not_part_of_the_key() -> None:
    """also_at 是去重的產物。讓它進鍵會使去重不具幕等性（第二次跑不出同樣結果）。"""
    plain = _offer("x")
    marked = _offer("x").model_copy(update={"also_at": [f"{BASE}/mall_09.html"]})
    assert offer_content_key(plain) == offer_content_key(marked)


def test_dedupe_does_not_mutate_input() -> None:
    """輸入的 campaigns 之後還要交給 write_site，不能被就地改掉。"""
    campaigns = _mirror_pages(f"{BASE}/mall_08.html", f"{BASE}/mall_09.html")
    dedupe_campaigns(campaigns)
    assert len(campaigns) == 2
    assert all(not offer.also_at for campaign in campaigns for offer in campaign.offers)


def test_dedupe_is_idempotent() -> None:
    once, _ = dedupe_campaigns(_mirror_pages(f"{BASE}/mall_08.html", f"{BASE}/mall_09.html"))
    twice, report = dedupe_campaigns(once)
    assert report.merged_offers == 0
    assert [campaign.source_url for campaign in twice] == [f"{BASE}/mall_08.html"]


# ── 與涵蓋率防護的交互作用 ────────────────────────────────


def _health(offer_count: int) -> list[SourceHealth]:
    return [
        SourceHealth(
            bank_id="dbs",
            bank_name="星展銀行",
            requested_url="https://www.dbs.com.tw/list",
            status="complete",
            campaign_count=6,
            offer_count=offer_count,
        )
    ]


def _index_after_dedupe(campaigns: list[Campaign]) -> dict[str, object]:
    raw_offers = sum(len(campaign.offers) for campaign in campaigns)
    deduped, report = dedupe_campaigns(campaigns)
    return build_index(
        deduped,
        health=_health(raw_offers),
        alerts=[],
        generated_at=NOW,
        dedupe=report,
    )


def test_index_keeps_raw_count_as_coverage_baseline() -> None:
    """``offers`` 與 ``sources[].offer_count`` 必須維持「去重前」的舊語意。

    guard.assess 拿上一版 index.json 的這兩個欄位當基準；改成去重後的數字，
    去重上線那次就會被記成一次憑空的筆數下降（實測星展 68→43 掉 36.8%，
    逐來源門檻 40%），而且下一次執行的基準變小，防護從此少偵測到一截真正的退步。
    """
    index = _index_after_dedupe(_mirror_pages(f"{BASE}/mall_08.html", f"{BASE}/mall_09.html"))
    counts = index["counts"]
    assert isinstance(counts, dict)
    assert counts["offers"] == 4          # 去重前
    assert counts["unique_offers"] == 2   # 實際發布
    assert counts["duplicate_offers"] == 2
    assert counts["campaigns"] == 2       # 去重前的活動頁數

    sources = index["sources"]
    assert isinstance(sources, list)
    assert sources[0]["offer_count"] == 4
    assert sources[0]["unique_offer_count"] == 2


def test_dedupe_alone_does_not_trip_the_coverage_guard() -> None:
    """去重上線那次不得被當成抓取退步。

    模擬最嚴苛的情形：上一版是去重前發布的（4 筆），這次同樣抓到 4 筆但去重成 2 筆。
    防護拿到的仍是 4 vs 4，不會觸發逐來源 40% 或全站 50% 的門檻。
    """
    campaigns = _mirror_pages(f"{BASE}/mall_08.html", f"{BASE}/mall_09.html")
    previous = build_index(campaigns, health=_health(4), alerts=[], generated_at=NOW)
    index = _index_after_dedupe(campaigns)

    counts = index["counts"]
    assert isinstance(counts, dict)
    guard = assess(
        health=_health(4),
        current_offers=int(counts["offers"]),
        previous_index=previous,
    )
    assert guard.status == "passed"
    assert guard.regressions == []
    assert guard.total_drop_percent == 0.0


def test_guard_still_catches_a_real_regression_after_dedupe() -> None:
    """去重不能把防護弄鈍：真的抓取退步仍要擋下。"""
    campaigns = _mirror_pages(f"{BASE}/mall_08.html", f"{BASE}/mall_09.html")
    previous = build_index(campaigns, health=_health(20), alerts=[], generated_at=NOW)
    index = _index_after_dedupe(campaigns)

    counts = index["counts"]
    assert isinstance(counts, dict)
    guard = assess(
        health=_health(4),  # 官方頁本來讀到 20 筆，這次只剩 4 筆
        current_offers=int(counts["offers"]),
        previous_index=previous,
    )
    assert guard.status == "blocked"
    assert "per_source_coverage_regression" in guard.reason_codes


# ── 輸出 ──────────────────────────────────────────────────


def test_catalog_publishes_also_at() -> None:
    result, _ = dedupe_campaigns(
        _mirror_pages(f"{BASE}/mall_08.html", f"{BASE}/mall_09.html", f"{BASE}/mall_11.html")
    )
    catalog = build_catalog("dbs", result)
    offers = catalog["offers"]
    assert isinstance(offers, list)
    assert len(offers) == 2
    assert offers[0]["also_at"] == [f"{BASE}/mall_09.html", f"{BASE}/mall_11.html"]


def test_also_at_is_absent_when_nothing_was_merged() -> None:
    """預設值不輸出 —— 沒有重複的活動不該多帶一個空陣列。"""
    result, _ = dedupe_campaigns([_campaign("dbs-0", f"{BASE}/mall_08.html", _offer("a"))])
    catalog = build_catalog("dbs", result)
    assert "also_at" not in json.dumps(catalog, ensure_ascii=False)


def test_agenda_carries_also_at() -> None:
    """時間軸也要看得到 —— 星展本案的重複正是出現在時間軸上。"""
    index = _index_after_dedupe(_mirror_pages(f"{BASE}/mall_08.html", f"{BASE}/mall_09.html"))
    agenda = index["agenda"]
    assert isinstance(agenda, list)
    assert len(agenda) == 2  # 兩筆各一次，不是四次
    assert {entry["title"] for entry in agenda} == {
        "【網購星精彩】7/1~9/30",
        "【指定商店星展日滿萬送千】7/1~9/30",
    }
    for entry in agenda:
        assert entry["also_at"] == [f"{BASE}/mall_09.html"]


def test_different_pages_with_the_same_extracted_offers_never_merge() -> None:
    """抽取結果相同但活動頁標題不同 → 是不同活動，不得合併。

    實測第一銀行「家電分期禮─全國電子／大同3C／三井3C」是三個不同零售商的
    活動頁，零售商名字只在頁面標題裡，切分後的子活動標題與期間、條件全部相同。
    合併掉會讓使用者永遠看不到大同與三井。聯邦「屈臣氏」與「康是美」同理。
    """
    campaigns = [
        _campaign(
            f"c{index}",
            f"{BASE}/appliance_{index}",
            _offer(f"a{index}", title="【家電分期禮】分期零利率 最高再享2,500元刷卡金"),
            _offer(f"b{index}", title="【家電分期禮】特店交易"),
            title=title,
        )
        for index, title in enumerate(
            ("家電分期禮─全國電子", "家電分期禮─大同3C", "家電分期禮─三井3C")
        )
    ]
    result, report = dedupe_campaigns(campaigns)

    assert len(result) == 3
    assert report.merged_offers == 0
    assert all(not offer.also_at for campaign in result for offer in campaign.offers)


def test_mirror_pages_still_merge_when_the_title_matches() -> None:
    """真鏡射的標題本來就一樣 —— 星展六個子頁全叫「分期0%利率」。"""
    campaigns = [
        _campaign(
            "c1",
            f"{BASE}/mall_08.html",
            _offer("a1", title="【網購星精彩】"),
            _offer("b1", title="【博客來】"),
            title="分期0%利率",
        ),
        _campaign(
            "c2",
            f"{BASE}/mall_09.html",
            _offer("a2", title="【網購星精彩】"),
            _offer("b2", title="【博客來】"),
            title="分期0%利率",
        ),
    ]
    result, report = dedupe_campaigns(campaigns)

    assert len(result) == 1
    assert report.merged_offers == 2
    assert result[0].offers[0].also_at == [f"{BASE}/mall_09.html"]
