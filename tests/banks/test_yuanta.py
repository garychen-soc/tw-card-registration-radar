"""元大擷取器。

清單頁 HTML 是依 v1 selector 重建的結構樣本（驗證表單分頁邏輯）；明細頁用
2026-11-05 以瀏覽器 UA 取得的官方回應 ``fixtures/yuanta/detail_applepay.html``。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from radar.banks.base import BROWSER_USER_AGENT
from radar.banks.yuanta import YuantaAdapter
from radar.models import Stub
from radar.transport import Response

PAGE = """<html><body><nav><a href="/bank/creditCard/index.do">信用卡</a></nav>
<form><input type="hidden" name="pA" value="2"/><input type="hidden" name="iA" value="3"/></form>
<ul>{items}</ul></body></html>"""
ITEM = (
    '<li><div class="pic"><img src="x.jpg"></div><h6>{title}</h6><p>摘要{n}</p>'
    '<a href="/bank/creditCard/promotionActivity/in.do?id={n}">詳細內容</a></li>'
)


class FakeHttp:
    def __init__(self) -> None:
        self.forms: list[dict[str, str]] = []

    def get(self, url: str, *, data: dict[str, str] | None = None, **_: object) -> Response:
        if data is None:
            items = (
                ITEM.format(title="網購最高回饋", n="a1") + ITEM.format(title="百貨滿額禮", n="a2")
            )
        else:
            self.forms.append(data)
            items = ITEM.format(title="海外消費加碼", n="b1") + "<li>無卡片的導覽項</li>"
        return Response(url, url, 200, PAGE.format(items=items), "text/html", "")


def test_discover_follows_form_pages() -> None:
    http = FakeHttp()
    adapter = YuantaAdapter(http)  # type: ignore[arg-type]
    stubs = adapter.discover()

    assert [s.title for s in stubs] == ["網購最高回饋", "百貨滿額禮", "海外消費加碼"]
    assert stubs[0].url == (
        "https://www.yuantabank.com.tw/bank/creditCard/promotionActivity/in.do?id=a1"
    )
    assert http.forms == [{"pN": "2", "pA": "2", "iA": "3"}]
    assert not adapter.skipped


DETAIL = (Path(__file__).parents[1] / "fixtures/yuanta/detail_applepay.html").read_text("utf-8")


class DetailHttp:
    def get(self, url: str, **_: object) -> Response:
        return Response(url, url, 200, DETAIL, "text/html", "")


def test_terms_scoped_to_event_info() -> None:
    adapter = YuantaAdapter(DetailHttp())  # type: ignore[arg-type]
    url = "https://www.yuantabank.com.tw/bank/creditCard/promotionActivity/in.do?id=6ae9eccdb90000053f09"
    terms = adapter.terms(Stub(bank_id="yuanta", key=url, title="x", url=url))

    assert terms.title == "綁定 Apple Pay 筆筆20%回饋，最高享200元刷卡金"
    assert (terms.period_start, terms.period_end) == (date(2026, 8, 18), date(2026, 12, 31))
    assert "登錄期間:2026/8/18 14:00~2026/12/31 23:59止" in terms.text
    for noise in ("個人金融", "IE瀏覽器停止支援", "元大行動銀行"):
        assert noise not in terms.text


def test_browser_user_agent_is_opt_in() -> None:
    from radar.banks.base import Adapter

    assert YuantaAdapter.user_agent == BROWSER_USER_AGENT
    assert "TwCardRegistrationRadar" not in BROWSER_USER_AGENT
    assert Adapter.user_agent is None
