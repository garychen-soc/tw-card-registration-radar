"""永豐擷取器：以精簡官方總覽頁回放 discover。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from radar.banks.sinopac import SinopacAdapter
from radar.transport import Response

LIST = (Path(__file__).parents[1] / "fixtures/sinopac/list.html").read_text("utf-8")


class FakeHttp:
    def get(self, url: str, **_: object) -> Response:
        return Response(url, url, 200, LIST, "text/html", "")


def test_discover_reads_all_cards() -> None:
    adapter = SinopacAdapter(FakeHttp())  # type: ignore[arg-type]
    stubs = adapter.discover()

    assert len(stubs) == 21
    assert [s.title for s in stubs[:3]] == [
        "旅遊優惠輕鬆享", "ApplePay綁定 萬元驚喜抽", "網購/電視購物優惠",
    ]
    assert not adapter.skipped
    apple = stubs[1]
    assert apple.url == (
        "https://bank.sinopac.com/sinopacBT/personal/credit-card/discount/310482674.html"
    )
    assert (apple.period_start, apple.period_end) == (date(2026, 7, 31), date(2026, 12, 31))
    # 迄日 2999 是常駐頁佔位，不當活動期間
    assert (stubs[0].period_start, stubs[0].period_end) == (None, None)
    assert sum(s.period_end is None for s in stubs) == 11
