"""活動條件抽取 —— 回答「這活動我拿不拿得到」。

前身完全沒有這些欄位，資訊只存在於自由文字（而且大部分被濾網濾掉）。
實測 242 筆需登錄活動中，這些條件在文字裡的出現率：

===================  =======
限量／額滿為止         62.8%
分期／期數             40.1%
指定卡別限定           33.1%
每期重新登錄           30.6%
累積滿額門檻           24.8%
單筆滿額門檻           14.0%
新戶／新卡友／首刷      9.5%
===================  =======

規則優先：這些文案在台灣銀行的寫法高度收斂，實測樣本如
``單筆滿10,000元以上，享1,000點``、``每月...累積滿30,000元(含)以上加碼2%``、
``限量登錄1,000名，限正卡人登錄``、``限量600名，額滿為止``。
規則吃不到或衝突的少數才需要人工／模型介入。
"""

from __future__ import annotations

import re

from ..models import (
    Conditions,
    Eligibility,
    Evidence,
    Installment,
    Quota,
    ThresholdKind,
    ThresholdTier,
)
from .normalize import normalize

_NEW_CUSTOMER = re.compile(r"新戶|新卡友|新申辦|首次申辦|新開卡|首辦")
_FIRST_SWIPE = re.compile(r"首刷")
_PRIMARY_ONLY = re.compile(r"限正卡人|僅限正卡人|正卡人(?:方可|始可|限定)?登錄|限正卡")

_PER_TRANSACTION = re.compile(
    r"單筆(?:一般)?(?:消費|分期)?\s*(?:滿|達)\s*(?:NT\$|\$)?\s*([\d,]+)\s*元"
)
_CUMULATIVE = re.compile(
    r"(?:累積|累計|當期|每月|全月|單月)[^。；\n]{0,12}?(?:消費)?\s*(?:滿|達)"
    r"\s*(?:NT\$|\$)?\s*([\d,]+)\s*元"
)

_PERIOD_TOKEN = re.compile(r"(\d{1,2})\s*期")
_ENUM_GAP = re.compile(r"[\s、,，／/或及]*")
_INSTALLMENT_REQUIRED = re.compile(r"需(?:辦理)?分期|分期滿額|刷卡分期|分期消費|限分期")
# 必須有明確的「利率」字樣或「零利率」。舊寫法的尾綴是選擇性的，會把
# 「最高享17.5%回饋」這種回饋率誤抓成分期利率（實測玉山頁面確實發生）。
_INSTALLMENT_RATE = re.compile(
    r"零利率|(?:利率|分期)\s*(\d{1,2}(?:\.\d+)?)\s*%|(\d{1,2}(?:\.\d+)?)\s*%\s*(?:利率|分期利率)"
)

_QUOTA_SEATS = re.compile(r"(?:限量|名額|前)\s*(?:登錄)?\s*([\d,]+)\s*名")
_QUOTA_UNLIMITED_MARK = re.compile(r"額滿為止|額滿即止|依名額|限量")

_REWARD_CAP_AMOUNT = re.compile(r"(?:回饋)?上限\s*(?:NT\$|\$)?\s*([\d,]+)\s*(?:元|點)")
_REWARD_CAP_PERCENT = re.compile(r"最高(?:享|回饋)?\s*(\d{1,2}(?:\.\d+)?)\s*%")

_HEADER_SPEND = re.compile(r"門檻|滿額|消費金額|級距")
_HEADER_REWARD = re.compile(r"刷卡金|回饋金|回饋(?!升級)|贈品|點數")
_HEADER_UPGRADE = re.compile(r"升級|以上回饋|加碼")
_HEADER_QUOTA = re.compile(r"名額|限量")
_HEADER_PERIODS = re.compile(r"分\s*(\d{1,2})\s*期")

_MONEY = re.compile(r"([\d,]+)")


def _int(text: str) -> int | None:
    match = _MONEY.search(text.replace(" ", ""))
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _evidence(text: str, pattern: re.Pattern[str]) -> Evidence | None:
    match = pattern.search(text)
    if not match:
        return None
    start = max(0, match.start() - 30)
    return Evidence(text=text[start : match.end() + 40].strip()[:400])


def extract_eligibility(raw_text: str, *, known_cards: tuple[str, ...] = ()) -> Eligibility:
    text = normalize(raw_text)
    new_customer = bool(_NEW_CUSTOMER.search(text))
    first_swipe = bool(_FIRST_SWIPE.search(text))
    primary_only = bool(_PRIMARY_ONLY.search(text))
    # 卡別採 spec 提供的已知清單比對。不用通用的「XX卡」正則 —— 那會抓到
    # 「簽帳金融卡、公司卡及採購卡等，恕不適用」這種排除條款，反而製造錯誤。
    cards = [name for name in known_cards if name in text]
    confidence = 0.0
    if new_customer or first_swipe or primary_only or cards:
        confidence = 0.75
    return Eligibility(
        new_customer_only=new_customer,
        first_swipe_only=first_swipe,
        primary_card_only=primary_only,
        cards=cards,
        confidence=confidence,
        evidence=_evidence(text, _NEW_CUSTOMER) or _evidence(text, _PRIMARY_ONLY),
    )


def extract_thresholds(raw_text: str) -> tuple[ThresholdKind, list[ThresholdTier]]:
    text = normalize(raw_text)
    per_transaction = [
        value for value in (_int(m.group(1)) for m in _PER_TRANSACTION.finditer(text)) if value
    ]
    cumulative = [
        value for value in (_int(m.group(1)) for m in _CUMULATIVE.finditer(text)) if value
    ]
    if per_transaction:
        tiers = [ThresholdTier(spend_twd=value) for value in sorted(set(per_transaction))]
        return "per_transaction", tiers
    if cumulative:
        tiers = [ThresholdTier(spend_twd=value) for value in sorted(set(cumulative))]
        return "cumulative", tiers
    return "unknown", []


def tiers_from_table(headers: list[str], rows: list[list[str]]) -> list[ThresholdTier]:
    """把表格式階梯門檻映射成結構化階梯。

    實測聯邦銀行的四階表格：
    ``35,000元 / 700元 / 800元 / 100名`` … ``75,000元 / 1,500元 / 2,500元 / 50名``
    """
    roles: list[str] = []
    upgrade_periods: int | None = None
    for header in headers:
        if _HEADER_SPEND.search(header):
            roles.append("spend")
        elif _HEADER_UPGRADE.search(header):
            roles.append("upgrade")
            if match := _HEADER_PERIODS.search(header):
                upgrade_periods = int(match.group(1))
        elif _HEADER_REWARD.search(header):
            roles.append("reward")
        elif _HEADER_QUOTA.search(header):
            roles.append("quota")
        else:
            roles.append("ignore")

    if "spend" not in roles:
        return []

    tiers: list[ThresholdTier] = []
    for row in rows:
        values: dict[str, int | None] = {}
        for role, cell in zip(roles, row, strict=False):
            if role != "ignore" and role not in values:
                values[role] = _int(cell)
        spend = values.get("spend")
        if spend is None:
            continue
        tiers.append(
            ThresholdTier(
                spend_twd=spend,
                reward_twd=values.get("reward"),
                reward_if_installment=values.get("upgrade"),
                installment_periods=upgrade_periods,
                quota_seats=values.get("quota"),
            )
        )
    return tiers


def _installment_periods(text: str) -> list[int]:
    """抽出分期期數，支援列舉寫法。

    真實文案常寫「可分3期、6期或分12期」—— 只認「分N期」會漏掉中間的 6 期。
    因此除了「分」直接接的數字，也接受緊隨在已認可期數之後、只以列舉符號
    分隔的數字。
    """
    periods: list[int] = []
    last_end = -1
    for match in _PERIOD_TOKEN.finditer(text):
        near_marker = "分" in text[max(0, match.start() - 3) : match.start()]
        continues = last_end >= 0 and bool(
            _ENUM_GAP.fullmatch(text[last_end : match.start()])
        )
        if not (near_marker or continues):
            continue
        value = int(match.group(1))
        if 2 <= value <= 60:
            periods.append(value)
            last_end = match.end()
    return sorted(set(periods))


def extract_installment(raw_text: str) -> Installment:
    text = normalize(raw_text)
    periods = _installment_periods(text)
    required = bool(_INSTALLMENT_REQUIRED.search(text))
    rate = ""
    if (required or periods) and (match := _INSTALLMENT_RATE.search(text)):
        if "零利率" in match.group(0):
            rate = "0%"
        elif value := (match.group(1) or match.group(2)):
            rate = f"{value.strip()}%"
    return Installment(
        required=required,
        periods=periods,
        rate=rate,
        confidence=0.75 if (required or periods) else 0.0,
    )


def extract_quota(raw_text: str) -> Quota:
    text = normalize(raw_text)
    seats: int | None = None
    if match := _QUOTA_SEATS.search(text):
        seats = _int(match.group(1))
    limited = seats is not None or bool(_QUOTA_UNLIMITED_MARK.search(text))
    if not limited:
        return Quota()
    evidence = _evidence(text, _QUOTA_SEATS) or _evidence(text, _QUOTA_UNLIMITED_MARK)
    return Quota(
        limited=True,
        seats=seats,
        text=evidence.text if evidence else "",
        confidence=0.85 if seats is not None else 0.6,
    )


def extract(
    raw_text: str,
    *,
    known_cards: tuple[str, ...] = (),
    table_headers: list[str] | None = None,
    table_rows: list[list[str]] | None = None,
) -> Conditions:
    """整合抽取。表格式階梯門檻優先於文字抽取的門檻。"""
    text = normalize(raw_text)
    kind, tiers = extract_thresholds(text)
    if table_headers and table_rows:
        table_tiers = tiers_from_table(table_headers, table_rows)
        if table_tiers:
            tiers = table_tiers
            kind = "per_transaction"

    cap_amount: int | None = None
    if match := _REWARD_CAP_AMOUNT.search(text):
        cap_amount = _int(match.group(1))
    cap_percent: float | None = None
    percents = [float(m.group(1)) for m in _REWARD_CAP_PERCENT.finditer(text)]
    if percents:
        cap_percent = max(value for value in percents if value <= 100) if percents else None

    return Conditions(
        eligibility=extract_eligibility(text, known_cards=known_cards),
        threshold_kind=kind,
        threshold_tiers=tiers,
        installment=extract_installment(text),
        quota=extract_quota(text),
        reward_cap_twd=cap_amount,
        reward_cap_percent=cap_percent,
    )
