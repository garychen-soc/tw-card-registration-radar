"""發布前閘門：golden 評分＋與上一版比較，回傳「擋下的原因」與「要通知的警告」。

只看 golden 精確率擋不住「漏資料」（活動被刪時 fp 不會增加），也擋不住「golden 樣本
過期、可評分時點趨近 0 時精確率恆為 100%」（獨立審查 🟡-1）。因此：
- 精確率 < block_precision：擋下不發布（沿用既有門檻）。
- 精確率 < warn_precision、召回 < warn_recall、golden 頁找不到、可評分時點過少：通知。
- 上一版仍在期間內的活動，這一版憑空消失（不含自然到期）達門檻：通知——擷取器可能
  壞了卻沒報錯；局部讀取失敗已由 pipeline 沿用處理，不會算進來。
警告不擋發布：官方頁改版也會造成這些現象，擋下會讓網站停更，交由人判斷。

自我修復（Gary 11-14 核准）：精確率低於擋下門檻時，若錯誤全來自「抽取引句逐字出現在
golden 原文副本」的條目（疑似 golden 漏看），改用排除這些條目後的分數判定並通知複核。
疑似條目過多（> max_suspects 或 > suspect_ratio）視為系統性問題，照樣擋下。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any


@dataclass
class Thresholds:
    block_precision: float = 0.90
    warn_precision: float = 0.98
    warn_recall: float = 0.95
    min_sample: int = 30
    vanish_min: int = 10
    vanish_ratio: float = 0.08
    max_suspects: int = 5
    suspect_ratio: float = 0.10


@dataclass
class Verdict:
    block: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"block": self.block, "warnings": self.warnings}


def evaluate(
    score: dict[str, Any],
    vanished: dict[str, tuple[int, int]] | None = None,
    limits: Thresholds | None = None,
) -> Verdict:
    """``vanished``：銀行名 →（上一版仍有效的筆數, 其中這一版消失的筆數）。"""
    t = limits or Thresholds()
    verdict = Verdict()
    precision = float(score.get("precision", 0.0))
    recall = float(score.get("recall", 0.0))
    sample = sum(int(score.get(k, 0)) for k in ("matched_slots", "wrong_slots", "missed_slots"))
    not_found = score.get("not_found") or []

    suspects = score.get("suspects") or []
    trusted = score.get("trusted") or {}
    if precision < t.block_precision and suspects and trusted:
        entries = int(trusted.get("entries", 0)) + len(suspects)
        if len(suspects) <= t.max_suspects and len(suspects) <= max(1, entries * t.suspect_ratio):
            ids = "、".join(str(s["id"]) for s in suspects)
            verdict.warnings.append(
                f"自我修復：golden 精確率 {precision:.3f}，{len(suspects)} 筆疑似 golden 漏看原文"
                f"（{ids}），本輪以其餘條目判定，請複核 golden"
            )
            precision = float(trusted.get("precision", precision))
            recall = float(trusted.get("recall", recall))
    elif suspects:
        ids = "、".join(str(s["id"]) for s in suspects)
        verdict.warnings.append(f"{len(suspects)} 筆 golden 疑似漏看原文（{ids}），請複核")
    if precision < t.block_precision:
        verdict.block = f"golden 精確率 {precision:.3f} 低於 {t.block_precision}，本輪不發布"
    elif precision < t.warn_precision:
        verdict.warnings.append(f"golden 精確率 {precision:.3f}（官方頁可能換了內容）")
    if recall < t.warn_recall:
        verdict.warnings.append(f"golden 召回 {recall:.3f}（可能有活動或時點漏抓）")
    if not_found:
        verdict.warnings.append(f"golden 有 {len(not_found)} 頁在網站資料中找不到")
    if sample < t.min_sample:
        verdict.warnings.append(f"golden 可評分時點只剩 {sample} 個，閘門快失效，請換新 golden")

    drops = [
        f"{bank} 少 {gone}/{valid} 筆"
        for bank, (valid, gone) in sorted((vanished or {}).items())
        if gone >= t.vanish_min or (valid and gone >= max(3, valid * t.vanish_ratio))
    ]
    if drops:
        verdict.warnings.append("仍在期間內的活動消失：" + "、".join(drops))
    return verdict


def vanished_since(ref: str, state: Path, today: date) -> dict[str, tuple[int, int]]:
    """比對 git ``ref`` 版本與目前 state：上一版仍有效（未到期）的活動有幾筆不見了。"""
    out: dict[str, tuple[int, int]] = {}
    for path in sorted((state / "activities").glob("*.json")):
        rel = path.relative_to(state.parent).as_posix()
        shown = subprocess.run(
            ["git", "show", f"{ref}:{rel}"], cwd=state.parent, capture_output=True, text=True
        )
        if shown.returncode != 0:
            continue
        before = [
            row for row in json.loads(shown.stdout)
            if not row.get("period_end") or date.fromisoformat(row["period_end"]) >= today
        ]
        now = {row["id"] for row in json.loads(path.read_text(encoding="utf-8"))}
        gone = sum(1 for row in before if row["id"] not in now)
        if before:
            name = before[0].get("bank_name") or path.stem
            out[name] = (len(before), gone)
    return out
