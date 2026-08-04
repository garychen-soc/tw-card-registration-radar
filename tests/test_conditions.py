"""活動條件抽取測試。原文取自玉山、聯邦官方頁實測。"""

from __future__ import annotations

from radar.parse.conditions import (
    extract,
    extract_eligibility,
    extract_installment,
    extract_quota,
    extract_thresholds,
    tiers_from_table,
)
from radar.segment import table_rows

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


def test_per_transaction_threshold() -> None:
    kind, tiers = extract_thresholds("單筆滿10,000元以上，享1,000點玉山e point。")
    assert kind == "per_transaction"
    assert [tier.spend_twd for tier in tiers] == [10000]


def test_cumulative_threshold() -> None:
    kind, tiers = extract_thresholds(
        "每月國內外一般消費累積滿30,000元 (含) 以上加碼2%P幣，最高3% P幣"
    )
    assert kind == "cumulative"
    assert [tier.spend_twd for tier in tiers] == [30000]


def test_per_transaction_wins_over_cumulative() -> None:
    """兩者同時出現時以單筆為主 —— 單筆門檻對使用者的行動指引更直接。"""
    kind, _ = extract_thresholds("單筆滿10,000元，當月累積滿30,000元另有加碼")
    assert kind == "per_transaction"


def test_quota_with_seats() -> None:
    quota = extract_quota("本活動適用一次付清或分期消費，限量登錄1,000名，限正卡人登錄")
    assert quota.limited is True
    assert quota.seats == 1000
    assert quota.confidence >= 0.8


def test_quota_without_seats_still_flags_limited() -> None:
    """『額滿為止』沒給數字，但使用者仍需知道要搶 —— 實測 62.8% 的需登錄活動屬此類。"""
    quota = extract_quota("於2026/8/17 10:00 開放登錄，額滿為止。")
    assert quota.limited is True
    assert quota.seats is None


def test_no_quota_wording_means_unlimited() -> None:
    assert extract_quota("完成登錄即享回饋").limited is False


def test_primary_card_only() -> None:
    eligibility = extract_eligibility("限量登錄1,000名，限正卡人登錄，正、附卡消費合併計算")
    assert eligibility.primary_card_only is True


def test_new_customer_and_first_swipe() -> None:
    eligibility = extract_eligibility(
        "玉山Unicard新申辦加碼需透過momo專屬連結首次申辦方可享有，並完成首刷"
    )
    assert eligibility.new_customer_only is True
    assert eligibility.first_swipe_only is True
    assert eligibility.evidence is not None


def test_known_cards_are_matched_from_spec_list() -> None:
    eligibility = extract_eligibility(
        "刷玉山Unicard最高享17.5%回饋",
        known_cards=("玉山Unicard", "玉山Pi拍錢包信用卡"),
    )
    assert eligibility.cards == ["玉山Unicard"]


def test_exclusion_clause_does_not_become_eligible_cards() -> None:
    """『簽帳金融卡、公司卡及採購卡等，恕不適用』是排除條款。

    通用的「XX卡」正則會把它抓成適用卡別，反而製造錯誤資訊 —— 所以卡別
    只從 spec 提供的已知清單比對。
    """
    eligibility = extract_eligibility(
        "簽帳金融卡、公司卡及採購卡等，恕不適用。",
        known_cards=("玉山Unicard",),
    )
    assert eligibility.cards == []


def test_installment_periods() -> None:
    installment = extract_installment("刷卡分期滿額最高享5,025元回饋，可分3期、6期或分12期")
    assert installment.required is True
    assert installment.periods == [3, 6, 12]


def test_installment_zero_rate() -> None:
    installment = extract_installment("刷卡分期0利率，可分12期（零利率）")
    assert installment.required is True
    assert installment.rate == "0%"


def test_cashback_percent_is_not_installment_rate() -> None:
    """『最高享17.5%回饋』是回饋率，不是分期利率。

    實測玉山 momo 頁確實把回饋率誤抓成利率 —— 尾綴的「利率」若是選擇性的，
    任何百分比都會命中。
    """
    installment = extract_installment("刷卡分期滿額，最高享17.5%回饋")
    assert installment.required is True
    assert installment.rate == ""


def test_installment_absent() -> None:
    installment = extract_installment("一次付清即享 5% 回饋")
    assert installment.required is False
    assert installment.periods == []


def test_tiers_from_real_ubot_table() -> None:
    """聯邦四階門檻表 —— 前身只有單一 max_reward_amount_twd，無法表達階梯。"""
    headers, rows = table_rows(UBOT_TIER_TABLE)
    tiers = tiers_from_table(headers, rows)
    assert len(tiers) == 4
    assert [tier.spend_twd for tier in tiers] == [35000, 50000, 65000, 75000]
    assert [tier.reward_twd for tier in tiers] == [700, 1000, 1300, 1500]
    assert [tier.reward_if_installment for tier in tiers] == [800, 1100, 2100, 2500]
    assert [tier.quota_seats for tier in tiers] == [100, 100, 100, 50]
    assert {tier.installment_periods for tier in tiers} == {12}


def test_extract_prefers_table_tiers_over_text() -> None:
    headers, rows = table_rows(UBOT_TIER_TABLE)
    conditions = extract(
        "單筆滿35,000元享回饋",
        table_headers=headers,
        table_rows=rows,
    )
    assert len(conditions.threshold_tiers) == 4
    assert conditions.threshold_kind == "per_transaction"


def test_extract_reward_caps() -> None:
    conditions = extract("最高享17.5%回饋，每月每歸戶上限1,000點")
    assert conditions.reward_cap_percent == 17.5
    assert conditions.reward_cap_twd == 1000


def test_extract_on_plain_text_is_all_unknown() -> None:
    conditions = extract("本行保留變更活動內容之權利")
    assert conditions.threshold_kind == "unknown"
    assert conditions.threshold_tiers == []
    assert conditions.quota.limited is False
    assert conditions.eligibility.confidence == 0.0


def test_menu_label_is_not_registration_evidence() -> None:
    """「活動登錄」是選單與按鈕的標籤，不是活動條件。

    實測玉山每一頁的側邊選單都有它，收進正面證據會讓 243 筆中 150 筆
    被誤判為「需登錄但抓不到時點」。
    """
    from radar.parse.conditions import requires_registration

    menu = "活動登錄\n中獎名單\n卡友權益\n常見問題"
    assert requires_registration(menu) is False


def test_real_registration_wording_is_detected() -> None:
    from radar.parse.conditions import requires_registration

    for text in (
        "須先完成活動登錄才享回饋",
        "登錄期間：2026/8/7 17:00~2026/8/31 23:59",
        "8/17 10:00 開放登錄，限量600名",
        "需登錄後消費始計入",
    ):
        assert requires_registration(text) is True, text


def test_negative_wording_overrides() -> None:
    from radar.parse.conditions import requires_registration

    assert requires_registration("本活動無需登錄，開放登錄期間不適用") is False
