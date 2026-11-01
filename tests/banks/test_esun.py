"""玉山銀行擷取器：回放兩頁 DiscountSearchResult。"""

from __future__ import annotations

import json
from pathlib import Path

from radar.banks.esun import EsunAdapter
from radar.transport import Response

PAGES = json.loads((Path(__file__).parents[1] / "fixtures/esun/listing.json").read_text("utf-8"))


class FakeHttp:
    def __init__(self) -> None:
        self.pages: list[str] = []

    def get(self, url: str, *, data: dict[str, str] | None = None, **_: object) -> Response:
        assert data is not None and data["category"] == ""  # 不分類＝全部
        self.pages.append(data["currentPage"])
        return Response(url, url, 200, PAGES[data["currentPage"]], "text/html", "")


def test_discover_pages_and_drops_non_activities() -> None:
    http = FakeHttp()
    adapter = EsunAdapter(http)  # type: ignore[arg-type]
    stubs = adapter.discover()

    # 總數 18、每頁 9 筆 → 讀兩頁；第二頁的教學、點數查詢、數位服務、信貸 4 張卡不收
    assert http.pages == ["1", "2"]
    assert len(stubs) == 14
    assert [s.title for s in stubs[:3]] == [
        "新光三越卡利High兌領專區", "玉山信用卡綁定全支付", "百貨週年慶",
    ]
    assert stubs[1].url == (
        "https://www.esunbank.com/zh-tw/personal/credit-card/discount/shopInfo?sno=5100"
    )
    assert stubs[1].summary == "繳稅費享3%全點回饋"
    titles = {s.title for s in stubs}
    excluded = {"點數兌換線上查詢", "玉山Wallet即享券使用教學", "卡友數位服務", "精選信貸專屬方案"}
    assert not titles & excluded
    assert all(s.period_end is None for s in stubs)  # 列表沒有「活動期間」就不猜
