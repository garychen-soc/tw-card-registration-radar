"""台北富邦擷取器：以官方清單頁＋Wicket Ajax 回應回放 discover。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from radar.banks.taipei_fubon import TaipeiFubonAdapter
from radar.transport import Response

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures/taipei_fubon/listing.json").read_text("utf-8")
)
# 狀態遺失時 Wicket 回的 redirect（線上實測 139 bytes 的回應格式）
REDIRECT = (
    '<?xml version="1.0" encoding="UTF-8"?><ajax-response><redirect><![CDATA['
    "../wicket/bookmarkable/org.apache.wicket.markup.html.pages.AccessDenied"
    "]]></redirect></ajax-response>"
)


class FakeHttp:
    def __init__(self, ajax: list[str]) -> None:
        self.ajax = ajax
        self.result_loads = 0
        self.ajax_headers: list[dict[str, str]] = []

    def get(self, url: str, *, headers: dict[str, str] | None = None, **_: object) -> Response:
        if url.endswith("/promotion/Result"):
            self.result_loads += 1
            text = FIXTURE["result_html"]
        else:
            assert url.endswith("-nav-next"), url
            self.ajax_headers.append(headers or {})
            text = self.ajax.pop(0) if self.ajax else REDIRECT
        return Response(url, url, 200, text, "text/xml", "")


def test_discover_pages_recovers_from_lost_state_and_reconciles_total() -> None:
    http = FakeHttp([FIXTURE["ajax_page2"], REDIRECT, FIXTURE["ajax_page3"]])
    adapter = TaipeiFubonAdapter(http)  # type: ignore[arg-type]
    stubs = adapter.discover()

    # 第 1 頁 9＋第 2 頁 9＋第 3 頁 9，F000002 跨頁重複 → 26 筆
    assert len(stubs) == 26
    assert [s.title for s in stubs[:3]] == [
        "Costco聯名卡分期優惠", "富邦卡海外消費單筆分期優惠", "momo卡11月店內活動",
    ]
    costco = stubs[0]
    assert costco.key == "F000018"
    assert costco.url == "https://cardpromote.taipeifubon.com.tw/promotion/Detail?sn=F000018"
    assert (costco.period_start, costco.period_end) == (date(2026, 1, 1), date(2026, 12, 31))
    assert http.result_loads > 1  # redirect 後有重開清單頁
    assert all(h.get("Referer") and h.get("Wicket-Ajax") == "true" for h in http.ajax_headers)
    # 沒收齊公告的 175 筆就要明講
    assert adapter.skipped == ["分頁未收齊：收到 26／公告 175 筆"]
