"""兆豐銀行擷取器：回放精簡的 DiscountOverview/GetDiscount。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from radar.banks.mega import MegaAdapter
from radar.transport import Response

API = (Path(__file__).parents[1] / "fixtures/mega/discount.json").read_text("utf-8")
OVERVIEW = "https://www.megabank.com.tw/personal/credit-card/event/overview"


class FakeHttp:
    def get(self, url: str, *, data: dict[str, str] | None = None, **_: object) -> Response:
        if "GetDiscount" in url:
            assert data is not None and set(data) == {"itemID", "settingID"}
            return Response(url, url, 200, API, "application/json", "")
        page = ("""<html><body><main><div class="c-share">分享至Facebook</div>
        <div class="l-majorBlock__item--text"><h2>旅展好禮</h2><p>活動期間：2026/11/6~11/9</p>
        <article class="gift-card"><p>精選下單禮"""
        + "，指定旅行社刷卡滿額贈" * 30
        + """</p></article><article class="gift-card"><p>展場滿額禮</p></article>
        <h3>注意事項</h3><p>限兆豐信用卡。</p></div>
        <div class="l-majorBlock__item--gy">您可能有興趣 其他活動</div></main></body></html>""")
        return Response(url, url, 200, page, "text/html", "")


def test_discover_live_cards_with_links() -> None:
    adapter = MegaAdapter(FakeHttp())  # type: ignore[arg-type]
    stubs = adapter.discover()

    # 136 筆樣本：已下架 32 筆不收、4 筆沒有明細連結記入 skipped
    assert len(stubs) == 100
    assert len(adapter.skipped) == 4
    assert [s.title for s in stubs[:3]] == [
        "2026 ITF台灣國際旅展 兆豐卡獨享雙重好禮",
        "新光三越2026週年慶｜刷兆豐優惠享不停！",
        "「利多御璽卡」國外消費最高3.1%回饋",
    ]
    assert stubs[0].url == f"{OVERVIEW}/2026itf"
    assert (stubs[0].period_start, stubs[0].period_end) == (date(2026, 11, 6), date(2026, 11, 9))
    ntso = [s for s in stubs if s.url == f"{OVERVIEW}/ntso"]
    assert len(ntso) == 2 and len({s.key for s in ntso}) == 2


def test_terms_whole_content_block_only() -> None:
    adapter = MegaAdapter(FakeHttp())  # type: ignore[arg-type]
    stub = adapter.stub(key="k", title="旅展", url=f"{OVERVIEW}/2026itf")
    assert stub is not None
    terms = adapter.terms(stub)
    assert terms.text.startswith("旅展好禮\n活動期間")
    assert "展場滿額禮" in terms.text and "注意事項" in terms.text  # 不只第一個 article
    assert "分享至" not in terms.text and "您可能有興趣" not in terms.text
    assert terms.period_end == date(2026, 11, 9)
