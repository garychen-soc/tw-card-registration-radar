"""以 golden 集（官方原文人工核對的登錄時點）評分。

比對單位是「時點」：開放時刻到分鐘、截止時刻到分鐘（兩邊都沒有截止也算相符）。
精確率＝產出的時點中正確的比例 —— 這是發布門檻（≥98%），因為時間軸上
一個錯的時點會讓使用者白跑一趟；召回率衡量漏了多少。

``score()`` 評目前 state；``evaluate(model)`` 只對 golden 涵蓋的頁面重跑抽取
（用 var/docs 的原文，不寫 state），用來比較不同模型。
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from .extract.locate import LocateError, locate
from .models import Activity
from .pipeline import DOCS, REGISTRATION_WORDS, STATE, TAIPEI, assemble, group_docs, load_docs
from .pipeline import previous as previous_state

GOLDEN = Path(__file__).resolve().parents[2] / "golden" / "golden.yaml"
WINDOW = (date(2026, 11, 1), date(2026, 12, 31))
Key = tuple[str | None, str | None]


def _minute(value: Any) -> str | None:
    if value in (None, ""):
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return moment.strftime("%Y-%m-%dT%H:%M")


def _in_window(key: Key) -> bool:
    anchor = key[0] or key[1]
    return anchor is not None and WINDOW[0].isoformat() <= anchor[:10] <= WINDOW[1].isoformat()


def _entries() -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    return loaded


def _match(entry: dict[str, Any], pool: list[Activity]) -> Activity | None:
    url = str(entry["url"]).rstrip("/")
    for activity in pool:
        if activity.url.rstrip("/") == url:
            return activity
    title = str(entry.get("title", ""))
    for activity in pool:
        if title and (activity.title == title or title in activity.aliases):
            return activity
    return None


def _compare(
    entries: list[dict[str, Any]], find: Callable[[dict[str, Any]], Activity | None]
) -> dict[str, Any]:
    tp = fp = fn = 0
    missing: list[str] = []
    diffs: list[dict[str, Any]] = []
    for entry in entries:
        expected: set[Key] = {
            (_minute(slot.get("start")), _minute(slot.get("end")))
            for sub in entry.get("subs") or []
            for slot in sub.get("slots") or []
        }
        expected = {k for k in expected if _in_window(k)}
        activity = find(entry)
        if activity is None:
            if expected:
                missing.append(str(entry["id"]))
                fn += len(expected)
            continue
        produced: set[Key] = {
            (_minute(slot.start), _minute(slot.end)) for sub in activity.subs for slot in sub.slots
        }
        produced = {k for k in produced if _in_window(k)}
        tp += len(expected & produced)
        fp += len(produced - expected)
        fn += len(expected - produced)
        if produced != expected:
            diffs.append(
                {
                    "id": entry["id"],
                    "wrong": sorted(produced - expected, key=str),
                    "missed": sorted(expected - produced, key=str),
                }
            )
    return {
        "entries": len(entries),
        "matched_slots": tp,
        "wrong_slots": fp,
        "missed_slots": fn,
        "precision": round(tp / (tp + fp), 4) if tp + fp else 1.0,
        "recall": round(tp / (tp + fn), 4) if tp + fn else 1.0,
        "not_found": missing,
        "diffs": diffs,
    }


def score() -> dict[str, Any]:
    entries = _entries()
    pools = {
        str(e["bank"]): previous_state(str(e["bank"]))
        for e in entries
        if (STATE / "activities" / f"{e['bank']}.json").exists()
    }
    return _compare(entries, lambda e: _match(e, pools.get(str(e["bank"]), [])))


def evaluate(model: str, workers: int = 3) -> dict[str, Any]:
    """只對 golden 頁面以指定模型抽取並評分。沒有原文快取的銀行列為 unavailable。"""
    observed = datetime.now(TAIPEI).replace(microsecond=0)
    entries = _entries()
    available = {str(e["bank"]) for e in entries if (DOCS / f"{e['bank']}.json").exists()}
    urls = {str(e["url"]).rstrip("/") for e in entries}
    titles = {str(e.get("title", "")) for e in entries}
    targets = []
    for bank in sorted(available):
        for doc in group_docs(load_docs(bank), observed.date()):
            if any(s.url.rstrip("/") in urls or s.title in titles for s in doc.stubs):
                targets.append(doc)

    def extract(doc: Any) -> Activity:
        if not REGISTRATION_WORDS.search(doc.text):
            return assemble(doc, None, "", observed)
        try:
            return assemble(doc, locate(doc.request(), model), "", observed)
        except LocateError as exc:
            return assemble(doc, None, f"抽取失敗：{exc}", observed)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        built = list(pool.map(extract, targets))
    by_bank: dict[str, list[Activity]] = {}
    for activity in built:
        by_bank.setdefault(activity.bank_id, []).append(activity)
    scored = [e for e in entries if str(e["bank"]) in available]
    report = _compare(scored, lambda e: _match(e, by_bank.get(str(e["bank"]), [])))
    report["model"] = model
    report["unavailable_banks"] = sorted({str(e["bank"]) for e in entries} - available)
    report["llm_docs"] = sum(1 for d in targets if REGISTRATION_WORDS.search(d.text))
    return report
