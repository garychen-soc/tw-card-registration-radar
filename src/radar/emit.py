"""輸出層：index.json、catalog/<bank>.json、detail/<bank>.json、registration.ics。

**三層而不是一份大檔。** 前身把 1,071 筆活動塞進單一 2.09MB 的 JSON，首屏
要下載 241KB gzip、解壓 2.09MB、739ms。我的第一版也犯了同樣的錯：實測聯邦
單一家銀行的完整輸出就 352KB，其中 **223 筆裡只有 19 筆是「可行動且有確定
登錄時點」** —— 為了顯示 19 筆而載入 223 筆的完整條件，擴到 17 家會變成 1.6MB。

所以分成：

``index.json``
    首屏所需。來源健康、警示、計數，以及 ``agenda`` —— 只含有登錄視窗的活動，
    每筆只帶時間軸卡片需要的欄位。
``catalog/<bank>.json``
    完整的活動條件。使用者開始瀏覽或篩選才按銀行載入。
``detail/<bank>.json``
    原文與 evidence。展開單筆活動才載入。
``calendar/registration.ics``
    可訂閱的登錄提醒，循環活動用 RRULE。

**去重也在這一層。** 銀行會把同一份活動內容掛在多個網址上（見 ``dedupe_campaigns``），
一頁一 Campaign 的抓取模型必然把它切成多筆。修在輸出層而不是 runner 層，是因為
``SourceHealth.offer_count`` 是涵蓋率防護（``guard.assess``）的比較基準：那個數字
必須一直代表「這次真的從官方頁讀到幾筆」，才能偵測抓取退步。若在 runner 去重，
它會混入「重複被合併掉幾筆」，防護就再也分不清筆數下降是抓取壞了還是去重生效。

**兩條輸出規則。**

1. 不含任何時間衍生狀態。「是否進行中」「今日待登錄」全由讀取端算。
   ``agenda`` 的收錄條件是「有登錄視窗」這個絕對事實，不是「未來 14 天」
   這種相對條件 —— 否則資料放三天就會漏掉該提醒的活動。
2. 預設值不輸出。223 筆裡絕大多數的 ``threshold_tiers``、``cards``、
   資格旗標都是空的或 false，逐筆輸出純粹是浪費。讀取端把「缺少」
   視為預設值。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .models import Alert, Campaign, Offer, RegistrationWindow, SourceHealth

SCHEMA_VERSION = 1
CALENDAR_PRODID = "-//TW Card Registration Radar//ZH-TW//EN"
# 行事曆事件需要長度，但「登錄開放」的真實截止往往未公告。這個長度只用於
# 呈現，資料層的 end 仍是 None —— 兩者刻意分開，不是用它去填補未知。
REMINDER_EVENT_MINUTES = 15
REMINDER_LEAD_MINUTES = 15
QUOTA_REMINDER_LEAD_MINUTES = 30


def _is_default(value: Any) -> bool:
    """是否為可省略的預設值。

    刻意不用 ``value in (None, "", [], {}, False)`` —— Python 裡 ``0 == False``
    與 ``0.0 == False``，那種寫法會把 0 一起丟掉。而 0 是有意義的：
    ``spend_days_left_after_registering = 0`` 代表「登錄截止日就是活動最後一天」，
    這正是使用者最需要知道的情況。
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return value is False
    if isinstance(value, str | list | dict | tuple):
        return len(value) == 0
    return False


def prune(value: Any) -> Any:
    """移除預設值。讀取端把「缺少」視為預設。

    只作用於巢狀內容 —— 頂層的契約欄位（counts、sources、agenda、offers）
    由呼叫端直接組裝，即使是空的也一律保留，否則消費端會拿到缺鍵的檔案。
    """
    if isinstance(value, dict):
        cleaned = {key: prune(item) for key, item in value.items()}
        return {key: item for key, item in cleaned.items() if not _is_default(item)}
    if isinstance(value, list):
        return [prune(item) for item in value]
    return value


def _window_payload(window: RegistrationWindow) -> dict[str, Any]:
    return {
        "kind": window.kind,
        "start": window.start.isoformat() if window.start else None,
        "end": window.end.isoformat() if window.end else None,
        "confidence": window.confidence,
    }


def _contract_payload(offer: Offer) -> dict[str, Any]:
    contract = offer.registration.timing_contract
    return {
        "kind": contract.kind,
        "spend_counts_from": (
            contract.spend_counts_from.isoformat() if contract.spend_counts_from else None
        ),
        "last_chance_to_register": (
            contract.last_chance_to_register.isoformat()
            if contract.last_chance_to_register
            else None
        ),
        "spend_days_left_after_registering": contract.spend_days_left_after_registering,
        "grace_days_after_period_end": contract.grace_days_after_period_end,
        "confidence": contract.confidence,
        "consistency": contract.consistency,
    }


def _period_payload(offer: Offer) -> dict[str, Any]:
    return {
        "start": offer.period.start.isoformat() if offer.period.start else None,
        "end": offer.period.end.isoformat() if offer.period.end else None,
        "confidence": offer.period.confidence,
    }


def agenda_entry(campaign: Campaign, offer: Offer) -> dict[str, Any]:
    """時間軸卡片所需的最小欄位集合。

    刻意不含條件細節 —— 使用者要看門檻與資格時再從 catalog 載入該銀行。
    """
    registration = offer.registration
    return {
        "id": offer.id,
        "bank_id": campaign.bank_id,
        "title": offer.title,
        "url": campaign.source_url,
        "page_title": campaign.title if campaign.title != offer.title else "",
        "also_at": offer.also_at,
        "period": _period_payload(offer),
        "windows": [_window_payload(window) for window in registration.windows],
        "recurrence": registration.recurrence.kind
        if registration.recurrence.kind != "once"
        else None,
        "contract": registration.timing_contract.kind,
        "quota_limited": offer.conditions.quota.limited,
        "quota_seats": offer.conditions.quota.seats,
        "needs_review": offer.needs_review,
        "review_codes": offer.review_codes,
    }


def catalog_entry(campaign: Campaign, offer: Offer) -> dict[str, Any]:
    """完整的活動條件。使用者瀏覽或篩選時才載入。"""
    conditions = offer.conditions
    eligibility = conditions.eligibility
    return {
        "id": offer.id,
        "campaign_id": campaign.id,
        "title": offer.title,
        "url": campaign.source_url,
        "page_title": campaign.title if campaign.title != offer.title else "",
        "also_at": offer.also_at,
        "period": _period_payload(offer),
        "registration": {
            "required": offer.registration.required,
            "windows": [_window_payload(window) for window in offer.registration.windows],
            "recurrence": {
                "kind": offer.registration.recurrence.kind,
                "note": offer.registration.recurrence.note,
            },
            "contract": _contract_payload(offer),
        },
        "conditions": {
            "eligibility": {
                "new_customer_only": eligibility.new_customer_only,
                "first_swipe_only": eligibility.first_swipe_only,
                "primary_card_only": eligibility.primary_card_only,
                "cards": eligibility.cards,
            },
            "threshold_kind": (
                conditions.threshold_kind if conditions.threshold_kind != "unknown" else None
            ),
            "threshold_tiers": [
                tier.model_dump(exclude_none=True) for tier in conditions.threshold_tiers
            ],
            "installment": {
                "required": conditions.installment.required,
                "periods": conditions.installment.periods,
                "rate": conditions.installment.rate,
            },
            "quota": {"limited": conditions.quota.limited, "seats": conditions.quota.seats},
            "reward_cap_twd": conditions.reward_cap_twd,
            "reward_cap_percent": conditions.reward_cap_percent,
        },
        "needs_review": offer.needs_review,
        "review_codes": offer.review_codes,
    }


# ── 去重 ────────────────────────────────────────────────

# 去重鍵刻意排除的欄位。id 內含網址 slug、campaign_id 與 url 就是網址本身
# —— 它們是「這筆從哪一頁切出來」的紀錄，不是「這是哪一個活動」。
_KEY_EXCLUDED = ("id", "campaign_id", "url", "also_at")

# 跨頁合併的最低子活動數。**這個門檻是整個設計的關鍵，理由是實測出來的。**
#
# 全站 17 家 1,077 筆裡，「內容完全相同」的組共 33 組（可移除 63 筆），但它們是
# 兩個性質完全相反的族群：
#
# 真重複（同一份活動掛在多個網址）—— 標題具體、期間具體，整頁的子活動清單一起重複：
#   星展 mall_08 / _08_2 / _08_3 / _08_5 / mall_09 / mall_11 六頁各切出同樣 5 筆
#   玉山 shopInfo?sno=pi 與 ?sno=pi2 兩頁各切出同樣 8 筆
#   聯邦 202607drugstore/index.htm 與 ...index.htm?p=cosmed（同頁的 query 變體）各 8 筆
#
# 假重複（不同活動，只是我們什麼都沒解析出來）—— 一頁一筆，標題是導覽字串，
# 期間是清單層的整年 fallback，沒有登錄時點也沒有條件：
#   凱基 5 個不同活動頁全叫「信用卡活動」、期間都是 2026-01-01~2026-12-31
#   台北富邦 5 個不同 promotion sn 全叫「用餐享優惠」、上海商銀「Mobile Phone」3 頁
#   王道「關於我們」、永豐「刷卡享優惠」、彰銀「請將裝置改以」4 頁
#
# 光靠內容雜湊無法分辨這兩者，把假重複合併掉會讓 catalog 直接少掉 4 個真實活動頁，
# 而使用者永遠不會知道它存在 —— 這比多顯示一筆嚴重得多。兩族群唯一穩定的結構差異
# 是「重複的是整頁還是單筆」：真重複是同一頁被鏡射到多個網址，所以整份子活動清單
# 逐筆對得上；假重複清一色是單筆頁面，而單筆頁面無法區分「鏡射」與「解析失敗」。
#
# 因此跨頁合併要求「整頁清單相同且該頁有 2 筆以上」。實測這條規則抓到全部 3 組
# 真重複（移除 41 筆）、拒絕全部假重複（0 筆誤併）。代價是少數殘留（例如富邦
# D000268/D000269 兩個單筆頁內容相同但仍各自顯示），寧可留著。
MIRROR_MIN_OFFERS = 2


class MirrorGroup(BaseModel):
    """一組「同內容、多網址」的鏡射頁。留在報告裡供人稽核每次合併了什麼。"""

    bank_id: str
    kept_url: str
    also_at: list[str]
    offers: int


class DedupeReport(BaseModel):
    """去重結果。``per_source`` 讓 index 能同時給出原始筆數與去重後筆數。"""

    merged_offers: int = 0
    merged_campaigns: int = 0
    per_source: dict[str, int] = Field(default_factory=dict)
    mirrors: list[MirrorGroup] = Field(default_factory=list)


# catalog_entry 需要一個 Campaign 來取 id/source_url，但那兩個欄位正是鍵要排除的，
# 所以用一個固定的空殼即可 —— 這也順帶保證鍵只是 Offer 的函數，跨頁比較才成立。
_KEY_CAMPAIGN = Campaign(
    id="",
    bank_id="",
    bank_name="",
    title="",
    source_url="",
    observed_at=datetime(2000, 1, 1, tzinfo=UTC),
)


def offer_content_key(offer: Offer) -> str:
    """「這兩筆會不會在網站上長得一模一樣」的雜湊。

    刻意拿 ``catalog_entry`` 的輸出來算，而不是自己挑幾個欄位：鍵的定義因此
    等於「使用者看得到的全部內容」，兩筆只有在渲染結果完全相同時才會被合併，
    不可能併掉使用者本來分辨得出來的差異。實測這一點很要緊 —— 若只用
    標題＋期間＋登錄時點當鍵，全站會誤併 21 組，包括玉山同一頁上
    ``sno=2008_08`` 的「【活動四】」兩筆（標題期間時點全同、門檻與名額不同）。

    也不能只用標題：全站 57 組同標題共 193 筆，其中包含同一活動的不同檔期
    （聯邦「【Tomod's 】」2024-01-01~2026-06-30 與另一檔期），期間不同就是
    不同活動，合併會讓使用者看到錯的期間。
    """
    payload = catalog_entry(_KEY_CAMPAIGN, offer)
    for field in _KEY_EXCLUDED:
        payload.pop(field, None)
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_first(url: str) -> tuple[int, str]:
    """鏡射組裡保留哪個網址：先短再字典序。

    銀行的鏡射網址通常是在基底網址後面加後綴（``mall_08.html`` 之於
    ``mall_08_2.html``、``index.htm`` 之於 ``index.htm?p=cosmed``），
    因此「最短」幾乎總是那個基底頁 —— 對使用者也是最合理的代表。
    用網址而不是抓取順序來決定，是為了讓 offer id 在跨次執行間穩定
    （id 內含網址 slug，換代表就等於換 id，會讓書籤與已登錄記錄失效）。
    """
    return (len(url), url)


def dedupe_campaigns(campaigns: list[Campaign]) -> tuple[list[Campaign], DedupeReport]:
    """把同一份活動在多個網址上的重複收斂成一筆。

    實測動機：星展「【網購星精彩】7/1~9/30」在網站上出現 6 次，期間、3 個登錄
    時點、限量、時序契約完全相同，只有網址不同 —— 官方把同一個共用活動區塊放在
    六個子頁上，一頁一 Campaign 的抓取模型必然各切出一份。

    兩條規則，作用範圍不同，因為風險不同：

    1. **同頁內**（同一個 Campaign）內容相同 → 直接留一筆。同一頁上的兩列若
       渲染結果完全相同，使用者無從分辨，多顯示一列只是雜訊；而且合併的兩筆
       同屬一頁，不存在「弄丟了另一個活動頁」的風險。實測聯邦 6 筆屬此類
       （切段把注意事項段落重複切出）。
    2. **跨頁**（不同 Campaign）→ 另外要求整頁清單相同且該頁 2 筆以上，
       理由見 ``MIRROR_MIN_OFFERS``。被合併掉的網址記進留存那筆的 ``also_at``，
       使用者仍看得到「這個活動也出現在這些頁」，不是無聲丟棄。

    回傳新的 campaign 串列（不改動輸入）與一份可稽核的報告。
    """
    removed: Counter[str] = Counter()

    # 規則 1：同頁內去重。順帶把每頁的內容雜湊算出來給規則 2 用。
    staged: list[Campaign] = []
    keys: list[tuple[str, ...]] = []
    for campaign in campaigns:
        unique: dict[str, Offer] = {}
        for offer in campaign.offers:
            unique.setdefault(offer_content_key(offer), offer)
        dropped = len(campaign.offers) - len(unique)
        if dropped:
            removed[campaign.bank_id] += dropped
            campaign = campaign.model_copy(update={"offers": list(unique.values())})
        staged.append(campaign)
        keys.append(tuple(unique))

    # 規則 2：整頁清單相同的鏡射頁。簽章包含 bank_id 與**活動頁標題**。
    #
    # bank_id：跨銀行的「同內容」只會是兩家都沒解析出東西，那是巧合。
    #
    # 活動頁標題：這是「真鏡射」與「不同活動但抽取結果相同」唯一可靠的分界。
    # 實測第一銀行的「家電分期禮─全國電子／大同3C／三井3C」是三個**不同零售商**
    # 的活動頁，零售商名字只出現在頁面標題裡，切分後的子活動標題一律是
    # 「【家電分期禮】分期零利率 最高再享2,500元刷卡金」，期間與條件也完全一樣
    # —— 光看子活動內容無法分辨，合併掉會讓使用者永遠看不到大同與三井。
    # 聯邦的「屈臣氏」與「康是美」（同一頁不同 query 參數）也是同一回事。
    # 真鏡射則相反：星展六個子頁的標題全是「分期0%利率」、玉山兩個都是
    # 「玉山Pi拍錢包信用卡」，因為它們本來就是同一頁被掛在多個網址上。
    #
    # 代價是標題不一致的真鏡射不會被合併，那只是多顯示一筆；反過來誤併會刪掉
    # 一個真實活動頁，兩者不對稱。
    families: dict[tuple[str, str, tuple[str, ...]], list[int]] = {}
    for position, campaign in enumerate(staged):
        if len(campaign.offers) < MIRROR_MIN_OFFERS:
            continue
        families.setdefault(
            (campaign.bank_id, campaign.title, keys[position]), []
        ).append(position)

    dropped_positions: set[int] = set()
    updates: dict[int, Campaign] = {}
    mirrors: list[MirrorGroup] = []
    merged_campaigns = 0
    for members in families.values():
        if len(members) < 2:
            continue
        keep = min(members, key=lambda position: _canonical_first(staged[position].source_url))
        also_at = sorted(
            (staged[position].source_url for position in members if position != keep),
            key=_canonical_first,
        )
        kept = staged[keep]
        updates[keep] = kept.model_copy(
            update={
                "offers": [
                    offer.model_copy(update={"also_at": also_at}) for offer in kept.offers
                ]
            }
        )
        for position in members:
            if position != keep:
                dropped_positions.add(position)
                removed[staged[position].bank_id] += len(staged[position].offers)
        merged_campaigns += len(members) - 1
        mirrors.append(
            MirrorGroup(
                bank_id=kept.bank_id,
                kept_url=kept.source_url,
                also_at=also_at,
                offers=len(kept.offers),
            )
        )

    result = [
        updates.get(position, campaign)
        for position, campaign in enumerate(staged)
        if position not in dropped_positions
    ]
    report = DedupeReport(
        merged_offers=sum(removed.values()),
        merged_campaigns=merged_campaigns,
        per_source=dict(sorted(removed.items())),
        mirrors=sorted(mirrors, key=lambda group: (group.bank_id, group.kept_url)),
    )
    return result, report


def describe_dedupe(report: DedupeReport) -> list[str]:
    """給 CI job summary 用的說明。合併了什麼必須看得見，不能靜默生效。"""
    if not report.merged_offers:
        return ["去重：沒有發現重複的子活動"]
    lines = [
        f"去重：合併 {report.merged_offers} 筆重複子活動"
        f"（其中 {report.merged_campaigns} 個鏡射活動頁）",
    ]
    for bank_id, count in report.per_source.items():
        lines.append(f"  {bank_id} 合併 {count} 筆")
    for group in report.mirrors:
        lines.append(
            f"  鏡射頁 {group.bank_id}：保留 {group.kept_url}"
            f"（{group.offers} 筆），另見 {len(group.also_at)} 個網址"
        )
    return lines


def build_index(
    campaigns: list[Campaign],
    *,
    health: list[SourceHealth],
    alerts: list[Alert],
    generated_at: datetime,
    portals: dict[str, dict[str, str]] | None = None,
    dedupe: DedupeReport | None = None,
) -> dict[str, Any]:
    """首屏索引。

    ``portals`` 是每家銀行的登錄入口。實測 223 筆活動共用同一個 portal，
    逐筆輸出是純粹的重複 —— 提到來源層級。

    ``dedupe`` 有值時，``campaigns`` 是已去重的。此時計數刻意給出**兩個**數字，
    而且 ``offers`` 與 ``sources[].offer_count`` 保持「去重前」的舊語意：
    ``guard.assess`` 拿上一版 index.json 的這兩個欄位當涵蓋率基準，一旦它們
    改成去重後的數字，去重上線那次就會被記成一次憑空的筆數下降 —— 而逐來源
    防護的門檻是 40%，實測星展單一家就掉 36.8%（68→43），離誤觸只差一步。
    改欄位語意還有更糟的後果：下一次執行的基準變小，防護從此少偵測到一截
    真正的抓取退步。所以新增 ``unique_*``／``duplicate_offers`` 給網站顯示用，
    舊欄位一個字都不動。
    """
    pairs = [(campaign, offer) for campaign in campaigns for offer in campaign.offers]
    agenda = [
        agenda_entry(campaign, offer)
        for campaign, offer in pairs
        if offer.registration.windows
    ]
    actionable = [entry for entry in agenda if not entry["needs_review"]]
    portal_map = portals or {}
    merged = dedupe.merged_offers if dedupe else 0
    merged_by_source = dedupe.per_source if dedupe else {}
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "timezone": "Asia/Taipei",
        "counts": {
            "campaigns": len(campaigns) + (dedupe.merged_campaigns if dedupe else 0),
            # offers = 去重前，涵蓋率基準；unique_offers = 實際發布、與 catalog 筆數一致
            "offers": len(pairs) + merged,
            "unique_offers": len(pairs),
            "duplicate_offers": merged,
            "with_window": len(agenda),
            "actionable_with_window": len(actionable),
            "needs_review": sum(1 for _, offer in pairs if offer.needs_review),
            "registration_required": sum(
                1 for _, offer in pairs if offer.registration.required
            ),
        },
        "sources": [
            prune({
                "bank_id": item.bank_id,
                "bank_name": item.bank_name,
                "status": item.status,
                "entry_url": item.requested_url,
                "campaign_count": item.campaign_count,
                "offer_count": item.offer_count,
                "unique_offer_count": item.offer_count - merged_by_source.get(item.bank_id, 0),
                "message": item.message,
                "portal": portal_map.get(item.bank_id, {}),
            })
            for item in health
        ],
        "alerts": [prune(alert.model_dump()) for alert in alerts],
        "agenda": [prune(entry) for entry in agenda],
    }
    return payload


def build_catalog(bank_id: str, campaigns: list[Campaign]) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "bank_id": bank_id,
        "offers": [
            prune(catalog_entry(campaign, offer))
            for campaign in campaigns
            if campaign.bank_id == bank_id
            for offer in campaign.offers
        ],
    }
    return payload


def build_detail(campaign: Campaign) -> dict[str, Any]:
    """單一活動頁的 evidence 與登錄原文。展開該筆活動時才載入。

    刻意**不**收錄整頁原文。第一版把 ``terms_raw`` 寫進 detail，聯邦一家就
    1.18MB —— 展開一筆活動要載入整家銀行 92 頁的全文。而且把銀行頁面全文
    存進公開 repo 既無必要也不妥（使用者要看全文，點官方連結就是）。

    留下來的是「支撐每個解析結果的節錄」與登錄段落 —— 那才是使用者需要
    自行核對的東西。
    """
    payload = {
        "schema_version": SCHEMA_VERSION,
        "bank_id": campaign.bank_id,
        "campaign_id": campaign.id,
        "title": campaign.title,
        "url": campaign.source_url,
        "observed_at": campaign.observed_at.isoformat(),
        "offers": [
            prune(
                {
                    "id": offer.id,
                    "title": offer.title,
                    "period_evidence": (
                        offer.period.evidence.text if offer.period.evidence else ""
                    ),
                    "registration_raw": offer.registration.raw_text,
                    "window_evidence": [
                        window.evidence.text if window.evidence else ""
                        for window in offer.registration.windows
                    ],
                    "eligibility_evidence": (
                        offer.conditions.eligibility.evidence.text
                        if offer.conditions.eligibility.evidence
                        else ""
                    ),
                    "quota_text": offer.conditions.quota.text,
                    "review_reasons": offer.review_reasons,
                }
            )
            for offer in campaign.offers
        ],
    }
    return payload


def _fold(line: str) -> str:
    """RFC 5545 的 75 octet 折行。

    前身沒做，含網址的 DESCRIPTION 會超過限制 —— 多數客戶端容忍，
    嚴格的解析器會拒絕整個檔案。
    """
    if len(line.encode("utf-8")) <= 75:
        return line
    parts: list[str] = []
    current = b""
    for char in line:
        char_bytes = char.encode("utf-8")
        limit = 75 if not parts else 74  # 續行第一個字元是空白
        if len(current) + len(char_bytes) > limit:
            parts.append(current.decode("utf-8"))
            current = b""
        current += char_bytes
    if current:
        parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
    )


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _event_lines(
    campaign: Campaign,
    offer: Offer,
    window: RegistrationWindow,
    *,
    index: int,
    now: datetime,
    portal_url: str,
) -> list[str]:
    anchor = window.anchor
    is_deadline = window.kind == "deadline"
    label = "登錄截止" if is_deadline else "開放登錄"
    quota = offer.conditions.quota
    lead = QUOTA_REMINDER_LEAD_MINUTES if quota.limited else REMINDER_LEAD_MINUTES

    if is_deadline or window.end is None:
        event_end = anchor + timedelta(minutes=REMINDER_EVENT_MINUTES)
        end_note = "此為登錄截止時間" if is_deadline else "官方未公告登錄截止時間"
    else:
        event_end = window.end
        end_note = f"登錄至 {window.end:%Y-%m-%d %H:%M}"

    contract = offer.registration.timing_contract
    description = [
        f"[{label}] {campaign.bank_name}｜{offer.title}",
        end_note,
        f"時序：{contract.kind}",
    ]
    if quota.limited:
        description.append(f"限量 {quota.seats or '未公告數量'}，建議準時登錄")
    if offer.registration.portal.hint:
        description.append(offer.registration.portal.hint)
    description.append(f"官方頁：{campaign.source_url}")

    lines = [
        "BEGIN:VEVENT",
        f"UID:{offer.id}-{index}@tw-card-registration-radar",
        f"DTSTAMP:{_utc(now)}",
        f"DTSTART:{_utc(anchor)}",
        f"DTEND:{_utc(event_end)}",
        f"SUMMARY:[{label}] {_escape(campaign.bank_name)}｜{_escape(offer.title[:60])}",
        f"DESCRIPTION:{_escape(chr(10).join(description))}",
        f"URL:{portal_url or campaign.source_url}",
    ]
    if offer.registration.recurrence.kind == "monthly":
        # 每月重複登錄用 RRULE，不列舉成多個單次事件（實測 30.6% 屬此類）
        rule = "RRULE:FREQ=MONTHLY"
        if offer.period.end is not None:
            rule += f";UNTIL={offer.period.end:%Y%m%d}T235900Z"
        lines.append(rule)
    lines.extend(
        [
            "BEGIN:VALARM",
            f"TRIGGER:-PT{lead}M",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_escape(f'{label}：{offer.title[:40]}')}",
            "END:VALARM",
            "END:VEVENT",
        ]
    )
    return lines


def build_ics(campaigns: list[Campaign], *, now: datetime) -> str:
    """可訂閱的登錄提醒行事曆。

    只收錄可直接行動的活動 —— 讓使用者依據未確認的時間去搶登錄，
    比不提醒更糟。
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{CALENDAR_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:刷卡登錄雷達",
        "X-WR-TIMEZONE:Asia/Taipei",
    ]
    for campaign in campaigns:
        for offer in campaign.offers:
            if offer.needs_review or not offer.registration.required:
                continue
            portal_url = offer.registration.portal.url
            for position, window in enumerate(offer.registration.windows):
                lines.extend(
                    _event_lines(
                        campaign,
                        offer,
                        window,
                        index=position,
                        now=now,
                        portal_url=portal_url,
                    )
                )
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def portals_of(campaigns: list[Campaign]) -> dict[str, dict[str, str]]:
    """每家銀行的登錄入口。同一家的所有活動共用，提到來源層級避免逐筆重複。"""
    result: dict[str, dict[str, str]] = {}
    for campaign in campaigns:
        for offer in campaign.offers:
            portal = offer.registration.portal
            if portal.url and campaign.bank_id not in result:
                result[campaign.bank_id] = {
                    "url": portal.url,
                    "kind": portal.kind,
                    "hint": portal.hint,
                }
    return result


def write_site(
    root: Path,
    index: dict[str, Any],
    campaigns: list[Campaign],
    *,
    now: datetime,
) -> list[Path]:
    written: list[Path] = []
    data_dir = root / "data"
    catalog_dir = data_dir / "catalog"
    detail_dir = data_dir / "detail"
    calendar_dir = root / "calendar"
    for directory in (data_dir, catalog_dir, detail_dir, calendar_dir):
        directory.mkdir(parents=True, exist_ok=True)

    index_path = data_dir / "index.json"
    _write_json(index_path, index)
    written.append(index_path)

    for bank_id in sorted({campaign.bank_id for campaign in campaigns}):
        catalog_path = catalog_dir / f"{bank_id}.json"
        _write_json(catalog_path, build_catalog(bank_id, campaigns))
        written.append(catalog_path)

    for campaign in campaigns:
        bank_dir = detail_dir / campaign.bank_id
        bank_dir.mkdir(parents=True, exist_ok=True)
        detail_path = bank_dir / f"{campaign.id}.json"
        _write_json(detail_path, build_detail(campaign))
        written.append(detail_path)

    calendar_path = calendar_dir / "registration.ics"
    calendar_path.write_text(build_ics(campaigns, now=now), encoding="utf-8")
    written.append(calendar_path)
    return written


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    # 機器讀的檔案用 compact 格式。實測縮排讓聯邦的輸出多了 24%。
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
