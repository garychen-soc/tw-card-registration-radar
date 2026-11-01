"""聯邦銀行擷取器：回放 Rewards API。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from radar.banks.ubot import UbotAdapter
from radar.transport import Response

API = (Path(__file__).parents[1] / "fixtures/ubot/rewards.json").read_text("utf-8")
DELIVERY = "https://activity.ubot.com.tw/aws_act/2026/202607delivery/index.htm"


class FakeHttp:
    def get(self, url: str, **_: object) -> Response:
        if url.endswith("/api/Rewards"):
            return Response(url, url, 200, API, "application/json", "")
        page = """<html><body><nav>回首頁 信用卡</nav>
        <div class="tab-pane"><article><p>活動一、Uber Eats 筆筆3%刷卡金，每月10日開放登錄。</p>
        <p>這段文字刻意寫長，讓第一個 article 自己就超過兩百字。""" + "說明" * 120 + """</p>
        </article></div><div class="tab-pane"><p>活動二、foodpanda 筆筆3%刷卡金。</p></div>
        <div class="modal"><h3>注意事項</h3><p>活動期間：2026/7/1-12/31</p></div>
        <div class="modal"><p>提醒您！</p><p>您即將離開聯邦銀行網站。</p>
        <p>我同意</p><p>我不同意</p></div><footer>謹慎理財</footer></body></html>"""
        return Response(url, url, 200, page, "text/html", "")


def test_discover_all_catalogs_and_shared_detail() -> None:
    adapter = UbotAdapter(FakeHttp())  # type: ignore[arg-type]
    stubs = adapter.discover()

    assert len(stubs) == 98  # 101 筆去掉「刷卡即時簡訊」與世界展望會公益
    assert [s.title for s in stubs[:3]] == [
        "保險分期回饋自由選", "iPASS MONEY", "聯邦M世界卡 透見不凡",
    ]
    assert (stubs[0].period_start, stubs[0].period_end) == (date(2026, 7, 1), date(2026, 12, 31))
    assert len({s.category for s in stubs}) == 7
    # 同一份明細被兩張卡片指到：各自一個 Stub，key 以標題區分、url 相同
    shared = [s for s in stubs if s.url == DELIVERY]
    assert sorted(s.title for s in shared) == ["Uber Eats", "foodpanda"]
    assert len({s.key for s in shared}) == 2


def test_terms_keep_every_tab_and_drop_leave_dialog() -> None:
    adapter = UbotAdapter(FakeHttp())  # type: ignore[arg-type]
    stub = adapter.stub(key=DELIVERY, title="Uber Eats", url=DELIVERY)
    assert stub is not None
    text = adapter.terms(stub).text
    assert text.startswith("活動一、Uber Eats")
    assert "活動二、foodpanda" in text and "注意事項" in text
    assert not any(w in text for w in ("回首頁", "即將離開", "我同意", "謹慎理財"))
