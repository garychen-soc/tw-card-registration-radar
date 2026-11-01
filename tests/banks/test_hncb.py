"""華南銀行擷取器：以精簡官方回應（2026-11-01）回放。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from radar.banks.hncb import HncbAdapter
from radar.transport import Response

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/hncb/listing.json").read_text("utf-8"))
TRAVEL = (
    "https://www.hncb.com.tw/wps/portal/HNCB/card/?1dmy&urile=wcm%3apath%3a"
    "/wps/wcm/connect/hncb_v2/site_map/hncb/card/event/channel/travel"
)


class FakeHttp:
    def get(self, url: str, **_: object) -> Response:
        if url == HncbAdapter.entry_url:
            final = FIXTURE["entry_url_final"]
            return Response(url, final, 200, FIXTURE["entry_html"], "text/html", "")
        return Response(url, url, 200, FIXTURE["detail_html"], "text/html", "")


def test_discover_only_credit_card_panel_own_details() -> None:
    adapter = HncbAdapter(FakeHttp())  # type: ignore[arg-type]
    stubs = adapter.discover()

    # 信用卡面板 41 張卡片：13 張本行活動明細；外站、卡別禮遇導覽、PDF DM、服務頁不列
    assert len(stubs) == 13
    assert [s.title for s in stubs[:3]] == [
        "華南熊Q卡 熊挺你的神隊友",
        "華南大玩家 天涼秋爽冬好遊",
        "台灣Pay信用卡繳「地價稅」華南超回饋",
    ]
    assert all(s.url.startswith("https://www.hncb.com.tw/wps/portal/HNCB/") for s in stubs)
    assert len({s.key for s in stubs}) == len(stubs)
    # 存款/外匯分頁的卡片不得混入
    assert not any("存款" in s.title or "定存" in s.title for s in stubs)
    # 旅遊主題頁被 4 張卡片共用：各回一個 Stub，key 帶標題保持唯一
    travel = [s for s in stubs if s.url == TRAVEL]
    assert len(travel) == 4
    assert all(s.key.startswith(TRAVEL + "#") for s in travel)
    # 列表沒有期間
    assert all(s.period_start is None and s.period_end is None for s in stubs)

    reasons = " ".join(adapter.skipped)
    assert "外站：VISA無限卡獨家饗宴" in reasons
    assert "卡別禮遇導覽頁（只列外站連結）：MasterCard世界卡 尊榮禮獻" in reasons
    assert "PDF 型 DM" in reasons and "服務介紹頁：立即申請綁定LINE個人化服務" in reasons
    assert "外站：線上申請轉換記名式悠遊聯名卡(無須換卡)" in reasons  # netbank 子網域


def test_terms_scoped_to_page_content() -> None:
    adapter = HncbAdapter(FakeHttp())  # type: ignore[arg-type]
    stub = next(s for s in adapter.discover() if s.url.endswith("TWPAY_115tax_LADN"))
    terms = adapter.terms(stub)

    assert terms.title == "台灣Pay信用卡繳「地價稅」華南超回饋"
    assert terms.text.startswith("台灣Pay信用卡繳「地價稅」華南超回饋\n活動期間")
    assert "登錄請按此" in terms.text
    assert (terms.period_start, terms.period_end) == (date(2026, 11, 1), date(2026, 11, 30))
    for noise in ("網銀登入", "主選單", "服務據點", "首頁", "回到頁首", "以下為重要訊息"):
        assert noise not in terms.text
