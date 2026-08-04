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

**兩條輸出規則。**

1. 不含任何時間衍生狀態。「是否進行中」「今日待登錄」全由讀取端算。
   ``agenda`` 的收錄條件是「有登錄視窗」這個絕對事實，不是「未來 14 天」
   這種相對條件 —— 否則資料放三天就會漏掉該提醒的活動。
2. 預設值不輸出。223 筆裡絕大多數的 ``threshold_tiers``、``cards``、
   資格旗標都是空的或 false，逐筆輸出純粹是浪費。讀取端把「缺少」
   視為預設值。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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


def build_index(
    campaigns: list[Campaign],
    *,
    health: list[SourceHealth],
    alerts: list[Alert],
    generated_at: datetime,
    portals: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """首屏索引。

    ``portals`` 是每家銀行的登錄入口。實測 223 筆活動共用同一個 portal，
    逐筆輸出是純粹的重複 —— 提到來源層級。
    """
    pairs = [(campaign, offer) for campaign in campaigns for offer in campaign.offers]
    agenda = [
        agenda_entry(campaign, offer)
        for campaign, offer in pairs
        if offer.registration.windows
    ]
    actionable = [entry for entry in agenda if not entry["needs_review"]]
    portal_map = portals or {}
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "timezone": "Asia/Taipei",
        "counts": {
            "campaigns": len(campaigns),
            "offers": len(pairs),
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
