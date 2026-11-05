#!/usr/bin/env python3
"""golden.yaml 結構與原文核對。

用法：
    .venv/bin/python golden/check.py [--golden golden/golden.yaml] [--src <golden_src 目錄>]

檢查項目：
  1. YAML 可解析、頂層為 list，每筆必要欄位齊全、型別正確。
  2. id 唯一，且以「<bank>-」開頭；bank 屬於 18 家清單。
  3. registration（頂層與子活動覆寫）屬於 required / not_required / unspecified_time。
  4. 每個時點為 "YYYY-MM-DDTHH:MM+08:00"（或 null），可解析；start <= end；
     slot 與檢核範圍（golden/window.json，台北時間整日）有交集（end=null 視為開放到期）；
     同一子活動內依時間排序。
  5. required 的筆至少有一個需登錄子活動帶時點；not_required / unspecified_time 的子活動不得有時點。
  6. 每個 quote 逐字出現在 <src>/<id>.txt（比對前把連續空白壓成一個空格；
     若仍找不到再試「去掉全部空白」，此情形列為 note 但不算失敗）。
只用標準庫 + PyYAML。結束碼：0 = 全過，1 = 有錯。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import yaml

BANKS = {
    "cathay", "chb", "ctbc", "dbs", "esun", "first", "hncb", "kgi", "mega", "obank",
    "scsb", "sinopac", "sunny", "taipei_fubon", "taishin", "tcbbank", "ubot", "yuanta",
}
REG = {"required", "not_required", "unspecified_time"}
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}\+08:00$")
TZ = dt.timezone(dt.timedelta(hours=8))
_HERE = Path(__file__).resolve().parent
_WINDOW = json.loads((_HERE / "window.json").read_text(encoding="utf-8"))
RANGE_START = dt.datetime.fromisoformat(_WINDOW["start"]).replace(tzinfo=TZ)
RANGE_END = dt.datetime.fromisoformat(_WINDOW["end"]).replace(hour=23, minute=59, tzinfo=TZ)
# 官方原文副本（每筆 <id>.txt）只留本機，不進版控
DEFAULT_SRC = _HERE.parent / "var" / "golden_src"
REQUIRED_KEYS = ("id", "bank", "url", "title", "registration", "period", "subs", "difficulty", "verified_at")


def parse_ts(value: object, where: str, errors: list[str]) -> dt.datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not TS_RE.match(value):
        errors.append(f"{where}: 時間格式須為 YYYY-MM-DDTHH:MM+08:00，得到 {value!r}")
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError as exc:
        errors.append(f"{where}: 無法解析 {value!r}（{exc}）")
        return None


def as_date(value: object) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def nospace(text: str) -> str:
    # 與管線相同的正規化（全形數字/冒號/波浪號折疊、民國年轉西元），只用於比對
    from radar.textnorm import normalize

    return re.sub(r"\s+", "", normalize(text))


def main() -> int:
    ap = argparse.ArgumentParser()
    here = Path(__file__).resolve().parent
    ap.add_argument("--golden", type=Path, default=here / "golden.yaml")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    args = ap.parse_args()

    errors: list[str] = []
    notes: list[str] = []
    try:
        data = yaml.safe_load(args.golden.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        print(f"FAIL YAML 解析失敗：{exc}")
        return 1
    if not isinstance(data, list):
        print("FAIL 頂層必須是 list")
        return 1

    seen: set[str] = set()
    stats = {"entries": 0, "slots": 0, "quotes": 0, "quotes_nospace": 0}
    by_reg: dict[str, int] = {}
    by_bank: dict[str, int] = {}
    src_cache: dict[str, tuple[str, str] | None] = {}

    for idx, entry in enumerate(data):
        where0 = f"#{idx}"
        if not isinstance(entry, dict):
            errors.append(f"{where0}: 每筆須為 mapping")
            continue
        eid = entry.get("id")
        where0 = f"{eid or where0}"
        missing = [k for k in REQUIRED_KEYS if k not in entry]
        if missing:
            errors.append(f"{where0}: 缺欄位 {missing}")
            continue
        stats["entries"] += 1
        if not isinstance(eid, str) or not eid:
            errors.append(f"{where0}: id 須為非空字串")
            continue
        if eid in seen:
            errors.append(f"{eid}: id 重複")
        seen.add(eid)
        bank = entry["bank"]
        if bank not in BANKS:
            errors.append(f"{eid}: bank {bank!r} 不在 18 家清單")
        elif not eid.startswith(f"{bank}-"):
            errors.append(f"{eid}: id 須以 '{bank}-' 開頭")
        by_bank[bank] = by_bank.get(bank, 0) + 1
        reg = entry["registration"]
        if reg not in REG:
            errors.append(f"{eid}: registration {reg!r} 不合法")
        by_reg[reg] = by_reg.get(reg, 0) + 1
        if not str(entry["url"]).startswith("http"):
            errors.append(f"{eid}: url 不合法")
        period = entry["period"]
        if period is not None:
            if not (isinstance(period, list) and len(period) == 2):
                errors.append(f"{eid}: period 須為 [start, end] 或 null")
            else:
                p0, p1 = as_date(period[0]), as_date(period[1])
                if p0 is None or p1 is None or p0 > p1:
                    errors.append(f"{eid}: period 日期不合法 {period}")
        if as_date(entry["verified_at"]) is None:
            errors.append(f"{eid}: verified_at 不是日期")
        if not isinstance(entry["difficulty"], list) or not entry["difficulty"]:
            errors.append(f"{eid}: difficulty 須為非空 list")

        if eid not in src_cache:
            path = args.src / f"{eid}.txt"
            if path.is_file():
                raw = path.read_text(encoding="utf-8", errors="replace")
                src_cache[eid] = (squash(raw), nospace(raw))
            else:
                src_cache[eid] = None
                errors.append(f"{eid}: 找不到原文檔 {path}")
        src = src_cache[eid]

        subs = entry["subs"]
        if not isinstance(subs, list) or not subs:
            errors.append(f"{eid}: subs 須為非空 list")
            continue
        required_slots = 0
        sub_names: set[str] = set()
        for sidx, sub in enumerate(subs):
            sw = f"{eid}.subs[{sidx}]"
            if not isinstance(sub, dict) or "slots" not in sub or "quotes" not in sub or "name" not in sub:
                errors.append(f"{sw}: 子活動須含 name / slots / quotes")
                continue
            name = sub["name"]
            if name in sub_names:
                errors.append(f"{sw}: 子活動名稱重複 {name!r}")
            sub_names.add(name)
            sreg = sub.get("registration", reg)
            if sreg not in REG:
                errors.append(f"{sw}: registration {sreg!r} 不合法")
            slots = sub["slots"] or []
            if sreg != "required" and slots:
                errors.append(f"{sw}: registration={sreg} 不應有 slots")
            prev_key: dt.datetime | None = None
            for tidx, slot in enumerate(slots):
                tw = f"{sw}.slots[{tidx}]"
                if not isinstance(slot, dict) or "start" not in slot or "end" not in slot:
                    errors.append(f"{tw}: slot 須含 start 與 end")
                    continue
                start = parse_ts(slot["start"], f"{tw}.start", errors)
                end = parse_ts(slot["end"], f"{tw}.end", errors)
                if slot["start"] is None and slot["end"] is None:
                    errors.append(f"{tw}: start 與 end 不可同時為 null")
                    continue
                if start and end and start > end:
                    errors.append(f"{tw}: start 晚於 end")
                # end=null 表示官方未寫截止（額滿為止），視為開放到期，只要求 start 不晚於範圍終點
                lo = start or end
                hi = end if end is not None else dt.datetime.max.replace(tzinfo=TZ)
                if lo and (hi < RANGE_START or lo > RANGE_END):
                    errors.append(f"{tw}: 與檢核範圍 {_WINDOW['start']}~{_WINDOW['end']} 無交集")
                key = start or end
                if key and prev_key and key < prev_key:
                    errors.append(f"{tw}: 未依時間排序")
                prev_key = key or prev_key
                stats["slots"] += 1
                if sreg == "required":
                    required_slots += 1
            quotes = sub["quotes"]
            if not isinstance(quotes, list) or not quotes:
                errors.append(f"{sw}: quotes 須為非空 list")
                continue
            for qidx, quote in enumerate(quotes):
                qw = f"{sw}.quotes[{qidx}]"
                if not isinstance(quote, str) or not quote.strip():
                    errors.append(f"{qw}: quote 須為非空字串")
                    continue
                stats["quotes"] += 1
                if src is None:
                    continue
                if squash(quote) in src[0]:
                    continue
                if nospace(quote) in src[1]:
                    stats["quotes_nospace"] += 1
                    notes.append(f"{qw}: 去除全部空白後才吻合")
                    continue
                errors.append(f"{qw}: 原文中找不到 {quote[:40]!r}")
        if reg == "required" and required_slots == 0:
            errors.append(f"{eid}: registration=required 但沒有任何時點")

    print(f"entries={stats['entries']} slots={stats['slots']} quotes={stats['quotes']} "
          f"(其中 {stats['quotes_nospace']} 筆需去空白才吻合)")
    print("registration:", ", ".join(f"{k}={v}" for k, v in sorted(by_reg.items())))
    print("banks:", ", ".join(f"{k}={v}" for k, v in sorted(by_bank.items())),
          f"(共 {len(by_bank)} 家)")
    missing_banks = sorted(BANKS - set(by_bank))
    if missing_banks:
        print("WARN 未涵蓋銀行:", ", ".join(missing_banks))
    for n in notes:
        print("NOTE", n)
    for e in errors:
        print("FAIL", e)
    print("RESULT:", "PASS" if not errors else f"FAIL ({len(errors)} errors)")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
