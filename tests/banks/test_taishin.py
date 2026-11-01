"""台新擷取器：以精簡官方回應（D 百貨購物分類 2 頁）回放 discover。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from radar.banks.taishin import TaishinAdapter
from radar.transport import FetchFailed, Response

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures/taishin/listing.json").read_text("utf-8")
)


class FakeHttp:
    def get(self, url: str, *, data: dict[str, str] | None = None, **_: object) -> Response:
        if url.endswith("/offerList/D"):
            text = FIXTURE["category_html"]
        elif data is not None and data["categoryId"] == "D":
            assert data["_csrf"], "要帶分類頁的 _csrf"
            text = json.dumps(FIXTURE["pages"][int(data["queryPage"]) - 1], ensure_ascii=False)
        else:
            raise FetchFailed(f"模擬其他分類讀不到：{url}")
        return Response(url, url, 200, text, "text/html", "")


def test_discover_pages_through_category() -> None:
    adapter = TaishinAdapter(FakeHttp())  # type: ignore[arg-type]
    stubs = adapter.discover()

    assert len(stubs) == 16  # 第 1 頁 12 筆＋第 2 頁 4 筆
    assert [s.title for s in stubs[:3]] == [
        "新光三越贈最高21%，週年慶刷台新信用卡",
        "2026新光三越夏天卡利HIGH兌領須知_憑簡訊兌領",
        "【新新券】新光三越加贈最高10%，週年慶刷台新新光三越聯名卡",
    ]
    first = stubs[0]
    assert first.url == (
        "https://mkpcard.taishinbank.com.tw/tscccms/promotion/detail/WM_20260814165339540"
    )
    # UTC 2026-09-30T16:00Z＝台北 10/1；2026-12-13T15:59:59Z＝台北 12/13
    assert (first.period_start, first.period_end) == (date(2026, 10, 1), date(2026, 12, 13))
    assert first.category == "百貨購物"
    # 其餘 8 個分類失敗各記一筆，供判定 partial
    assert len(adapter.skipped) == 8
    assert all(item.startswith("分類失敗：") for item in adapter.skipped)
