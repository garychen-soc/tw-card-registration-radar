"""星展銀行擷取器：以精簡官方回應回放 discover／terms。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from radar.banks.dbs import DbsAdapter
from radar.transport import Response

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/dbs/listing.json").read_text("utf-8"))
ROUTES = {
    "cards-offers/default.page": "entry",
    "offers/ce/default.html": "mall",
    "offers/installment/default.html": "installment",
    "offers/department_store/default.html": "department",
}
OFFERS = "https://www.dbs.com.tw/personal-zh/cards/offers/"


class FakeHttp:
    def __init__(self, extra: dict[str, str] | None = None) -> None:
        self.extra = extra or {}
        self.calls: list[str] = []

    def get(self, url: str, **_: object) -> Response:
        self.calls.append(url)
        text = self.extra.get(url) or next(
            FIXTURE[name] for suffix, name in ROUTES.items() if url.endswith(suffix))
        return Response(url, url, 200, text, "text/html", "")


def test_discover_expands_hubs_with_distinct_store_names() -> None:
    http = FakeHttp()
    adapter = DbsAdapter(http)  # type: ignore[arg-type]
    stubs = adapter.discover()

    assert len(stubs) == 133
    assert len({s.key for s in stubs}) == 133
    assert [s.title for s in stubs[:3]] == [
        "星願集刷 回饋升級 刷星展卡享最高3%回饋",
        "探索世界刷星展 滿額最高解鎖RIMOWA",
        "摘星假期 星級享受 國內旅遊享最高5%回饋",
    ]
    # 每個清單頁只讀一次（精選與分期頁都連到網購／百貨）
    assert len(http.calls) == len(set(http.calls)) == 4

    # 網購六個「分期0%利率」頁不再共用標題：取清單卡片上的商店名
    mall = [s.title for s in stubs if "/ce/mall_08" in s.url]
    assert {"生活市集", "指定電商"} <= set(mall)
    assert len(set(mall)) == len(mall)
    by_title = {s.title: s for s in stubs}
    honda = by_title["Honda"]
    assert honda.url == f"{OFFERS}2026honda/index.html"
    assert (honda.period_start, honda.period_end) == (date(2026, 10, 1), date(2026, 11, 30))

    # 清單頁本身、攻略與文章不是活動；百貨三家只有 logo、無標題
    urls = {s.url for s in stubs}
    assert f"{OFFERS}ce/default.html" not in urls
    assert f"{OFFERS}cardguide/index.html" not in urls
    assert sum(x.startswith("無標題") for x in adapter.skipped) == 3


def test_terms_limited_to_main_content() -> None:
    url = f"{OFFERS}poya/index.html"
    html = (
        "<html><head><title>POYA</title></head><body><header>個人網路銀行 企業網路銀行</header>"
        "<section class='main-content'><h1>綁定星展銀行信用卡</h1>"
        "<p>活動期間:2026/11/1~2026/12/31</p>"
        "<p>注意事項：每月限量2,000份。</p></section><div class='fix-ad'>立即辦卡</div>"
        "<footer>謹慎理財</footer></body></html>"
    )
    adapter = DbsAdapter(FakeHttp({url: html}))  # type: ignore[arg-type]
    stub = adapter.stub(key=url, title="POYA", url=url)
    assert stub is not None
    terms = adapter.terms(stub)
    assert terms.text.startswith("綁定星展銀行信用卡")
    assert "個人網路銀行" not in terms.text and "立即辦卡" not in terms.text
    assert (terms.period_start, terms.period_end) == (date(2026, 11, 1), date(2026, 12, 31))
