"""把來源設定跑成 Campaign / Offer。

三個設計重點，都直接對應前身實測到的問題：

**逐筆容錯。** 前身在單一筆連結未通過白名單時例外往上冒，導致整次更新
exit 1、其餘 16 家一起失敗（實測 2026-08-03 就是這樣中斷的）。這裡每一筆
明細各自捕捉例外，記錄成警示後繼續。

**活動粒度。** 一個明細頁產生多個 Offer。無法切出邊界時標記 needs_review，
不把多個子活動的登錄時點合併成一筆。

**明細快取。** 清單指紋未變、距上次檢查未滿 30 天、且不在活動起訖日前後
3 天內時，沿用上一版的 Offer，不重讀明細。前身實測此策略省下 798 次讀取。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from .adapters.listing import Fetch, ListingItem, read_listing
from .htmltext import strings_of, to_text
from .invariants import apply as apply_invariants
from .models import (
    Alert,
    Campaign,
    Evidence,
    Offer,
    Period,
    Portal,
    Registration,
    SourceHealth,
)
from .parse import conditions as cond
from .parse.contract import derive
from .parse.datetimes import detect_recurrence, drop_period_echoes, find_period, find_windows
from .segment import registration_text, split_offers, table_rows
from .spec import SourceSpec
from .transport import BlockedURL, FetchFailed, TransportError

CACHE_MAX_AGE_DAYS = 30
BOUNDARY_REFRESH_DAYS = 3


@dataclass
class RunStats:
    detail_fetched: int = 0
    detail_reused: int = 0
    detail_failed: int = 0
    detail_blocked: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "detail_fetched": self.detail_fetched,
            "detail_reused": self.detail_reused,
            "detail_failed": self.detail_failed,
            "detail_blocked": self.detail_blocked,
        }


@dataclass
class SourceResult:
    campaigns: list[Campaign] = field(default_factory=list)
    health: SourceHealth | None = None
    alerts: list[Alert] = field(default_factory=list)
    stats: RunStats = field(default_factory=RunStats)

    @property
    def offer_count(self) -> int:
        return sum(len(campaign.offers) for campaign in self.campaigns)


def build_offers(
    spec: SourceSpec,
    *,
    url: str,
    html: str,
    text: str,
    today: date,
    listing_start: date | None = None,
    listing_end: date | None = None,
) -> list[Offer]:
    """把單一明細頁的文字切成多個子活動並解析。

    ``listing_start``/``listing_end`` 是清單層級已知的期間，用來在明細頁
    抓不到期間時補位 —— 但只補位，不覆蓋明細頁自己寫的期間。
    """
    headers, rows = table_rows(html) if spec.detail.table_tiers else ([], [])
    known_cards = tuple(spec.conditions.known_cards)
    offers: list[Offer] = []

    for index, chunk in enumerate(split_offers(text, pattern=spec.detail.boundary or None)):
        start, end, confidence, evidence = find_period(
            chunk.text, default_year=today.year, reference=today
        )
        if start is None and listing_start is not None:
            # 清單層級的期間是可靠但較粗的來源（不區分子活動），
            # 給固定的中等信心，不要沿用「明細頁抓不到」的 0.0
            start, confidence = listing_start, 0.5
        if end is None and listing_end is not None:
            end = listing_end
            confidence = min(confidence, 0.5) if confidence else 0.5
        period = Period(
            start=start,
            end=end,
            confidence=confidence,
            evidence=Evidence(text=evidence, source_url=url) if evidence else None,
        )
        windows = drop_period_echoes(
            find_windows(chunk.text, default_year=today.year, reference=today, source_url=url),
            start,
            end,
        )
        recurrence = detect_recurrence(chunk.text)
        offer = Offer(
            id=f"{spec.id}-{_slug(url)}-{index}",
            title=chunk.title,
            period=period,
            registration=Registration(
                required=bool(windows) or "登錄" in chunk.text,
                windows=windows,
                recurrence=recurrence,
                portal=_portal(spec),
                timing_contract=derive(
                    period=period,
                    windows=windows,
                    recurrence=recurrence,
                    raw_text=chunk.text,
                ),
                raw_text=registration_text(chunk.text)[:1500],
            ),
            conditions=cond.extract(
                chunk.text,
                known_cards=known_cards,
                table_headers=headers or None,
                table_rows=rows or None,
            ),
        )
        # spec 明說這家是單頁多活動，卻切不出邊界 —— 不能假裝切好了
        extra = (
            ("offer_boundary_missing",)
            if not chunk.split and spec.detail.cardinality == "many"
            else ()
        )
        offers.append(apply_invariants(offer, extra_codes=extra))
    return offers


def _portal(spec: SourceSpec) -> Portal:
    return Portal(
        url=spec.registration.portal_url,
        kind=spec.registration.portal_kind,
        hint=spec.registration.portal_hint,
    )


def _slug(url: str) -> str:
    import hashlib

    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]


def _can_reuse(previous: Campaign, fingerprint: str, now: datetime, today: date) -> bool:
    if previous.content_hash != fingerprint:
        return False
    if now - previous.observed_at > timedelta(days=CACHE_MAX_AGE_DAYS):
        return False
    margin = timedelta(days=BOUNDARY_REFRESH_DAYS)
    for offer in previous.offers:
        for boundary in (offer.period.start, offer.period.end):
            if boundary is not None and abs(boundary - today) <= margin:
                return False
    return True


def run_source(
    spec: SourceSpec,
    fetcher: Fetch,
    *,
    today: date,
    now: datetime | None = None,
    previous: dict[str, Campaign] | None = None,
) -> SourceResult:
    """讀取一個來源。任何單筆問題都不會讓整個來源歸零。"""
    moment = now or datetime.now(UTC)
    cache = previous or {}
    result = SourceResult()

    try:
        items = read_listing(spec, fetcher)
    except BlockedURL as exc:
        result.alerts.append(
            Alert(
                type="source_emitted_invalid_url",
                bank_id=spec.id,
                bank_name=spec.bank_name,
                message=f"官方清單提供了不可信任的位址：{exc}",
                url=spec.listing.entry_url,
            )
        )
        result.health = _health(spec, "blocked", result, str(exc))
        return result
    except TransportError as exc:
        result.alerts.append(
            Alert(
                type="source_failed",
                bank_id=spec.id,
                bank_name=spec.bank_name,
                message=f"官方活動清單暫時無法讀取：{exc}",
                url=spec.listing.entry_url,
            )
        )
        result.health = _health(spec, "failed", result, str(exc))
        return result

    for item in items:
        campaign = _run_item(spec, fetcher, item, today, moment, cache, result)
        if campaign is not None and campaign.offers:
            result.campaigns.append(campaign)

    status = _status(result, len(items))
    result.health = _health(spec, status, result, _message(result))
    return result


def _run_item(
    spec: SourceSpec,
    fetcher: Fetch,
    item: ListingItem,
    today: date,
    moment: datetime,
    cache: dict[str, Campaign],
    result: SourceResult,
) -> Campaign | None:
    fingerprint = item.fingerprint
    cached = cache.get(item.url)
    if cached is not None and _can_reuse(cached, fingerprint, moment, today):
        result.stats.detail_reused += 1
        refreshed = cached.model_copy(update={"observed_at": moment})
        return refreshed

    html = ""
    text = f"{item.title}\n{item.summary}"
    if spec.detail.source == "html":
        # 條件式 GET 只在「有上一版可沿用」且「清單指紋未變」時才用得上：
        # 304 不會帶回內容，唯一能做的就是沿用上一版。若清單指紋變了（標題或
        # 期間改了），即使頁面本身沒變也要拿到完整內容重新推導。
        conditional = cached is not None and cached.content_hash == fingerprint
        try:
            response = fetcher.get(item.url, conditional=conditional)
            if response.not_modified:
                # 走到這裡表示快取因效期或活動邊界而過期，但頁面內容未變
                assert cached is not None
                result.stats.detail_reused += 1
                return cached.model_copy(update={"observed_at": moment})
            result.stats.detail_fetched += 1
            html = response.text
            text = to_text(html) or text
        except BlockedURL as exc:
            result.stats.detail_blocked += 1
            result.alerts.append(
                Alert(
                    type="source_emitted_invalid_url",
                    bank_id=spec.id,
                    bank_name=spec.bank_name,
                    message=f"官方頁提供了不可信任的活動連結，已略過該筆：{exc}",
                    url=item.url,
                )
            )
            return None
        except FetchFailed as exc:
            result.stats.detail_failed += 1
            result.alerts.append(
                Alert(
                    type="detail_unreadable",
                    bank_id=spec.id,
                    bank_name=spec.bank_name,
                    message=f"活動明細暫時無法讀取，改用清單資訊：{exc}",
                    url=item.url,
                )
            )
    elif spec.detail.source == "api" and item.props is not None:
        # 明細頁為 SPA、純文字只有數十字（實測國泰世華 31 字），
        # 唯一可用的內容在清單 API 的附加欄位裡
        text = _props_text(item) or text

    offers = build_offers(
        spec,
        url=item.url,
        html=html,
        text=text,
        today=today,
        listing_start=item.start,
        listing_end=item.end,
    )
    return Campaign(
        id=f"{spec.id}-{_slug(item.url)}",
        bank_id=spec.id,
        bank_name=spec.bank_name,
        title=item.title or (offers[0].title if offers else item.url),
        source_url=item.url,
        observed_at=moment,
        offers=offers,
        terms_raw=text[:20000],
        content_hash=fingerprint,
    )


def _props_text(item: ListingItem) -> str:
    return "\n".join(strings_of(item.props))


def _status(result: SourceResult, item_count: int) -> str:
    if not result.campaigns:
        return "failed"
    stats = result.stats
    if stats.detail_failed or stats.detail_blocked or len(result.campaigns) < item_count:
        return "partial"
    return "complete"


def _message(result: SourceResult) -> str:
    stats = result.stats
    parts: list[str] = []
    if stats.detail_failed:
        parts.append(f"{stats.detail_failed} 筆明細暫時無法讀取")
    if stats.detail_blocked:
        parts.append(f"{stats.detail_blocked} 筆連結未通過安全檢查已略過")
    if stats.detail_reused:
        parts.append(f"沿用快取 {stats.detail_reused} 筆")
    return "；".join(parts)


def _health(spec: SourceSpec, status: str, result: SourceResult, message: str) -> SourceHealth:
    return SourceHealth(
        bank_id=spec.id,
        bank_name=spec.bank_name,
        requested_url=spec.listing.entry_url,
        status=status,  # type: ignore[arg-type]
        campaign_count=len(result.campaigns),
        offer_count=result.offer_count,
        checked_at=datetime.now(UTC),
        message=message,
    )
