"""LLM 定位：從條款全文找出登錄規則，回傳逐字引句與結構化數值。

分工原則 —— LLM 只做它擅長、正則做不好的事：

* 判斷哪一句是登錄規則、哪一句是舉例、哪條規則屬於哪個子活動；
* 讀懂「中午12:00」「遇假日順延」「開放至月底」「無需每月登錄」這類語意。

數值換算成具體日期、套用辦公日曆、檢查是否落在活動期間，全部由
``radar.extract.schedule`` 以確定性程式完成；``radar.extract.verify`` 再確認
每個引句逐字出現在原文、每個數字都能在引句裡找到。LLM 的輸出因此只是
「候選」，過不了程式檢查就不會上時間軸。

呼叫走本機 Claude Code CLI（``claude -p``，使用者的訂閱，不需 API key），
以 ``--json-schema`` 強制結構化輸出。結果以「提示版本＋模型＋條款文字」
的雜湊快取在 ``state/extractions/``，條款沒變就不重算。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROMPT_VERSION = "2026-11-01.1"
ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = ROOT / "state" / "extractions"
DEFAULT_MODEL = os.environ.get("RADAR_LLM_MODEL", "opus")
TIMEOUT_SECONDS = 420
MAX_TERMS_CHARS = 30_000

_POINT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["year", "month", "day", "hour", "minute"],
    "properties": {
        k: {"type": ["integer", "null"]} for k in ("year", "month", "day", "hour", "minute")
    },
}
_TIME = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": ["hour", "minute"],
    "properties": {"hour": {"type": "integer"}, "minute": {"type": "integer"}},
}
_CLAIM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["value", "quote"],
    "properties": {"value": {"type": "string"}, "quote": {"type": "string"}},
}
_RULE = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "quote",
        "kind",
        "is_example",
        "holiday",
        "start",
        "end",
        "days_of_month",
        "nth",
        "weekday",
        "dates",
        "open_time",
        "close_time",
        "end_of_month",
    ],
    "properties": {
        "quote": {"type": "string"},
        "kind": {
            "enum": [
                "range",
                "opens_at",
                "deadline",
                "monthly",
                "monthly_nth_weekday",
                "enumerated",
                "other",
            ]
        },
        "is_example": {"type": "boolean"},
        "holiday": {"enum": ["next_business_day", "previous_business_day", "none"]},
        "start": {"anyOf": [_POINT, {"type": "null"}]},
        "end": {"anyOf": [_POINT, {"type": "null"}]},
        "days_of_month": {"type": "array", "items": {"type": "integer"}},
        "nth": {"type": ["integer", "null"]},
        "weekday": {"type": ["integer", "null"]},
        "dates": {"type": "array", "items": _POINT},
        "open_time": _TIME,
        "close_time": _TIME,
        "end_of_month": {"type": "boolean"},
    },
}
SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["registration", "subs"],
    "properties": {
        "registration": _CLAIM,
        "subs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "rules", "reregister", "order", "quota"],
                "properties": {
                    "name": {"type": "string"},
                    "rules": {"type": "array", "items": _RULE},
                    "reregister": _CLAIM,
                    "order": _CLAIM,
                    "quota": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["seats", "quote"],
                        "properties": {
                            "seats": {"type": ["integer", "null"]},
                            "quote": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}

INSTRUCTIONS = """你是台灣信用卡活動條款的資料標註員。下面是某銀行官方活動頁的條款全文。請找出「持卡人何時可以／必須完成活動登錄」的所有規則，依 JSON schema 回答。

規則：
1. 每個 quote 必須逐字複製自條款原文的一個連續片段，不可改字、拼接或省略中間文字；長度以足以支撐你填的數值為準（通常 10–80 字）。
2. 只標「登錄」（含報名、登記、領券等持卡人須主動操作才能參加的動作）的時點。活動期間、消費期間、回饋入帳時間都不是登錄時點，除非條文明說「活動暨登錄期間」「登錄期間同活動期間」之類。
3. is_example=true 只用在說明情境的舉例句（例如「舉例：1月1日早上9點已登錄成功，後於1月11日再次登錄，系統將回覆…」）。「登錄日期如：10月01日、11月02日」這種列出實際開放日的寫法是真實日期：is_example=false、kind=enumerated。
4. 頁面有多個子活動（活動一／活動二、不同通路或卡別各自的登錄規則）時，每個子活動一個 subs 項目，name 逐字取自該子活動在頁面上的標題；規則只放進它所屬的子活動，不可把 A 子活動的時點放到 B。整頁只有一個活動時 subs 只有一項且 name=""。
5. 數值只填原文明寫的數字：沒寫年份 year=null；「中午12:00」hour=12；「下午3點」hour=15；「24:00」填 hour=23、minute=59；沒寫時刻 hour/minute=null。
6. kind：range（明寫起訖）、opens_at（明寫開始未寫截止）、deadline（只寫截止）、monthly（每月第幾日固定時刻開放，days_of_month 可多個，時刻放 open_time）、monthly_nth_weekday（每月第 nth 個週 weekday，weekday 1=週一…7=週日）、enumerated（列舉多個具體日期，放 dates）、other（以上皆非，如「消費後 7 日內登錄」）。
7. holiday：條文寫「遇假日順延」填 next_business_day；「提前至前一營業日」填 previous_business_day；沒寫填 none。
8. close_time：同一天內的截止時刻（如「每月1日08:00─24:00」close_time=23:59）。end_of_month：「開放登錄至月底／當月底」填 true。
9. reregister.value：each_period（每月／每期須重新登錄）、once（登錄一次即可／無需每月登錄）、unclear。order.value：register_before_spend（須先登錄再消費、登錄前消費不計）、retroactive_ok（活動期間內完成登錄即可、可事後補登）、unclear。unclear 時 quote=""。
10. quota.seats：登錄名額（如「每月限額3,000名」填 3000）；沒寫 null、quote=""。
11. registration.value：整頁活動是否需要登錄才能參加：required、not_required、unclear，附 quote。
12. 不需登錄時 subs 可為空陣列。不知道就留空或 unclear，絕不推測。"""


class LocateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Request:
    title: str
    period: str
    text: str

    def prompt(self) -> str:
        return (
            f"{INSTRUCTIONS}\n\n活動標題：{self.title}\n活動期間（列表公告）：{self.period or '未知'}\n"
            f"條款全文：\n<<<\n{self.text[:MAX_TERMS_CHARS]}\n>>>"
        )


def cache_key(request: Request, model: str) -> str:
    material = "\x1f".join([PROMPT_VERSION, model, request.title, request.period, request.text])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def cached(request: Request, model: str = DEFAULT_MODEL) -> dict[str, Any] | None:
    path = CACHE_DIR / f"{cache_key(request, model)}.json"
    if not path.exists():
        return None
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload["output"]


def _claude_bin() -> str:
    found = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    if fallback.exists():
        return str(fallback)
    raise LocateError("找不到 claude CLI（設定 CLAUDE_BIN 或把它加進 PATH）")


def locate(request: Request, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """回傳符合 ``SCHEMA`` 的 dict。有快取直接回快取。"""
    hit = cached(request, model)
    if hit is not None:
        return hit
    command = [
        _claude_bin(),
        "-p",
        "--model",
        model,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(SCHEMA, ensure_ascii=False),
        "--tools",
        "",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--no-session-persistence",
    ]
    with tempfile.TemporaryDirectory(prefix="radar-llm-") as workdir:
        try:
            completed = subprocess.run(
                command,
                input=request.prompt(),
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
                cwd=workdir,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LocateError(f"claude -p 逾時（{TIMEOUT_SECONDS}s）") from exc
    if completed.returncode != 0:
        raise LocateError(f"claude -p 失敗（{completed.returncode}）：{completed.stderr[-400:]}")
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise LocateError(f"claude -p 輸出不是 JSON：{completed.stdout[:200]}") from exc
    output = envelope.get("structured_output")
    if envelope.get("is_error") or not isinstance(output, dict):
        raise LocateError(f"claude -p 沒有結構化輸出：{str(envelope.get('result'))[:200]}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "key": cache_key(request, model),
        "prompt_version": PROMPT_VERSION,
        "model": model,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "title": request.title,
        "duration_ms": envelope.get("duration_ms"),
        "cost_usd": envelope.get("total_cost_usd"),
        "output": output,
    }
    path = CACHE_DIR / f"{record['key']}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return output
