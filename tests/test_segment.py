"""明細頁切分測試。原文取自官方頁實測。"""

from __future__ import annotations

from radar.segment import registration_text, sections, split_offers, table_rows

# 實測：玉山 momo 頁的活動邊界。前身把三個子活動的登錄時點合併成一筆，
# 才會出現「活動至 8/15 卻有 8/17 登錄時點」。
ESUN_MULTI_OFFER = """
【活動一】momo 指定連結消費滿額回饋
活動期間：2026/8/1~2026/8/15
※ 登錄辦法：2026/8/7 17:00:00~2026/8/31 23:59:00統一開放正卡人登錄。
單筆滿10,000元，享1,000點玉山e point。限量登錄1,000名。
【活動二】momo 站上加碼
活動期間：2026/8/1~2026/8/31
※ 於2026/8/17 10:00 於momo活動登記頁開放登錄，限量600名，額滿為止。
【活動三】玉山Unicard專屬優惠，最高享5,025元回饋
活動期間：2026/8/1~2026/8/31
8/20 17:00開放登錄(限量1,300名)
"""

UBOT_TIER_TABLE = """
<dl>
<dt>單筆分期門檻</dt><dt>回饋刷卡金</dt><dt>分12期以上回饋升級</dt>
<dt>每波限量登錄名額</dt><dt>登錄時間</dt>
<dd>35,000元</dd><dd>700元</dd><dd>800元</dd><dd>100名</dd>
<dd>第一波 8/15下午3點開放登錄至8/20</dd>
<dd>50,000元</dd><dd>1,000元</dd><dd>1,100元</dd><dd>100名</dd>
<dd>65,000元</dd><dd>1,300元</dd><dd>2,100元</dd><dd>100名</dd>
<dd>75,000元</dd><dd>1,500元</dd><dd>2,500元</dd><dd>50名</dd>
</dl>
"""


def test_splits_multiple_offers_on_bracket_marker() -> None:
    chunks = split_offers(ESUN_MULTI_OFFER)
    assert len(chunks) == 3
    assert all(chunk.split for chunk in chunks)
    assert chunks[0].boundary_marker.startswith("【活動一")
    # 每個子活動只帶自己的登錄時點 —— 這是修正前身活動粒度錯誤的核心
    assert "2026/8/7 17:00" in chunks[0].text
    assert "2026/8/7" not in chunks[1].text
    assert "8/17 10:00" in chunks[1].text or "2026/8/17 10:00" in chunks[1].text
    assert "8/20 17:00" in chunks[2].text


def test_single_offer_page_reports_no_boundary() -> None:
    """切不出來時明確標示，讓呼叫端能標記 needs_review 而不是假裝切好了。"""
    chunks = split_offers("活動期間：2026/8/1~2026/8/31 完成登錄享 5% 回饋")
    assert len(chunks) == 1
    assert chunks[0].split is False
    assert chunks[0].boundary_marker == ""


def test_spec_supplied_pattern_wins() -> None:
    # 中信一頁 14 個活動，錨點是「活動・注意事項」而非【】
    filler_a = "甲" * 50
    filler_b = "乙" * 50
    text = f"活動・注意事項 A {filler_a}\n活動・注意事項 B {filler_b}"
    chunks = split_offers(text, pattern=r"活動・注意事項")
    assert len(chunks) == 2


def test_sections_are_keyword_anchored() -> None:
    parts = sections(ESUN_MULTI_OFFER)
    assert "period" in parts
    assert "registration" in parts
    assert "2026/8/1" in parts["period"]
    assert "登錄辦法" in parts["registration"]


def test_registration_text_prefers_registration_section() -> None:
    text = registration_text(ESUN_MULTI_OFFER)
    assert "登錄" in text


def test_registration_text_falls_back_to_full_text() -> None:
    """找不到登錄段落時退回全文，資訊不會消失 —— 這是與前身濾網最大的差異。"""
    body = "單筆滿10,000元享5%回饋，適用一次付清"
    assert registration_text(body) == body


def test_sections_keeps_lead_text() -> None:
    parts = sections("momo 購物8月網購活動\n活動期間：2026/8/1~2026/8/31")
    assert parts["lead"].startswith("momo")


def test_table_rows_handles_merged_last_column() -> None:
    """5 個表頭、每列 4 個值（登錄時間欄跨列合併）。等寬切分會錯位。"""
    headers, rows = table_rows(UBOT_TIER_TABLE)
    assert headers == ["單筆分期門檻", "回饋刷卡金", "分12期以上回饋升級", "每波限量登錄名額"]
    assert len(rows) == 4
    assert rows[0] == ["35,000元", "700元", "800元", "100名"]
    assert rows[3] == ["75,000元", "1,500元", "2,500元", "50名"]


def test_table_rows_returns_empty_when_no_headers() -> None:
    assert table_rows("<p>沒有表格</p>") == ([], [])


def test_chunk_title_skips_invisible_first_line() -> None:
    from radar.segment import split_offers as split

    body = (
        "活動一、﻿\n實際標題在第二行" + "甲" * 60 + "\n"
        "活動二、正常標題" + "乙" * 60
    )
    chunks = split(body)
    assert len(chunks) == 2
    assert chunks[0].title.startswith("活動一")
    assert "（無標題）" not in chunks[0].title


def test_boilerplate_is_stripped_before_analysis() -> None:
    """導覽與 Cookie 告知必須先移除。

    實測元大的活動頁若不移除，活動標題會變成 Cookie 公告，而導覽列的
    「活動登錄」選單會讓 57 筆中 49 筆被誤判為需要登錄。
    """
    from radar.htmltext import to_text

    html = """
    <html><body>
      <div class="cookie-notice">為提供您更好、更個人化的服務，本網站採用網路追蹤工具</div>
      <nav><a href="/login">活動登錄</a><a href="/cards">信用卡產品</a></nav>
      <main>
        <h1>元大悠遊聯名卡首筆20%回饋</h1>
        <p>活動時間： 2026/06/01~2026/08/31</p>
        <p>指定卡別： 鑽金智富悠遊聯名卡</p>
      </main>
      <footer>本行保留變更權利</footer>
    </body></html>
    """
    text = to_text(html)
    assert "元大悠遊聯名卡" in text
    assert "活動時間" in text
    assert "Cookie" not in text and "網路追蹤工具" not in text
    assert "活動登錄" not in text, "導覽列的登錄選單不得混入內文"
    assert "本行保留變更權利" not in text


def test_looks_multi_offer_needs_two_distinct_periods() -> None:
    """同一個活動期間在注意事項裡重複提到，不算多活動證據
    （實測玉山有 116 頁因為只數標籤次數而被誤標）。"""
    from radar.segment import looks_multi_offer

    repeated = "活動期間：2026/8/1~2026/8/31 … 注意事項：活動期間：2026/8/1~2026/8/31 恕不適用"
    genuine = "活動期間：2026/8/1~2026/8/10 享5% … 活動期間：2026/8/11~2026/8/31 享8%"
    assert looks_multi_offer(repeated) is False
    assert looks_multi_offer(genuine) is True


def test_link_dense_menu_is_dropped_even_without_nav_markers() -> None:
    """玉山的側邊選單既不在 <nav> 裡、class 也不像導覽，但整塊都是連結。

    實測它污染了 150 筆活動的登錄原文，讓它們全被判定為「需登錄但抓不到時點」。
    """
    from radar.htmltext import to_text

    html = """
    <html><body>
      <div class="box-a">
        <a href="/1">活動登錄</a><a href="/2">中獎名單</a><a href="/3">卡友權益</a>
        <a href="/4">常見問題</a><a href="/5">辦卡進度</a><a href="/6">補件說明</a>
      </div>
      <div class="box-b">
        <p>寶雅今夏你最美 美力加倍最高10%回饋</p>
        <p>活動期間：2026/8/1~2026/8/31，至寶雅實體門市消費滿799元立折80元。</p>
      </div>
    </body></html>
    """
    text = to_text(html)
    assert "寶雅" in text
    assert "活動期間" in text
    assert "活動登錄" not in text
    assert "中獎名單" not in text


def test_prose_with_a_few_links_is_kept() -> None:
    """內文裡有幾個連結不算選單 —— 連結密度門檻不能誤刪內容。"""
    from radar.htmltext import to_text

    html = """
    <html><body><div>
      <p>活動期間：2026/8/1~2026/8/31，單筆滿10,000元享5%回饋，
      詳情請見<a href="/terms">活動辦法</a>，或參考<a href="/faq">常見問題</a>。
      本活動限正卡人參加，限量1,000名，額滿為止。</p>
    </div></body></html>
    """
    text = to_text(html)
    assert "單筆滿10,000元" in text
    assert "限量1,000名" in text


def test_index_page_splits_on_title_then_period_lines() -> None:
    """索引頁的通用形態：一行標題，下一行就是「活動期間」，如此重複。

    實測凱基與陽信的活動索引頁都是這樣，整頁被當成一個活動時兩家各只產出 1 筆。
    """
    text = (
        "信用卡\n最新優惠活動\n"
        "新辦萬事達卡 7-11/全家/全聯最高10%回饋\n"
        "活動期間：115/7/17~115/12/31\n"
        "新申辦指定萬事達卡，核卡後30天內於便利商店消費享回饋，需完成活動登錄。\n"
        "凱基悠遊聯名卡 加碼回饋\n"
        "活動期間：115/8/1~115/9/30\n"
        "指定通路消費滿額享回饋，登錄期間：115/8/1 10:00~115/9/30 23:59 開放登錄。\n"
    )
    chunks = split_offers(text)
    assert len(chunks) == 2
    assert chunks[0].title.startswith("新辦萬事達卡")
    assert chunks[1].title.startswith("凱基悠遊聯名卡")
    # 每塊只帶自己的期間
    assert "115/7/17" in chunks[0].text or "2026/7/17" in chunks[0].text
    assert "7/17" not in chunks[1].text


def test_single_activity_page_is_not_split_by_period_anchor() -> None:
    """只有一個活動期間時不得切開 —— 這個錨點是最後手段，需要重複出現。"""
    text = "夏日回饋活動\n活動期間：2026/8/1~2026/8/31\n單筆滿1,000元享5%回饋，完成登錄後計入。\n"
    chunks = split_offers(text)
    assert len(chunks) == 1
    assert chunks[0].split is False
