"""國泰世華擷取器：回放精簡的 credit-card.model.list.json。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from radar.banks.cathay import CathayAdapter
from radar.transport import Response

LIST = (Path(__file__).parents[1] / "fixtures/cathay/list.json").read_text("utf-8")
BASE = "https://www.cathay-cube.com.tw/cathaybk/personal/event/overview/credit-card"


class FakeHttp:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def get(self, url: str, **_: object) -> Response:
        return Response(url, url, 200, self.pages[url], "application/json", "")


def test_discover_titles_periods_and_exclusions() -> None:
    adapter = CathayAdapter(FakeHttp({CathayAdapter.list_url: LIST}))  # type: ignore[arg-type]
    stubs = adapter.discover()

    # 12 筆樣本：小樹點兌換／FAQ、生活繳費服務頁、捐款共 6 筆不是活動
    assert len(stubs) == 6
    assert [s.title for s in stubs[:3]] == [
        "日本大丸百貨福岡天神店刷CUBE信用卡領券消費享7.8%小樹點(信用卡)回饋無上限",
        "樹享券",
        "momo購物網 雙11嘉年華 (11/1~11/15)",
    ]
    first = stubs[0]
    # campaignPath 去掉 AEM 前綴與 .html；UTC 時間戳換成台北日期（+8h）
    assert first.url == f"{BASE}/shopping/202507/Daimaru2025H2"
    assert (first.period_start, first.period_end) == (date(2026, 1, 1), date(2026, 12, 31))
    titles = " ".join(s.title for s in stubs)
    assert "童樂匯" in titles  # bonus 路徑下的期間活動照收
    assert not any(w in titles for w in ("停車費", "FAQ", "捐款", "代扣繳", "航空里程"))


def test_terms_render_component_tree_once() -> None:
    model = {"title": "測試活動", ":items": {"root": {":items": {
        "header": {":type": "x/cub-experiencefragment", "text": "<p>頁首選單</p>"},
        "responsivegrid": {":itemsOrder": ["main"], ":items": {"main": {
            ":itemsOrder": ["crumb", "body", "tabs", "more", "notice"], ":items": {
                "crumb": {":type": "x/cub-breadcrumb", "items": [{"title": "國泰世華銀行"}]},
                "body": {":type": "x/cub-textb", "title": "活動內容",
                         "text": "<p>活動期間：2026/11/01 ~ 2026/11/30</p>"},
                "tabs": {":type": "x/cub-tabs",
                         "tabs": {":items": {"t": {"cq:panelTitle": "分頁一"}}},
                         ":itemsOrder": ["t"], ":items": {"t": {"cq:panelTitle": "分頁一",
                                                                 "text": "<p>分頁內容</p>"}}},
                "more": {":type": "x/cub-campaignpromotioncard",
                         "campaignPromotionMap": {"a": {"title": "其他活動"}}},
                "notice": {":type": "x/cub-notice", "title": "注意事項",
                           "text": "<ol><li>限正卡持卡人。</li></ol>"},
            }}}},
    }}}}
    url = f"{BASE}/x/test"
    adapter = CathayAdapter(FakeHttp({f"{url}.model.json": json.dumps(model)}))  # type: ignore[arg-type]
    stub = adapter.stub(key=url, title="測試", url=url)
    assert stub is not None
    text = adapter.terms(stub).text
    assert text.startswith("活動內容\n活動期間")
    assert text.count("分頁內容") == 1  # tabs 與 :items 重複的分頁只輸出一次
    assert "注意事項" in text and "限正卡持卡人" in text
    assert not any(w in text for w in ("頁首選單", "國泰世華銀行", "其他活動"))
