"""上海商銀擷取器：以精簡官方回應回放 discover／terms。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from radar.banks.scsb import ScsbAdapter
from radar.transport import Response

LISTING = (Path(__file__).parents[1] / "fixtures/scsb/hotList.html").read_text("utf-8")
CARD = "https://www.scsb.com.tw/content/card/"


class FakeHttp:
    def __init__(self, extra: dict[str, str] | None = None) -> None:
        self.extra = extra or {}

    def get(self, url: str, **_: object) -> Response:
        return Response(url, url, 200, self.extra.get(url, LISTING), "text/html", "")


def test_discover_reads_all_cards() -> None:
    stubs = ScsbAdapter(FakeHttp()).discover()  # type: ignore[arg-type]

    assert len(stubs) == 24
    assert len({s.key for s in stubs}) == 24
    assert [s.title for s in stubs[:3]] == [
        "持Teresa卡當月一般消費滿額，享指定餐廳滿兩千送200",
        "線上投保汽機車任意險最高享86折，旅遊班機延誤最高賠2.4萬",
        "KKday全站消費刷上海商銀卡，滿額最高折500!",
    ]
    by_url = {s.url: s for s in stubs}
    teresa = by_url[f"{CARD}hotPage/5422"]
    assert (teresa.period_start, teresa.period_end) == (date(2026, 10, 5), date(2026, 12, 31))
    assert teresa.category == "卡片優惠"
    # 舊版 news_*.html 明細也收
    assert f"{CARD}news_230320.html" in by_url
    assert all(s.period_end is not None for s in stubs)


def test_terms_limited_to_right_column() -> None:
    url = f"{CARD}hotPage/5422"
    html = (
        "<html><body><div class='header'>個人金融 信用卡</div><div class='middleWrap'>"
        "<div class='leftCol'>熱門活動 卡片介紹</div><div class='rightCol'>"
        "<ol class='breadcrumb'><li>首頁</li></ol><div class='sharebar'>分享: 收藏</div>"
        "<h2>持Teresa卡當月一般消費滿額</h2><p>活動期間:2026/10/1-2026/12/31</p>"
        "<p>注意事項：正卡須完成活動登錄。</p></div></div><footer>版權</footer></body></html>"
    )
    adapter = ScsbAdapter(FakeHttp({url: html}))  # type: ignore[arg-type]
    stub = adapter.stub(key=url, title="Teresa", url=url)
    assert stub is not None
    terms = adapter.terms(stub)
    assert terms.text.startswith("持Teresa卡當月一般消費滿額")
    for noise in ("分享", "首頁", "熱門活動", "個人金融", "版權"):
        assert noise not in terms.text
    assert (terms.period_start, terms.period_end) == (date(2026, 10, 1), date(2026, 12, 31))
