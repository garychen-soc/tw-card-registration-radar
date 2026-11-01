"""中國信託擷取器：以精簡官方回應回放 discover／terms。"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from radar.banks.ctbc import LINEPAY, TAIPEI, CtbcAdapter
from radar.transport import AccessDenied, Response

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/ctbc/listing.json").read_text("utf-8"))


class FakeHttp:
    def __init__(self, pages: dict[str, tuple[int, str]] | None = None) -> None:
        self.pages = pages or {}

    def get(self, url: str, **_: object) -> Response:
        if url in self.pages:
            status, text = self.pages[url]
        elif url == CtbcAdapter.entry_url:
            status, text = 200, json.dumps(FIXTURE["offerdata"], ensure_ascii=False)
        elif url == f"{LINEPAY}page_food.html":
            status, text = 200, FIXTURE["linepay_food"]
        else:
            status, text = 200, "<html><body></body></html>"
        return Response(url, url, status, text, "text/html", "")


class Fixed(CtbcAdapter):
    def now(self) -> datetime:
        return datetime(2026, 11, 1, 12, tzinfo=TAIPEI)


def test_discover_reads_offer_json_and_linepay_cards() -> None:
    adapter = Fixed(FakeHttp())  # type: ignore[arg-type]
    stubs = adapter.discover()

    # 優惠 JSON：14 筆 cc_offer 顯示中＋雄獅 60 週年（/creditcard/ 路徑）＋EZTABLE 兩筆
    # 60 週年別名；存款／信貸／網銀不收。LINE Pay 美食頁 14 張卡，4 張連外站不收。
    assert len(stubs) == 27
    assert len({s.key for s in stubs}) == 27
    assert [s.title for s in stubs[:3]] == ["板橋凱薩大飯店", "foodpanda", "Uber Eats"]
    assert stubs[10].title == "【耆妙屋】指定商品9折優惠"
    assert not any("定存" in s.title or "信貸" in s.title for s in stubs)
    assert len(adapter.skipped) == 4 and all("LINE Pay 支付" in x for x in adapter.skipped)

    by_title = {s.title: s for s in stubs}
    rental = by_title["【中租租車】刷中信卡訂車，享平日7折、假日8折優惠"]
    assert rental.url == "https://www.ctbcbank.com/content/dam/minisite/long/creditcard/rental/index.html"
    assert (rental.period_start, rental.period_end) == (date(2026, 1, 1), date(2026, 12, 31))

    sushiro = by_title["SUSHIRO 壽司郎"]
    assert sushiro.url == f"{LINEPAY}page_food.html#lightbox_sushiro"
    assert (sushiro.period_start, sushiro.period_end) == (date(2026, 7, 1), date(2026, 12, 31))


def test_offers_outside_display_window_are_dropped() -> None:
    class Later(CtbcAdapter):
        def now(self) -> datetime:
            return datetime(2027, 1, 1, tzinfo=TAIPEI)

    titles = {s.title for s in Later(FakeHttp()).discover()}  # type: ignore[arg-type]
    assert "【線上購物】雙11刷中信卡，最高現折5600！" not in titles  # offTime 2026/11/30
    assert "【丹普床墊】購買指定系列床墊可享現金折抵2,000元" in titles


def test_terms_scopes_lightbox_and_refuses_js_challenge() -> None:
    page = f"{LINEPAY}page_food.html"
    html = (
        "<html><body><nav>回首頁 信用卡 存款</nav><div class='lightbox' id='lightbox_a'>"
        "<h3><img alt='A店，享10%'></h3><h4>A店・注意事項</h4><ol><li>活動期間：2026/7/1~2026/12/31"
        "，每月6號開放登錄。</li></ol></div><footer>版權所有</footer></body></html>"
    )
    mkt = "https://mkt.ctbcbank.com/long/creditcard/parking/index.html"
    adapter = Fixed(FakeHttp({page: (200, html), mkt: (202, "<script>challenge</script>")}))  # type: ignore[arg-type]
    stub = adapter.stub(key="a", title="A店", url=f"{page}#lightbox_a")
    assert stub is not None
    terms = adapter.terms(stub)
    assert terms.text.startswith("A店・注意事項")
    assert "回首頁" not in terms.text and "版權所有" not in terms.text
    assert terms.title == "A店，享10%"
    assert terms.period_end == date(2026, 12, 31)

    blocked = adapter.stub(key="b", title="停車", url=mkt)
    assert blocked is not None
    with pytest.raises(AccessDenied):
        adapter.terms(blocked)
