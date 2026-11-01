"""台中銀行擷取器：回放 Promotion Entrance API（已去掉 base64 圖檔）。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from radar.banks.tcbbank import TcbbankAdapter
from radar.transport import Response

API = (Path(__file__).parents[1] / "fixtures/tcbbank/entrance.json").read_text("utf-8")


class FakeHttp:
    def get(self, url: str, **_: object) -> Response:
        assert "Promotion/Entrance" in url, f"不該抓 {url}"
        return Response(url, url, 200, API, "application/json", "")


def test_discover_titles_and_subtitle_deadline() -> None:
    adapter = TcbbankAdapter(FakeHttp())  # type: ignore[arg-type]
    stubs = adapter.discover()

    assert len(stubs) == 24  # 25 筆扣掉「累積紅利…7-11兌換」
    assert [s.title for s in stubs[:3]] == [
        "線上申辦JCB晶緻卡，晶選加碼禮",
        "新卡友首刷禮！",
        "現金回饋卡！不分國內外、不限金額，享0.8%回饋無上限(當期消費、當期回饋、刷越多賺越多！",
    ]
    by_title = {s.title: s for s in stubs}
    nov = by_title["網購雙十一購物節"]
    assert nov.url == "https://www.tcbbank.com.tw/Promotion/Info.html?ID=823"
    # 截止日取 SubTitle「(至115年11月30日有效)」；StartDate 是上架日不當活動起日
    assert (nov.period_start, nov.period_end) == (None, date(2026, 11, 30))
    assert not any("紅利" in s.title and "兌換" in s.title for s in stubs)


def test_image_only_page_returns_empty_terms_without_fetching() -> None:
    adapter = TcbbankAdapter(FakeHttp())  # type: ignore[arg-type]
    stub = next(s for s in adapter.discover() if "Info.html" in s.url)
    assert adapter.terms(stub).text == ""
