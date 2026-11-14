"""以 golden 集（官方原文人工核對的登錄時點）評分。

比對單位是「時點」：開放時刻到分鐘、截止時刻到分鐘（兩邊都沒有截止也算相符）。
精確率＝產出的時點中正確的比例 —— 這是發布門檻（≥98%），因為時間軸上
一個錯的時點會讓使用者白跑一趟；召回率衡量漏了多少。

``score()`` 評目前 state；``evaluate(model)`` 只對 golden 涵蓋的頁面重跑抽取
（用 var/docs 的原文，不寫 state），用來比較不同模型。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .extract.locate import LocateError, locate
from .models import Activity
from .pipeline import DOCS, REGISTRATION_WORDS, STATE, TAIPEI, assemble, group_docs, load_docs
from .pipeline import previous as previous_state
from .textnorm import normalize

GOLDEN = Path(__file__).resolve().parents[2] / "golden" / "golden.yaml"
# 檢核窗口（golden 涵蓋的兩個月）；每批換新只改 golden/window.json
_WINDOW = json.loads((GOLDEN.parent / "window.json").read_text(encoding="utf-8"))
WINDOW = (date.fromisoformat(_WINDOW["start"]), date.fromisoformat(_WINDOW["end"]))
Key = tuple[str | None, str | None]


def _minute(value: Any) -> str | None:
    if value in (None, ""):
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return moment.strftime("%Y-%m-%dT%H:%M")


def _end_minute(value: Any) -> str | None:
    """截止時刻的「24:00」與「隔天 00:00」都等同當天 23:59。"""
    if value in (None, ""):
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.hour == 0 and moment.minute == 0:
        moment = moment - timedelta(minutes=1)
    return moment.strftime("%Y-%m-%dT%H:%M")


def _in_window(key: Key) -> bool:
    anchor = key[0] or key[1]
    return anchor is not None and WINDOW[0].isoformat() <= anchor[:10] <= WINDOW[1].isoformat()


def _entries() -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    return loaded


def _base(url: str) -> str:
    """比對用的網址：去掉片段；華南入口網址只看 urile 參數（前面的 portal 狀態每次不同）。"""
    url, _, fragment = url.partition("#")
    url = url.rstrip("/")
    # 中信 LINE Pay 一頁數十個活動，各以 #lightbox_<名稱> 區分，片段是活動身分
    tail = f"#{fragment}" if fragment.startswith("lightbox_") else ""
    found = re.search(r"urile=([^&]+)", url)
    if found:
        return found.group(1) + tail
    # 同一頁常有兩個官方網域（國泰 cathaybk.com.tw／cathay-cube.com.tw）與有無 .html 兩種寫法；
    # 比對只在同一家銀行內進行，以路徑（含 query）為準
    parts = urlsplit(url)
    path = re.sub(r"\.html?$", "", parts.path)
    return (f"{path}?{parts.query}" if parts.query else path) + tail


def _candidates(entry: dict[str, Any], pool: list[Activity]) -> list[Activity]:
    url = _base(str(entry["url"]))
    hits = [a for a in pool if _base(a.url) == url]
    if hits:
        return hits
    title = str(entry.get("title", ""))
    return [a for a in pool if title and (a.title == title or title in a.aliases)]


def _produced(activity: Activity) -> set[Key]:
    keys = {
        (_minute(slot.start), _end_minute(slot.end)) for sub in activity.subs for slot in sub.slots
    }
    return {k for k in keys if _in_window(k)}


def _compare(
    entries: list[dict[str, Any]], find: Callable[[dict[str, Any]], list[Activity]]
) -> dict[str, Any]:
    tp = fp = fn = 0
    missing: list[str] = []
    diffs: list[dict[str, Any]] = []
    for entry in entries:
        expected: set[Key] = {
            (_minute(slot.get("start")), _end_minute(slot.get("end")))
            for sub in entry.get("subs") or []
            for slot in sub.get("slots") or []
        }
        expected = {k for k in expected if _in_window(k)}
        # golden 以「官方頁面」為單位；同一頁拆成多筆活動時合併比對
        # （中信 LINE Pay 以 lightbox 片段區分，不合併）
        candidates = find(entry)
        if not candidates:
            if expected:
                missing.append(str(entry["id"]))
                fn += len(expected)
            continue
        produced = set().union(*(_produced(a) for a in candidates))
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


SOURCES = GOLDEN.parents[1] / "var" / "golden_src"
_WS = re.compile(r"\s+")


def _squash(text: str) -> str:
    return _WS.sub("", normalize(text))


def suspect_entries(
    entries: list[dict[str, Any]],
    find: Callable[[dict[str, Any]], list[Activity]],
    sources: Path = SOURCES,
) -> list[dict[str, Any]]:
    """疑似 golden 漏看原文的條目（自我修復用）。

    判準：該筆有「多出的時點」，而每個多出時點的抽取引句都逐字出現在該筆 golden 的
    官方原文副本裡——抽取有原文佐證、golden 卻沒記，最常見的原因是 golden 漏看
    表格或注意事項（11-14 dbs 兩筆即此類）。沒有原文副本的條目不判定。
    """
    out: list[dict[str, Any]] = []
    for entry in entries:
        src = sources / f"{entry['id']}.txt"
        if not src.exists():
            continue
        expected = {
            (_minute(slot.get("start")), _end_minute(slot.get("end")))
            for sub in entry.get("subs") or []
            for slot in sub.get("slots") or []
        }
        quotes: dict[Key, list[str]] = {}
        for activity in find(entry):
            for sub in activity.subs:
                for slot in sub.slots:
                    key = (_minute(slot.start), _end_minute(slot.end))
                    if _in_window(key):
                        quotes.setdefault(key, []).append(slot.evidence.quote)
        extra = {k: q for k, q in quotes.items() if k not in expected}
        if not extra:
            continue
        text = _squash(src.read_text(encoding="utf-8"))
        if all(any(_squash(q) in text for q in qs) for qs in extra.values()):
            out.append({
                "id": entry["id"],
                "extra": sorted(extra, key=str),
                "quotes": sorted({q for qs in extra.values() for q in qs})[:3],
            })
    return out


def score() -> dict[str, Any]:
    """評分；另列 ``suspects``（疑似 golden 漏看）與排除它們後的 ``trusted`` 分數。"""
    entries = _entries()
    pools = {
        str(e["bank"]): previous_state(str(e["bank"]))
        for e in entries
        if (STATE / "activities" / f"{e['bank']}.json").exists()
    }

    def find(entry: dict[str, Any]) -> list[Activity]:
        return _candidates(entry, pools.get(str(entry["bank"]), []))

    report = _compare(entries, find)
    suspects = suspect_entries(entries, find)
    ids = {s["id"] for s in suspects}
    trusted = _compare([e for e in entries if e["id"] not in ids], find) if ids else report
    report["suspects"] = suspects
    report["trusted"] = {k: trusted[k] for k in ("precision", "recall", "entries")}
    return report


def evaluate(model: str, workers: int = 3) -> dict[str, Any]:
    """只對 golden 頁面以指定模型抽取並評分。沒有原文快取的銀行列為 unavailable。"""
    observed = datetime.now(TAIPEI).replace(microsecond=0)
    entries = _entries()
    available = {str(e["bank"]) for e in entries if (DOCS / f"{e['bank']}.json").exists()}
    urls = {_base(str(e["url"])) for e in entries}
    titles = {str(e.get("title", "")) for e in entries}
    targets = []
    for bank in sorted(available):
        for doc in group_docs(load_docs(bank), observed.date()):
            if any(_base(s.url) in urls or s.title in titles for s in doc.stubs):
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
    report = _compare(scored, lambda e: _candidates(e, by_bank.get(str(e["bank"]), [])))
    report["model"] = model
    report["unavailable_banks"] = sorted({str(e["bank"]) for e in entries} - available)
    report["llm_docs"] = sum(1 for d in targets if REGISTRATION_WORDS.search(d.text))
    return report
