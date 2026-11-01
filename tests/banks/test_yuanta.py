"""元大擷取器。

官方清單頁在本機以專案 UA 存取回 403，沒有官方回應樣本；下列 HTML 是依 v1
selector 與 Codex 版測試重建的**結構樣本**，只驗證解析與表單分頁邏輯。
"""

from __future__ import annotations

from radar.banks.yuanta import YuantaAdapter
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
