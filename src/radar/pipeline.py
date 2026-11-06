"""一輪完整更新：擷取 → 合併 → LLM 定位 → 換算驗證 → 沿用與防護 → 寫入 state。

state/activities/<bank>.json 是唯一正本（進版控），網站資料由 ``radar.emit``
從它產生。來源失敗時沿用上一輪仍有效的活動並標 stale，而不是讓整家銀行
從網站消失；但也不把舊資料冒充成新資料。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import banks
from .extract.locate import (
    DEFAULT_MODEL,
    AuthExpired,
    LocateError,
    Meter,
    Request,
    cached,
    locate,
)
from .extract.schedule import Context, build, squash
from .models import Activity, SourceHealth, Stub, SubActivity
from .sources import open_adapter
from .textnorm import normalize
from .transport import AccessDenied, TransportError

log = logging.getLogger(__name__)
TAIPEI = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "state"
DROP_LIMIT = 0.4
MIN_TERMS_CHARS = 80
REGISTRATION_WORDS = re.compile(r"登錄|登記|報名")
GENERIC_TITLE = re.compile(
    r"^(?:優惠內容|活動內容|活動說明|活動辦法|注意事項|共同注意事項|關於我們|活動詳情|"
    r"更多優惠|了解更多|立即登錄|活動登錄)[:：]?$"
)


@dataclass
class Fetched:
    bank_id: str
    health: SourceHealth
    docs: list[tuple[Stub, str]] = field(default_factory=list)
    # 本輪讀取不完整的線索，供 _carry_missing 判斷哪些舊活動該沿用
    failed_urls: set[str] = field(default_factory=set)
    category_failed: bool = False


@dataclass
class RunResult:
    observed: datetime
    activities: dict[str, list[Activity]]
    health: list[SourceHealth]
    llm_calls: int
    llm_failures: int
    publishable: bool
    messages: list[str]
    usage: dict[str, Any] = field(default_factory=dict)


# ---- 擷取 ----------------------------------------------------------------


def fetch_bank(bank_id: str) -> Fetched:
    cls = banks.load(bank_id)
    health = SourceHealth(bank_id=bank_id, bank_name=cls.name, status="ok", entry_url=cls.entry_url)
    out = Fetched(bank_id, health)
    try:
        with open_adapter(bank_id) as adapter:
            stubs = _dedupe(adapter.discover())
            health.stubs = len(stubs)
            failures = 0
            for stub in stubs:
                if stub.detail_text is not None:
                    out.docs.append((stub, stub.detail_text))
                    continue
                try:
                    terms = adapter.terms(stub)
                    if stub.period_end is None and terms.period_end is not None:
                        # 列表沒有期間、明細有（玉山）：以明細為準
                        stub = stub.model_copy(
                            update={
                                "period_start": terms.period_start,
                                "period_end": terms.period_end,
                            }
                        )
                    out.docs.append((stub, terms.text))
                except TransportError as exc:
                    failures += 1
                    out.failed_urls.add(stub.url)
                    log.warning("%s 明細失敗 %s：%s", bank_id, stub.url, exc)
            health.detail_failures = failures
            partial = [s for s in adapter.skipped if s.startswith("分類失敗")]
            out.category_failed = bool(partial)
            if partial or (stubs and failures > len(stubs) * 0.2):
                health.status = "partial"
                health.message = "；".join(partial[:3]) or f"{failures} 筆明細讀取失敗"
            elif failures:
                # 比例不高仍判 ok，但要讓人看得到缺口（中信 mkt 站 11 頁卡 JS 驗證，曾顯示「正常」）
                health.message = f"{failures} 頁明細讀不到，這些活動未收錄"
            if not stubs:
                health.status = "failed"
                health.message = "列表為空"
    except AccessDenied as exc:
        health.status, health.message = "blocked", str(exc)[:200]
    except Exception as exc:
        health.status, health.message = "failed", f"{type(exc).__name__}: {exc}"[:200]
    return out


DOCS = ROOT / "var" / "docs"
# 每輪 LLM 用量一行（只留本機，`radar usage` 讀它）
USAGE_LOG = ROOT / "var" / "usage.jsonl"


def save_docs(item: Fetched) -> None:
    """本輪抓到的條款原文存在本機（不進版控），供換模型重算或除錯時免重抓官網。"""
    DOCS.mkdir(parents=True, exist_ok=True)
    payload = {
        "health": item.health.model_dump(mode="json"),
        "docs": [{"stub": s.model_dump(mode="json"), "text": t} for s, t in item.docs],
        "failed_urls": sorted(item.failed_urls),
        "category_failed": item.category_failed,
    }
    (DOCS / f"{item.bank_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def load_docs(bank_id: str) -> Fetched:
    payload = json.loads((DOCS / f"{bank_id}.json").read_text(encoding="utf-8"))
    item = Fetched(bank_id, SourceHealth.model_validate(payload["health"]))
    item.docs = [(Stub.model_validate(d["stub"]), d["text"]) for d in payload["docs"]]
    item.failed_urls = set(payload.get("failed_urls", []))
    item.category_failed = bool(payload.get("category_failed", False))
    return item


def _dedupe(stubs: Iterable[Stub]) -> list[Stub]:
    seen: set[str] = set()
    out: list[Stub] = []
    for stub in stubs:
        if stub.key not in seen:
            seen.add(stub.key)
            out.append(stub)
    return out


def content_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", "", normalize(text)).encode("utf-8")).hexdigest()[:16]


# ---- 組活動 ----------------------------------------------------------------


@dataclass
class Doc:
    bank_id: str
    stubs: list[Stub]
    text: str
    digest: str

    @property
    def primary(self) -> Stub:
        return self.stubs[0]

    def request(self) -> Request:
        stub = self.primary
        period = f"{stub.period_start or '?'}~{stub.period_end or '?'}"
        return Request(title=stub.title, period=period, text=self.text)


def group_docs(fetched: Fetched, today: date | None = None) -> list[Doc]:
    """同一家銀行、條款文字相同的多張卡片合併成一筆活動，其餘標題留作別名。

    列表公告的結束日已過的活動直接略過 —— 不發布，也不浪費 LLM 額度。
    """
    groups: dict[str, Doc] = {}
    for stub, text in fetched.docs:
        if today and stub.period_end and stub.period_end < today:
            continue
        digest = content_hash(text) if len(text) >= 200 else content_hash(stub.key)
        doc = groups.get(digest)
        if doc is None:
            groups[digest] = Doc(fetched.bank_id, [stub], text, digest)
        else:
            doc.stubs.append(stub)
    for doc in groups.values():
        # 最具描述性（最長）的標題當主標題；同一份條款涵蓋多個通路時標明家數
        doc.stubs.sort(key=lambda s: (-len(s.title), s.key))
    return list(groups.values())


_LIST_NUMBER = re.compile(r"^\d{1,3}[.、](?!\d)\s*")


def _group_title(doc: Doc, owner: str | None = None) -> str:
    # 列表連結文字常帶序號（彰銀「22.【百貨購物】…」）；「11.11」這類日期不受影響
    primary = _LIST_NUMBER.sub("", owner or doc.primary.title)
    titles = {s.title for s in doc.stubs}
    if len(titles) >= 3:
        return f"{primary} 等 {len(titles)} 個通路"
    return primary


MIN_OWNER_CHARS = 6
MIN_CROWD_CHARS = 3
LIST_WINDOW = 120


def _owner_title(doc: Doc, subs: list[SubActivity]) -> str | None:
    """多張列表卡共用同一頁時，找出登錄時點真正屬於哪一張卡。

    主標題原本取最長的卡名，但共用頁常是多個活動並列（華南：Klook、可樂旅遊、
    機場接送、華南大玩家同一頁），時點就會掛到不相干的卡名下。作法：在原文中找每個
    有時點的子活動引句的位置，往前找最近出現的卡名當它的主人。保守起見，以下情況
    一律維持原標題：
    - 原主標題就出現在引句之前（它本身就是合理的段落標題，例：聯邦 Uber Eats）；
    - 候選卡名周圍擠著其他卡名（是商家清單裡的一項，不是段落標題，例：星展 40 通路）；
    - 找不到位置，或各子活動指向不同卡名。
    """
    titles = {s.title for s in doc.stubs}
    if len(titles) < 2:
        return None
    text = squash(doc.text)
    primary_key = squash(_LIST_NUMBER.sub("", doc.primary.title))
    # 所有卡名的出現位置（判斷「商家清單」用）；只有夠長的卡名可以當主人
    every: list[tuple[int, str]] = []
    for title in titles:
        key = squash(_LIST_NUMBER.sub("", title))
        if len(key) < MIN_CROWD_CHARS:
            continue
        start = text.find(key)
        while start != -1:
            every.append((start, title))
            start = text.find(key, start + 1)
    marks = [
        (p, t) for p, t in every if len(squash(_LIST_NUMBER.sub("", t))) >= MIN_OWNER_CHARS
    ]
    if not marks:
        return None
    owners: set[str] = set()
    for sub in subs:
        if not sub.slots:
            continue
        pos = text.find(squash(sub.slots[0].evidence.quote))
        if pos < 0 and sub.name:
            pos = text.find(squash(sub.name))
        if pos < 0:
            return None
        primary_at = text.find(primary_key) if primary_key else -1
        if 0 <= primary_at <= pos:
            return None
        before = [mark for mark in marks if mark[0] <= pos]
        if not before:
            return None
        at, owner = max(before)
        crowd = {t for p, t in every if t != owner and abs(p - at) <= LIST_WINDOW}
        if len(crowd) >= 2:
            return None
        owners.add(owner)
    if len(owners) != 1:
        return None
    owner = owners.pop()
    return None if owner == doc.primary.title else owner


def activity_id(bank_id: str, key: str) -> str:
    return f"{bank_id}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]}"


def assemble(doc: Doc, output: dict[str, Any] | None, failure: str, observed: datetime) -> Activity:
    cls = banks.load(doc.bank_id)
    stub = doc.primary
    review: list[str] = []
    activity = Activity(
        id=activity_id(doc.bank_id, stub.key),
        bank_id=doc.bank_id,
        bank_name=cls.name,
        title=_group_title(doc),
        aliases=sorted({s.title for s in doc.stubs} - {stub.title}),
        url=stub.url,
        period_start=stub.period_start,
        period_end=stub.period_end,
        summary=stub.summary,
        category=stub.category,
        portal_url=cls.portal_url or cls.entry_url,
        portal_hint=cls.portal_hint,
        content_hash=doc.digest,
        observed_at=observed,
    )
    if GENERIC_TITLE.match(stub.title) or stub.title == cls.name or len(stub.title) < 2:
        review.append("標題疑似不是活動名稱")
    if output is not None:
        ctx = Context(
            text=doc.text,
            url=stub.url,
            period_start=stub.period_start,
            period_end=stub.period_end,
            observed=observed,
        )
        result = build(output, ctx)
        activity.registration = result.registration
        activity.registration_quote = result.registration_quote
        activity.subs = result.subs
        review.extend(result.review)
        owner = _owner_title(doc, result.subs)
        if owner is not None:
            # id 仍取自原主卡（ICS 的 UID 不變），只換顯示標題
            activity.title = _group_title(doc, owner)
            activity.aliases = sorted({s.title for s in doc.stubs} - {owner})
    elif failure:
        activity.registration = "unknown"
        review.append(failure)
    elif len(doc.text.strip()) < MIN_TERMS_CHARS:
        # 圖片型頁面、外站 SPA、已下架頁：讀不到文字不等於不需登錄
        activity.registration = "unknown"
        review.append("官方頁面沒有可讀的條款文字（可能是圖片或已下架），請直接查看活動頁")
    else:
        activity.registration = "not_required"
    activity.review = review
    return activity


# ---- 一輪 ----------------------------------------------------------------


def previous(bank_id: str) -> list[Activity]:
    path = STATE / "activities" / f"{bank_id}.json"
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [Activity.model_validate(row) for row in rows]


def run(
    bank_ids: list[str] | None = None,
    *,
    model: str = DEFAULT_MODEL,
    llm_workers: int = 3,
    llm_budget: int = 200,
    fetch_workers: int = 6,
    observed: datetime | None = None,
    reuse_docs: bool = False,
) -> RunResult:
    observed = observed or datetime.now(TAIPEI).replace(microsecond=0)
    bank_ids = bank_ids or banks.available()
    messages: list[str] = []

    fetched: dict[str, Fetched] = {}
    if reuse_docs:
        fetched = {b: load_docs(b) for b in bank_ids}
    else:
        http_banks = [b for b in bank_ids if banks.load(b).transport == "http"]
        browser_banks = [b for b in bank_ids if b not in http_banks]
        with ThreadPoolExecutor(max_workers=fetch_workers) as pool:
            for result in pool.map(fetch_bank, http_banks):
                fetched[result.bank_id] = result
        for bank_id in browser_banks:
            fetched[bank_id] = fetch_bank(bank_id)
        for item in fetched.values():
            save_docs(item)

    docs = [doc for bank_id in bank_ids for doc in group_docs(fetched[bank_id], observed.date())]
    outputs: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    pending: list[Doc] = []
    for doc in docs:
        if not REGISTRATION_WORDS.search(doc.text):
            continue
        hit = cached(doc.request(), model)
        if hit is not None:
            outputs[doc.digest] = hit
        else:
            pending.append(doc)
    if len(pending) > llm_budget:
        messages.append(f"LLM 待抽取 {len(pending)} 筆，超出本輪額度 {llm_budget}，其餘下輪處理")
        for doc in pending[llm_budget:]:
            failures[doc.digest] = "登錄規則本輪尚未抽取（超出額度，下輪處理）"
        pending = pending[:llm_budget]
    calls = 0
    auth_failed = False
    meter = Meter()
    with ThreadPoolExecutor(max_workers=llm_workers) as pool:
        futures = {pool.submit(locate, doc.request(), model, meter): doc for doc in pending}
        for future in as_completed(futures):
            doc = futures[future]
            if future.cancelled():
                failures[doc.digest] = "登錄規則本輪尚未抽取（下輪處理）"
                continue
            calls += 1
            try:
                outputs[doc.digest] = future.result()
            except AuthExpired as exc:
                failures[doc.digest] = "登錄規則本輪尚未抽取（下輪處理）"
                if not auth_failed:
                    auth_failed = True
                    messages.append(str(exc))
                    for other in futures:
                        other.cancel()
            except LocateError as exc:
                failures[doc.digest] = "登錄規則抽取失敗，請查看官方頁"
                log.warning("LLM 失敗 %s：%s", doc.primary.url, exc)

    activities: dict[str, list[Activity]] = {}
    health: list[SourceHealth] = []
    for bank_id in bank_ids:
        item = fetched[bank_id]
        built = [
            assemble(doc, outputs.get(doc.digest), failures.get(doc.digest, ""), observed)
            for doc in docs
            if doc.bank_id == bank_id
        ]
        built = _carry_unextracted(built, bank_id)
        old = [a for a in previous(bank_id) if _still_valid(a, observed)]
        fresh_old = [a for a in old if not a.stale]
        if item.health.status in ("failed", "blocked"):
            built = [a.model_copy(update={"stale": True}) for a in old]
            item.health.carried = len(built)
        elif fresh_old and len(built) < len(fresh_old) * (1 - DROP_LIMIT):
            item.health.status = "partial"
            item.health.message = (
                f"筆數由 {len(fresh_old)} 掉到 {len(built)}（超過 {int(DROP_LIMIT * 100)}%），"
                "沿用上一輪資料"
            )
            built = [a.model_copy(update={"stale": True}) for a in old]
            item.health.carried = len(built)
        else:
            built, carried = _carry_missing(built, old, item)
            if carried:
                item.health.carried = carried
                item.health.status = "partial"
                note = f"沿用上一輪 {carried} 筆（本輪讀取不完整）"
                prior = item.health.message
                item.health.message = f"{prior}；{note}" if prior else note
        item.health.activities = len(built)
        activities[bank_id] = built
        health.append(item.health)

    failed = sum(1 for h in health if h.status in ("failed", "blocked"))
    publishable = failed < max(3, len(health) // 2)
    if not publishable:
        messages.append(f"{failed}/{len(health)} 個來源失敗，本輪不發布")
    return RunResult(
        observed,
        activities,
        health,
        calls,
        sum(1 for v in failures.values() if "失敗" in v),
        publishable,
        messages,
        {"model": model, **meter.as_dict()},
    )


def _still_valid(activity: Activity, observed: datetime) -> bool:
    return (
        activity.period_end is None or activity.period_end >= (observed - timedelta(days=1)).date()
    )


def _carry_missing(
    built: list[Activity], old: list[Activity], fetched: Fetched
) -> tuple[list[Activity], int]:
    """局部讀取失敗時，把本輪漏掉、上一輪仍有效的活動標 stale 沿用。

    - 某分類讀不到：無從得知該分類原有哪些活動，所有本輪缺席的舊活動都沿用。
    - 只有個別明細頁失敗：只沿用 URL 落在失敗清單裡的舊活動；其餘缺席者視為
      官方已下架，照常移除。
    讀取完整的一輪不沿用任何東西，因此 stale 活動會在來源恢復後的第一輪消失。
    ``old`` 須已濾掉過期活動。
    """
    if not fetched.category_failed and not fetched.failed_urls:
        return built, 0
    present_ids = {a.id for a in built}
    present_hashes = {a.content_hash for a in built if a.content_hash}
    carried = [
        a.model_copy(update={"stale": True})
        for a in old
        if a.id not in present_ids
        and not (a.content_hash and a.content_hash in present_hashes)
        and (fetched.category_failed or a.url in fetched.failed_urls)
    ]
    return built + carried, len(carried)


def _carry_unextracted(built: list[Activity], bank_id: str) -> list[Activity]:
    """本輪 LLM 沒跑到的活動，若上一輪同內容已有結果就沿用。"""
    old = {a.content_hash: a for a in previous(bank_id)}
    out: list[Activity] = []
    for activity in built:
        prior = old.get(activity.content_hash)
        unextracted = any("尚未抽取" in r or "抽取失敗" in r for r in activity.review)
        if unextracted and prior is not None and prior.subs:
            activity = activity.model_copy(
                update={
                    "registration": prior.registration,
                    "registration_quote": prior.registration_quote,
                    "subs": prior.subs,
                    "review": [r for r in activity.review if "抽取" not in r] + prior.review,
                }
            )
        out.append(activity)
    return out


def save(result: RunResult) -> None:
    folder = STATE / "activities"
    folder.mkdir(parents=True, exist_ok=True)
    for bank_id, items in result.activities.items():
        rows = [a.model_dump(mode="json") for a in sorted(items, key=lambda a: a.id)]
        path = folder / f"{bank_id}.json"
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    run_info = {
        "observed_at": result.observed.isoformat(),
        "llm_calls": result.llm_calls,
        "llm_failures": result.llm_failures,
        "llm_usage": result.usage,
        "publishable": result.publishable,
        "messages": result.messages,
        "sources": [h.model_dump(mode="json") for h in result.health],
    }
    (STATE / "run.json").write_text(
        json.dumps(run_info, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


def log_usage(result: RunResult, scope: list[str] | None, *, path: Path | None = None) -> None:
    """把本輪 LLM 用量附加到 ``var/usage.jsonl``；不論本輪是否發布都記（token 已花掉）。"""
    target = path or USAGE_LOG
    target.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "observed_at": result.observed.isoformat(),
        "scope": ",".join(scope) if scope else "all",
        "publishable": result.publishable,
        **result.usage,
    }
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_usage(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or USAGE_LOG
    if not target.exists():
        return []
    return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line]
