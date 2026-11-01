"""凱基銀行擷取器：以精簡官方回應（三個分頁）回放 discover／terms。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from radar.banks.kgi import KgiAdapter
from radar.transport import Response

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/kgi/listing.json").read_text("utf-8"))
BASE = "https://www.kgibank.com.tw/zh-tw/personal/promotion/card-campaign"


class FakeHttp:
    def __init__(self, extra: dict[str, str] | None = None) -> None:
        self.extra = extra or {}
        self.calls: list[str] = []

    def get(self, url: str, **_: object) -> Response:
        self.calls.append(url)
        text = self.extra.get(url) or FIXTURE[url.split("?p=")[1] if "?p=" in url else "1"]
        return Response(url, url, 200, text, "text/html", "")


def test_discover_follows_pagination() -> None:
    http = FakeHttp()
    stubs = KgiAdapter(http).discover()  # type: ignore[arg-type]

    assert http.calls == [BASE, f"{BASE}?p=2", f"{BASE}?p=3"]
    assert len(stubs) == 41  # 第 1 頁 18 筆＋第 2 頁 19 筆＋第 3 頁 4 筆
    assert len({s.key for s in stubs}) == 41
    assert [s.title for s in stubs[:3]] == [
        "兩廳院2026秋天藝術節 票券95折優惠",
        "指定影音/外送/搭用車/遊戲平台滿額最高賺300元刷卡金",
        "指定旅行社 / 訂房網 / 航空公司 最高4,500元刷卡金",
    ]
    by_title = {s.title: s for s in stubs}
    thsr = by_title["聰明轉高鐵TGo點 享受多一點"]
    assert thsr.url == f"{BASE}/thsr"
    # 卡片上的民國年期間
    assert (thsr.period_start, thsr.period_end) == (date(2026, 9, 1), date(2026, 11, 30))
    # 誠品兩張卡片指向同一明細，各自保留
    assert sum(s.url == f"{BASE}/eslite-a" for s in stubs) == 2


def test_terms_drops_share_bar_and_disclaimer() -> None:
    url = f"{BASE}/thsr"
    html = (
        "<html><body><header>個人金融 信用卡</header><main class='c-main'>"
        "<h1>聰明轉高鐵TGo點</h1><div class='kgibOther003__sharegroup'>分享至 Facebook Line</div>"
        "<p>活動期間:2026/9/1~2026/11/30</p><h3>活動注意事項</h3><p>1. 兌換後方符合抽獎資格。</p>"
        "<div class='bg-warm-white'><div>謹慎理財 信用至上</div></div></main>"
        "<footer>客服專線</footer></body></html>"
    )
    adapter = KgiAdapter(FakeHttp({url: html}))  # type: ignore[arg-type]
    stub = adapter.stub(key=url, title="高鐵", url=url)
    assert stub is not None
    terms = adapter.terms(stub)
    assert terms.text.startswith("聰明轉高鐵TGo點")
    assert "活動注意事項" in terms.text
    for noise in ("分享至", "謹慎理財", "個人金融", "客服專線"):
        assert noise not in terms.text
    assert terms.period_end == date(2026, 11, 30)
