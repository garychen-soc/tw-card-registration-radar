"""把來源設定跑成 Campaign / Offer。

三個設計重點，都直接對應前身實測到的問題：

**逐筆容錯。** 前身在單一筆連結未通過白名單時例外往上冒，導致整次更新
exit 1、其餘 16 家一起失敗（實測 2026-08-03 就是這樣中斷的）。這裡每一筆
明細各自捕捉例外，記錄成警示後繼續。

**活動粒度。** 一個明細頁產生多個 Offer。無法切出邊界時標記 needs_review，
不把多個子活動的登錄時點合併成一筆。

**不快取解析結果。** 前身（以及本專案第一版）快取的是推導出來的活動資料，
清單指紋未變就沿用上一版的 Offer。那會讓解析器的修正無法傳播 —— 今天修好
``HH:MM:SS``，指紋未變的頁面還會掛著錯的登錄時間最多 30 天，正是「衍生狀態
被凍結」那一類問題。這裡一律用當下的解析器重新推導，只快取**輸入**
（頁面原始 HTML，見 ``pagestore``），修正立即生效於全部頁面。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from .adapters.listing import Fetch, ListingItem, read_listing
from .htmltext import scope_html, strings_of, to_text
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
from .pagestore import PageStore
from .parse import conditions as cond
from .parse.contract import derive
from .parse.datetimes import (
    detect_recurrence,
    drop_period_echoes,
    find_date_range,
    find_period,
    find_windows,
)
from .segment import (
    looks_multi_offer,
    registration_text,
    single_chunk,
    split_offers,
    table_rows,
)
from .spec import SourceSpec
from .transport import AccessDenied, BlockedURL, FetchFailed, TransportError


@dataclass
class RunStats:
    detail_fetched: int = 0
    detail_not_modified: int = 0
    """伺服器回 304、改用本機存的 HTML 重新推導的次數。"""
    detail_failed: int = 0
    detail_blocked: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "detail_fetched": self.detail_fetched,
            "detail_not_modified": self.detail_not_modified,
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
    listing_title: str = "",
    listing_start: date | None = None,
    listing_end: date | None = None,
) -> list[Offer]:
    """把單一明細頁的文字切成多個子活動並解析。

    ``listing_start``/``listing_end`` 是清單層級已知的期間，用來在明細頁
    抓不到期間時補位 —— 但只補位，不覆蓋明細頁自己寫的期間。

    ``listing_title`` 是清單卡片上的標題。未切分時優先採用它 —— 明細頁的
    第一行常是頁面樣板（實測元大是「元大行動銀行」App 推廣區塊），
    清單標題才是這個活動真正的名字。
    """
    headers, rows = table_rows(html) if spec.detail.table_tiers else ([], [])
    known_cards = tuple(spec.conditions.known_cards)
    offers: list[Offer] = []

    # 頁面層級的活動期間。單頁多活動時它常寫在第一個子活動之前的前言裡，
    # 而 split_offers 只保留各邊界之後的內容 —— 星展的頁面就是這樣：
    # 「活動期間：2026/8/1~2026/8/31」在「活動一」之前，導致 68 筆全都
    # 抓不到期間。子活動沒寫自己的期間時，繼承頁面層級的。
    page_start, page_end, page_confidence, page_evidence = find_period(
        text, default_year=today.year, reference=today
    )

    # 遵守 spec 宣告的 cardinality。宣告 one 就不切 —— 否則樣板文字裡的
    # 【】之類符號會把單一活動頁切成好幾塊。
    chunks = (
        split_offers(text, pattern=spec.detail.boundary or None)
        if spec.detail.cardinality == "many"
        else single_chunk(text)
    )
    multi_evidence = spec.detail.cardinality == "many" and looks_multi_offer(text)

    for index, chunk in enumerate(chunks):
        start, end, confidence, evidence = find_period(
            chunk.text, default_year=today.year, reference=today
        )
        # 有標籤的期間抓不到時，退而找裸日期區間（例如富邦明細頁直接印
        # 一行 2026/01/01~2026/12/31）。信心較低，且只看文字開頭。
        if start is None:
            start, end, confidence, evidence = find_date_range(
                chunk.text, default_year=today.year, reference=today
            )

        # 後備順序：子活動自己寫的 → 頁面層級 → 清單層級。
        # 每退一層信心就降低，因為粒度越粗、越可能不是這個子活動的真實期間。
        if start is None and page_start is not None:
            start, end = page_start, end or page_end
            confidence = min(page_confidence, 0.6)
            evidence = evidence or page_evidence
        if start is None and listing_start is not None:
            start, confidence = listing_start, 0.5
        if end is None and page_end is not None:
            end = page_end
            confidence = min(confidence or 0.6, 0.6)
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
        title = listing_title if (listing_title and not chunk.split) else chunk.title
        offer = Offer(
            id=f"{spec.id}-{_slug(url)}-{index}",
            title=title,
            period=period,
            registration=Registration(
                required=bool(windows) or cond.requires_registration(chunk.text),
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
        # 只在「有多活動的正面證據卻切不出邊界」時才標記。宣告 many 的來源
        # 裡有很多頁其實只有一個活動（實測玉山 182 頁中 170 頁如此），
        # 一律標記會把需人工確認的量灌到失去意義。
        extra = ("offer_boundary_missing",) if not chunk.split and multi_evidence else ()
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


def run_source(
    spec: SourceSpec,
    fetcher: Fetch,
    *,
    today: date,
    now: datetime | None = None,
    pages: PageStore | None = None,
) -> SourceResult:
    """讀取一個來源。任何單筆問題都不會讓整個來源歸零。"""
    moment = now or datetime.now(UTC)
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
    except AccessDenied as exc:
        result.alerts.append(
            Alert(
                type="source_access_blocked",
                bank_id=spec.id,
                bank_name=spec.bank_name,
                message=f"來源拒絕自動化存取，本次未取得資料：{exc}",
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
        campaign = _run_item(spec, fetcher, item, today, moment, pages, result)
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
    pages: PageStore | None,
    result: SourceResult,
) -> Campaign | None:
    html = ""
    text = f"{item.title}\n{item.summary}"
    if spec.detail.source == "html":
        # 只在本機已有這頁的 HTML 時才發條件式請求 —— 304 不帶 body，
        # 沒有存檔可對照就無事可做。
        stored = pages.get(item.url) if pages else None
        try:
            response = fetcher.get(item.url, conditional=stored is not None)
            if response.not_modified and stored is not None:
                result.stats.detail_not_modified += 1
                html = stored
            else:
                result.stats.detail_fetched += 1
                html = response.text
                # 只快取伺服器提供驗證標頭的頁面。沒有 ETag／Last-Modified
                # 就永遠不會收到 304，存了也用不到。
                if pages is not None and _has_validators(fetcher, item.url):
                    pages.put(item.url, html, cache_control=response.cache_control)
            html = scope_html(
                html,
                selector=spec.detail.scope_selector,
                tab_label=spec.detail.scope_tab_label,
            )
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
        except AccessDenied as exc:
            result.stats.detail_blocked += 1
            result.alerts.append(
                Alert(
                    type="source_access_blocked",
                    bank_id=spec.id,
                    bank_name=spec.bank_name,
                    message=f"來源拒絕自動化存取，已略過該筆：{exc}",
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
        listing_title=item.title,
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
        content_hash=item.fingerprint,
    )


def _props_text(item: ListingItem) -> str:
    return "\n".join(strings_of(item.props))


def _status(result: SourceResult, item_count: int) -> str:
    if not result.campaigns:
        # 全被拒絕存取時要說「被拒」而不是「失敗」—— 前者是換執行環境能解的，
        # 後者是來源本身有問題。混在一起就失去了「該不該改走 self-hosted
        # runner」的判斷依據（實測陽信在 CI 上就是這種情形）。
        if result.stats.detail_blocked and not result.stats.detail_failed:
            return "blocked"
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
        parts.append(f"{stats.detail_blocked} 筆被拒絕存取或未通過安全檢查已略過")
    if stats.detail_not_modified:
        parts.append(f"{stats.detail_not_modified} 筆官方回報未變更，改用本機存檔")
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


def _has_validators(fetcher: Fetch, url: str) -> bool:
    cache = getattr(fetcher, "cache", None)
    checker = getattr(cache, "has_validators", None)
    return bool(checker(url)) if callable(checker) else False
