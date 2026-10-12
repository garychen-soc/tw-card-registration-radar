"""HTML parser migration contracts: text, scope, links and downstream safety."""

from datetime import UTC, date, datetime

import pytest
from conftest import FakeFetcher

from radar.adapters.listing import read_listing
from radar.emit import dedupe_campaigns
from radar.guard import assess
from radar.htmltext import links, scope_html, to_text
from radar.runner import run_source
from radar.spec import SourceSpec


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            "<h1>回饋 &amp; 加碼</h1><p>活動期間：2026/8/1~2026/8/31</p>"
            "<p>登錄期間：2026/8/7 17:00~2026/8/20 23:59</p>",
            "回饋 & 加碼\n活動期間：2026/8/1~2026/8/31\n"
            "登錄期間：2026/8/7 17:00~2026/8/20 23:59",
        ),
        ("<p>繁體中文<strong>加碼</strong><br>限量&nbsp;100名</p>",
         "繁體中文\n加碼\n限量\xa0100名"),
        ("<table><tr><td>活動期間<td>2026/8/1~2026/8/31</table>",
         "活動期間\n2026/8/1~2026/8/31"),
        ("", ""),
    ],
)
def test_text_preserves_entities_unicode_and_line_boundaries(html: str, expected: str) -> None:
    assert to_text(html) == expected


def test_invisible_and_nested_boilerplate_dates_are_removed() -> None:
    html = """
    <html><body><script>活動期間：2099/1/1~2099/12/31</script>
    <style>.notice { content: '登錄期間：2099/1/1'; }</style>
    <noscript>登錄期間：2099/1/1</noscript>
    <template><p>登錄期間：2099/1/1</p></template>
    <nav><div class="menu"><a href="/register">活動登錄</a></div></nav>
    <div class="cookie-notice"><span class="cookie-message">Cookie 告知</span></div>
    <main><h1>信用卡加碼</h1><p>活動期間：2026/8/1~2026/8/31</p></main>
    <footer>登錄期間：2099/1/1</footer></body></html>
    """
    assert to_text(html) == "信用卡加碼\n活動期間：2026/8/1~2026/8/31"


def test_scope_and_link_attributes_keep_the_official_card_panel() -> None:
    html = """
    <a aria-label="信用卡" aria-controls="cards">卡友優惠</a>
    <section id="loans"><a href="/loan">貸款</a></section>
    <section id="cards">
      <a href="/wrong" data-link="/register?campaign=1&amp;channel=card">登錄 &amp; 回饋</a>
      <a href="detail/2">活動二</a><a>沒有網址</a>
    </section>
    """
    scoped = scope_html(html, tab_label="信用卡")
    expected = [
        ("https://example.com/register?campaign=1&channel=card", "登錄 & 回饋"),
        ("https://example.com/promos/detail/2", "活動二"),
    ]
    assert links(scoped, "https://example.com/promos/list") == expected
    assert links(scope_html(html, selector="#cards"), "https://example.com/promos/list") == expected
    assert scope_html(html, selector="#missing") == html
    assert scope_html(html, tab_label="不存在") == html


def test_html_listing_to_offers_dedupe_and_guard_contract() -> None:
    spec = SourceSpec.model_validate({
        "id": "demo", "bank_name": "示範銀行", "domains": ["example.com"],
        "listing": {
            "kind": "html_list", "entry_url": "https://example.com/list",
            "item_selector": "li.card", "link_pattern": r'href="(/promo/[^"?#]+)"',
            "title_selector": "h3", "summary_selector": ".summary",
        },
        "detail": {"cardinality": "many"},
    })
    card = ('<li class="card"><a href="/promo/{slug}">詳情</a>'
            '<h3>回饋 &amp; 加碼</h3><p class="summary">最高 5%</p>'
            '<p>2026.08.01~2026.08.31</p></li>')
    detail = """
    <h1>回饋 &amp; 加碼</h1><p>【活動一】網購回饋 活動期間：2026/8/1~2026/8/31</p>
    <p>登錄期間：2026/8/7 17:00~2026/8/20 23:59 開放登錄</p>
    <p>單筆滿10,000元享5%回饋，限量登錄1,000名，限正卡人登錄。</p>
    <p>【活動二】百貨回饋 活動期間：2026/8/1~2026/8/31</p>
    <p>單筆滿20,000元享3%回饋，限量登錄500名，限正卡人登錄。</p>
    """
    fetcher = FakeFetcher(pages={
        "https://example.com/list": "<ul>" + "".join(
            card.format(slug=slug) for slug in ("1", "1", "1_copy")
        ) + "</ul>",
        "https://example.com/promo/1": detail,
        "https://example.com/promo/1_copy": detail,
    })
    items = read_listing(spec, fetcher)
    assert len(items) == 2
    assert items[0].title == "回饋 & 加碼"
    assert items[0].summary == "最高 5%"
    assert (items[0].start, items[0].end) == (date(2026, 8, 1), date(2026, 8, 31))

    result = run_source(spec, fetcher, today=date(2026, 8, 4),
                        now=datetime(2026, 8, 4, tzinfo=UTC))
    assert result.health is not None and result.health.status == "complete"
    assert result.health.offer_count == 4
    campaigns, report = dedupe_campaigns(result.campaigns)
    assert len(campaigns) == 1 and report.merged_offers == 2
    offer = campaigns[0].offers[0]
    assert (offer.period.start, offer.period.end) == (date(2026, 8, 1), date(2026, 8, 31))
    assert offer.registration.windows[0].start == datetime.fromisoformat("2026-08-07T17:00+08:00")
    assert offer.registration.windows[0].end == datetime.fromisoformat("2026-08-20T23:59+08:00")
    assert offer.conditions.quota.seats == 1000
    assert offer.conditions.eligibility.primary_card_only
    assert offer.also_at == ["https://example.com/promo/1_copy"]

    previous = {"counts": {"offers": 4}, "sources": [{"bank_id": "demo", "offer_count": 4}]}
    assert not assess(health=[result.health], current_offers=4, previous_index=previous).blocked
    previous = {"counts": {"offers": 10}, "sources": [{"bank_id": "demo", "offer_count": 10}]}
    guard = assess(health=[result.health], current_offers=4, previous_index=previous)
    assert guard.blocked and guard.published_snapshot_preserved
    assert "per_source_coverage_regression" in guard.reason_codes
