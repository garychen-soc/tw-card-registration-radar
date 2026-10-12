"""Bank-specific listing regressions using the actual declarative source specs."""

from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from conftest import FakeFetcher

from radar.adapters.listing import read_listing
from radar.runner import run_source
from radar.spec import load_spec
from radar.transport import AccessDenied

SOURCES = Path(__file__).resolve().parents[1] / "sources"
SUNNY_DETAIL = "https://www.sunnybank.com.tw/activityevent/2026Q4JCBdrugstore/web.html"
SUNNY_LIST = """
<nav><a href="https://www.sunnybank.com.tw/activityevent/not-an-offer/web.html">導覽</a></nav>
<div class="txt"><div class="row ad-link">
  <div class="card">
    <a href="https://www.sunnybank.com.tw/activityevent/2026Q4JCBdrugstore/web.html">圖片</a>
    <h5 class="card-title">買日本藥妝首選 &amp; 回饋</h5>
    <p class="card-text"><a href="https://www.sunnybank.com.tw/activityevent/2026Q4JCBdrugstore/web.html">
      活動期間:115/10/1-115/12/31</a></p>
  </div>
  <div class="card"><a href="/net/Prg/ActNewsView/1940">圖片</a>
    <h5 class="card-title">陽信卡友日</h5><p class="card-text">活動期間:115/1/1-115/12/31</p>
  </div>
  <div class="card"><a href="/net/Main/Page/39">VISA 優惠導覽</a></div>
  <div class="card"><a href="https://www.sunnybank.com.tw/activityevent/202010-sunnybank_app/web.html">
    行動網銀 App</a></div>
  <div class="card"><a href="https://example.com/activityevent/external/web.html">外站</a></div>
</div></div>
"""


def test_hncb_short_entry_preserves_the_wcm_content_selector() -> None:
    spec = load_spec(SOURCES / "hncb.toml")
    url = urlsplit(spec.listing.entry_url)
    assert url.path == "/wps/portal/HNCB/card/"
    query = parse_qs(url.query, keep_blank_values=True)
    assert "1dmy" in query
    assert query["urile"] == [
        "wcm:path:/wps/wcm/connect/hncb_v2/site_map/hncb/per_finance/service/public_item/best_offer"
    ]


def test_hncb_listing_stays_in_the_card_panel() -> None:
    spec = load_spec(SOURCES / "hncb.toml")
    path = "/wps/portal/HNCB/msg/co-management/pb-best-offer/c1/t5/TWPAY_115tax_LADN"
    html = f"""
    <a aria-label="信用卡" aria-controls="cards">信用卡</a>
    <section id="deposits"><a href="{path}" title="連結至存款">存款</a></section>
    <section id="cards"><a href="{path}" title="連結至台灣Pay繳地價稅">詳情</a>
      <a href="https://example.com/offer" title="連結至外站">外站</a>
      <a href="?1dmy&amp;urile=category" title="連結至分類">分類</a>
    </section>
    """
    items = read_listing(spec, FakeFetcher(pages={spec.listing.entry_url: html}))
    assert len(items) == 1
    assert items[0].url == "https://www.hncb.com.tw" + path
    assert items[0].title == "台灣Pay繳地價稅"


def test_sunny_listing_reads_individual_details_and_deduplicates_card_links() -> None:
    spec = load_spec(SOURCES / "sunny.toml")
    items = read_listing(spec, FakeFetcher(pages={spec.listing.entry_url: SUNNY_LIST}))
    assert [item.url for item in items] == [
        SUNNY_DETAIL, "https://www.sunnybank.com.tw/net/Prg/ActNewsView/1940",
    ]
    assert items[0].title == "買日本藥妝首選 & 回饋"
    assert (items[0].start, items[0].end) == (date(2026, 10, 1), date(2026, 12, 31))


def test_sunny_registration_is_read_from_the_detail_not_the_listing_menu() -> None:
    spec = load_spec(SOURCES / "sunny.toml")
    html = SUNNY_LIST.replace(
        '<div class="card"><a href="/net/Prg/ActNewsView/1940">圖片</a>',
        '<div class="card"><a href="/net/Main/Page/39">導覽</a>',
    )
    detail = """
    <h1>日本藥妝回饋</h1><p>活動期間：115/10/1~115/12/31</p>
    <p>登錄期間：115/10/15 15:00~115/10/31 23:59 開放登錄</p>
    <p>單筆滿6,000元享300元刷卡金，限量登錄60名，限信用卡正卡人。</p>
    """
    fetcher = FakeFetcher(pages={spec.listing.entry_url: html, SUNNY_DETAIL: detail})
    result = run_source(spec, fetcher, today=date(2026, 10, 13),
                        now=datetime(2026, 10, 13, tzinfo=UTC))
    assert result.health is not None and result.health.status == "complete"
    assert fetcher.requested == [spec.listing.entry_url, SUNNY_DETAIL]
    assert len(result.campaigns) == 1
    offer = result.campaigns[0].offers[0]
    assert (offer.period.start, offer.period.end) == (date(2026, 10, 1), date(2026, 12, 31))
    assert offer.registration.windows[0].start == datetime.fromisoformat("2026-10-15T15:00+08:00")
    assert offer.registration.windows[0].end == datetime.fromisoformat("2026-10-31T23:59+08:00")
    assert offer.conditions.quota.seats == 60


def test_sunny_cloud_access_denial_does_not_become_an_empty_campaign() -> None:
    spec = load_spec(SOURCES / "sunny.toml")
    fetcher = FakeFetcher(failures={spec.listing.entry_url: AccessDenied("HTTP 403")})
    result = run_source(spec, fetcher, today=date(2026, 10, 13))
    assert result.campaigns == []
    assert result.health is not None and result.health.status == "blocked"
    assert result.alerts[0].type == "source_access_blocked"
