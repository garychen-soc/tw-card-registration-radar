"""第一銀行擷取器：以精簡官方回應回放 discover。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from radar.banks.first import FirstAdapter
from radar.transport import FetchFailed, Response

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/first/listing.json").read_text("utf-8"))


class FakeHttp:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def get(self, url: str, *, data: dict[str, str] | None = None, **_: object) -> Response:
        if data is None:
            text = FIXTURE["entry_html"]
        else:
            self.posts.append(data["categoryEnName"])
            if data["categoryEnName"] not in FIXTURE["rest"]:
                raise FetchFailed("模擬分類暫時讀不到")
            text = json.dumps(FIXTURE["rest"][data["categoryEnName"]], ensure_ascii=False)
        return Response(url, url, 200, text, "text/html", "")


def test_discover_reads_categories_and_marks_failed_one() -> None:
    http = FakeHttp()
    adapter = FirstAdapter(http)  # type: ignore[arg-type]
    adapter.retry_delay = 0
    stubs = adapter.discover()

    # 線上購物 13 筆＋百貨總表 3 筆；「其他」分類失敗不拖垮其餘分類
    assert len(stubs) == 16
    assert [s.title for s in stubs[:3]] == ["momo購物網", "蝦皮購物", "coupang酷澎"]
    assert adapter.skipped and adapter.skipped[0].startswith("分類失敗：其他")
    assert http.posts.count("activity_other") == adapter.category_attempts  # 有重試
    assert "all_category" not in http.posts

    by_title = {s.title: s for s in stubs}
    pchome = by_title["PChome線上購物"]
    assert pchome.url == "https://card.firstbank.com.tw/sites/card/zh_TW/1565701869556"
    assert (pchome.period_start, pchome.period_end) == (date(2026, 7, 1), date(2026, 12, 31))
    assert pchome.category == "線上購物"
    # 百貨總表是常駐頁，activityDate 的 2024 舊值不當期間
    assert by_title["百貨購物總表"].period_end is None
    assert by_title["momo購物網"].period_end is None  # API 沒給期間就留空


def test_all_categories_failing_raises() -> None:
    class Down(FakeHttp):
        def get(self, url: str, *, data: dict[str, str] | None = None, **_: object) -> Response:
            if data is not None:
                raise FetchFailed("模擬全部分類讀不到")
            return super().get(url)

    adapter = FirstAdapter(Down())  # type: ignore[arg-type]
    adapter.retry_delay = 0
    with pytest.raises(FetchFailed):
        adapter.discover()
