"""彰化銀行擷取器：以精簡官方回應（總頁＋四分類）回放 discover／terms。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from radar.banks.chb import ChbAdapter
from radar.transport import Response

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/chb/listing.json").read_text("utf-8"))
DETAIL = "https://www.bankchb.com/frontend/bonusDetail.jsp?id="


class FakeHttp:
    def __init__(self, extra: dict[str, str] | None = None) -> None:
        self.extra = extra or {}

    def get(self, url: str, **_: object) -> Response:
        text = self.extra.get(url) or FIXTURE[url.rsplit("=", 1)[1]]
        return Response(url, url, 200, text, "text/html", "")


def test_discover_reads_all_four_categories() -> None:
    stubs = ChbAdapter(FakeHttp()).discover()  # type: ignore[arg-type]

    assert len(stubs) == 66
    assert len({s.key for s in stubs}) == 66
    counts: dict[str, int] = {}
    for stub in stubs:
        counts[stub.category] = counts.get(stub.category, 0) + 1
    assert counts == {"美食饗宴": 4, "購物消費": 24, "旅遊住宿": 16, "其他優惠": 22}
    assert [s.title for s in stubs[:3]] == [
        "彰化銀行 x 王品 一起瘋美食",
        "曾記麻糬享優惠 彰銀刷卡省5%",
        "刷Visa商旅御璽Fun心卡，日日好飲最高享3%回饋！",
    ]
    by_url = {s.url: s for s in stubs}
    # 官方頁寫成內網位址 http://10.100.6.38/...?id=3594 的連結改回官網同一支程式
    assert by_url[f"{DETAIL}3594"].title == "【保險/稅費】年中繳保費，限量分期荷包更輕鬆"
    assert all(s.url.startswith(DETAIL) for s in stubs)
    assert f"{DETAIL}3657" not in by_url and f"{DETAIL}3646" not in by_url


def test_terms_scoped_and_empty_when_withdrawn() -> None:
    live, gone = f"{DETAIL}3729", f"{DETAIL}3720"
    menu = "<div class='header'><nav>個人金融 信用卡 優惠訊息</nav></div>"
    html = (
        f"<html><head><title>彰化銀行-優惠訊息-美廉社</title></head><body>{menu}"
        "<div class='content-Ri'><a>:::</a><div class='content-inside'><h3>美廉社用台灣Pay</h3>"
        "<p>活動期間:2026年9月24日至2026年10月26日</p><p>注意事項：須完成活動登錄。</p>"
        "</div></div><div class='footer'>客服</div></body></html>"
    )
    empty = f"<html><head><title>彰化銀行-優惠訊息-</title></head><body>{menu}</body></html>"
    adapter = ChbAdapter(FakeHttp({live: html, gone: empty}))  # type: ignore[arg-type]
    stub = adapter.stub(key=live, title="美廉社", url=live)
    assert stub is not None
    terms = adapter.terms(stub)
    assert terms.text.startswith("美廉社用台灣Pay")
    assert "個人金融" not in terms.text and ":::" not in terms.text
    assert terms.title == "美廉社"
    assert (terms.period_start, terms.period_end) == (date(2026, 9, 24), date(2026, 10, 26))

    withdrawn = adapter.stub(key=gone, title="十月寵愛", url=gone)
    assert withdrawn is not None
    assert adapter.terms(withdrawn).text == ""  # 下架頁不退回整頁選單
