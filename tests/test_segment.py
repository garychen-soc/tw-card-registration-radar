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
