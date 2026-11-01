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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import banks
from .extract.locate import DEFAULT_MODEL, LocateError, Request, cached, locate
from .extract.schedule import Context, build
from .models import Activity, SourceHealth, Stub
from .sources import open_adapter
from .textnorm import normalize
from .transport import AccessDenied, TransportError

log = logging.getLogger(__name__)
TAIPEI = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "state"
DROP_LIMIT = 0.4
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


@dataclass
class RunResult:
    observed: datetime
    activities: dict[str, list[Activity]]
    health: list[SourceHealth]
    llm_calls: int
    llm_failures: int
    publishable: bool
    messages: list[str]


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
                    out.docs.append((stub, adapter.terms(stub).text))
                except TransportError as exc:
                    failures += 1
                    log.warning("%s 明細失敗 %s：%s", bank_id, stub.url, exc)
            health.detail_failures = failures
            partial = [s for s in adapter.skipped if s.startswith("分類失敗")]
            if partial or (stubs and failures > len(stubs) * 0.2):
                health.status = "partial"
                health.message = "；".join(partial[:3]) or f"{failures} 筆明細讀取失敗"
            if not stubs:
                health.status = "failed"
                health.message = "列表為空"
    except AccessDenied as exc:
        health.status, health.message = "blocked", str(exc)[:200]
    except Exception as exc:
        health.status, health.message = "failed", f"{type(exc).__name__}: {exc}"[:200]
    return out


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


def group_docs(fetched: Fetched) -> list[Doc]:
    """同一家銀行、條款文字相同的多張卡片合併成一筆活動，其餘標題留作別名。"""
    groups: dict[str, Doc] = {}
    for stub, text in fetched.docs:
        digest = content_hash(text) if len(text) >= 200 else content_hash(stub.key)
        doc = groups.get(digest)
        if doc is None:
            groups[digest] = Doc(fetched.bank_id, [stub], text, digest)
        else:
            doc.stubs.append(stub)
    for doc in groups.values():
        doc.stubs.sort(key=lambda s: (len(s.title), s.key))
    return list(groups.values())


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
        title=stub.title,
        aliases=sorted({s.title for s in doc.stubs[1:]} - {stub.title}),
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
    elif failure:
        activity.registration = "unknown"
        review.append(failure)
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
) -> RunResult:
    observed = observed or datetime.now(TAIPEI).replace(microsecond=0)
    bank_ids = bank_ids or banks.available()
    messages: list[str] = []

    fetched: dict[str, Fetched] = {}
    http_banks = [b for b in bank_ids if banks.load(b).transport == "http"]
    browser_banks = [b for b in bank_ids if b not in http_banks]
    with ThreadPoolExecutor(max_workers=fetch_workers) as pool:
        for result in pool.map(fetch_bank, http_banks):
            fetched[result.bank_id] = result
    for bank_id in browser_banks:
        fetched[bank_id] = fetch_bank(bank_id)

    docs = [doc for bank_id in bank_ids for doc in group_docs(fetched[bank_id])]
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
    with ThreadPoolExecutor(max_workers=llm_workers) as pool:
        futures = {pool.submit(locate, doc.request(), model): doc for doc in pending}
        for future in as_completed(futures):
            doc = futures[future]
            calls += 1
            try:
                outputs[doc.digest] = future.result()
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
    )


def _still_valid(activity: Activity, observed: datetime) -> bool:
    return (
        activity.period_end is None or activity.period_end >= (observed - timedelta(days=1)).date()
    )


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
        "publishable": result.publishable,
        "messages": result.messages,
        "sources": [h.model_dump(mode="json") for h in result.health],
    }
    (STATE / "run.json").write_text(
        json.dumps(run_info, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
