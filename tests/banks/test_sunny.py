"""陽信銀行擷取器：以精簡官方回應（2026-11-01）回放。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from radar.banks.sunny import SunnyAdapter
from radar.transport import Response

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/sunny/listing.json").read_text("utf-8"))
BASE = "https://www.sunnybank.com.tw"
PAGES = {
    SunnyAdapter.entry_url: "entry_html",
    f"{BASE}/net/Main/Page/48": "page48_html",
    f"{BASE}/net/Main/Page/64": "page64_html",
    f"{BASE}/net/Main/Page/66": "page66_html",
}


class FakeHttp:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, **_: object) -> Response:
        self.urls.append(url)
        return Response(url, url, 200, FIXTURE[PAGES.get(url, "detail_html")], "text/html", "")


def test_discover_cards_and_sublists() -> None:
    http = FakeHttp()
    adapter = SunnyAdapter(http)  # type: ignore[arg-type]
    stubs = adapter.discover()

    # 主列表 39 張卡片中 29 個逐活動明細；熱門網購 +6、特店優惠（精簡為 2 張）+2；
    # 卡友日子列表與主列表重複，不重收
    assert len(stubs) == 37
    assert [s.title for s in stubs[:3]] == [
        "陽信一卡通聯名卡『呷好逗相報』餐飲最高5%回饋",
        "買日本藥妝首選 享最高10%回饋",
        "冬季旅遊攻略！刷陽信卡最高回饋3,000元",
    ]
    assert len({s.key for s in stubs}) == len(stubs)
    assert all(s.url.startswith(f"{BASE}/") for s in stubs)

    by_title = {s.title: s for s in stubs}
    first = by_title["鑫世界卡新卡首刷禮-陽信領航‧禮遇起航"]
    assert first.url == f"{BASE}/creditcard/wcardinfo/"  # 去掉 #first
    assert (first.period_start, first.period_end) == (date(2026, 7, 1), date(2026, 12, 31))
    assert by_title["旅遊就刷陽信JCB晶緻卡 全球訂房/機票享現折優惠"].period_start is None
    assert by_title["精選百貨 滿額送好禮"].period_end is None  # 卡片沒寫期間
    momo = by_title["11月momo購物刷陽信卡，最高回饋2,500元"]
    assert momo.url == f"{BASE}/net/Prg/ActNewsView/1493"
    assert momo.category == "熱門網購"
    assert (momo.period_start, momo.period_end) == (date(2026, 11, 1), date(2026, 11, 30))

    # 導覽入口不列，也不展開
    skipped = " ".join(adapter.skipped)
    for name in ("VISA優惠", "現金紅利點數兌換專區", "新一代 行動網銀APP", "公益活動", "Sunny Pay"):
        assert name in skipped
        assert name not in by_title
    assert f"{BASE}/net/Main/Page/39" not in http.urls


def test_terms_scoped_to_mainbody() -> None:
    adapter = SunnyAdapter(FakeHttp())  # type: ignore[arg-type]
    stub = next(s for s in adapter.discover() if s.url.endswith("/ActNewsView/1493"))
    terms = adapter.terms(stub)

    assert terms.title == "11月momo購物刷陽信卡，最高回饋2,500元"
    assert "登錄時間2026/11/25 10:00開放登錄至2026/11/30 23:59止" in terms.text
    assert (terms.period_start, terms.period_end) == (date(2026, 11, 1), date(2026, 11, 30))
    for noise in ("穩健、前瞻、專業、熱忱", "悠遊VISA金融卡", "回上頁"):
        assert noise not in terms.text
