"""登錄時點解析：tokenizer + 狀態機。

為什麼不用正則疊加：舊實作在 `_registration_windows` 裡疊了六層獨立的正則
（range / same_day_range / point / deadline / month / recurring），各自帶一套
上下文關鍵字判斷，再用 `seen` 與 `range_spans` 手工去重。疊到第六層之後
沒人能安全修改，而且相鄰 pattern 的優先權是隱含的 —— 秒級時間讓第一層失配後
默默掉到第三層，產出「起點 + 固定分鐘數」的假視窗，沒有任何訊號指出降級發生了。

這裡改成兩階段：先把文字掃成 token 串，再用單一狀態機決定每個 token 叢集
代表哪一種視窗。優先權變成顯式的，降級路徑也變成可觀測的（confidence 下降）。

三條核心規則：

* 解析不到結束時間就留 ``None``。絕不用固定分鐘數填補。
* 「只知道截止時間」是合法結果（``kind="deadline"``、``start=None``）。
* 每個視窗都帶 confidence 與 evidence，讓 UI 能區分「我確定」與「我猜的」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from ..models import Evidence, Recurrence, RegistrationWindow, WindowKind
from .normalize import normalize

TAIPEI = ZoneInfo("Asia/Taipei")

TokenKind = Literal["DATE", "TIME", "SEP", "UNTIL", "FROM"]

# 叢集內 token 之間容許的最大間隔（字元）。超過就視為兩個獨立叢集。
MAX_TOKEN_GAP = 8
# 判斷「這個時間是不是登錄時間」的上下文視窗
CONTEXT_BEFORE = 90
CONTEXT_AFTER = 70

_REGISTRATION_HINTS = ("登錄", "登記")
_NEGATIVE_HINTS = ("無需登錄", "免登錄", "不需登錄", "毋需登錄", "無須登錄")
# 這些是「可消費期間」的標記，不是登錄期間。若它比任何登錄標記更靠近該時間叢集，
# 就不該把它當成登錄視窗 —— 舊實作沒有這層區分。
_SPEND_PERIOD_HINTS = ("活動期間", "活動日期", "優惠期間", "消費期間", "活動時間")
_DEADLINE_HINTS = ("登錄期限", "期限", "截止", "最晚")

_SPECIFIC_MARKERS = (
    "登錄期間",
    "開放登錄",
    "開始登錄",
    "完成活動登錄",
    "完成登錄",
    "登錄時間",
    "登錄辦法",
    "活動登錄",
    "登錄日期",
    "波登錄",
    "檔登錄",
    "登錄期限",
)

# token 型態組合，長者優先。優先權在這裡是**顯式**的 —— 舊實作靠六個正則的
# 宣告順序隱含決定，秒級時間讓第一層失配時會無聲降級到第三層。
_PATTERNS: tuple[tuple[TokenKind, ...], ...] = (
    ("DATE", "TIME", "SEP", "DATE", "TIME"),
    ("DATE", "TIME", "SEP", "DATE"),
    ("DATE", "TIME", "SEP", "TIME"),
    ("DATE", "SEP", "DATE"),
    ("DATE", "TIME"),
    ("DATE",),
)

_TOKEN = re.compile(
    r"(?P<date_full>(?<!\d)(?P<fy>20\d{2})\s*[/\-]\s*(?P<fm>\d{1,2})\s*[/\-]\s*(?P<fd>\d{1,2})(?!\d))"
    r"|(?P<date_cjk_full>(?P<cy>20\d{2})\s*年\s*(?P<cm>\d{1,2})\s*月\s*(?P<cd>\d{1,2})\s*日?)"
    r"|(?P<date_cjk>(?P<km>\d{1,2})\s*月\s*(?P<kd>\d{1,2})\s*日)"
    r"|(?P<date_short>(?<![\d/])(?P<sm>\d{1,2})\s*/\s*(?P<sd>\d{1,2})(?![\d/]))"
    r"|(?P<time>(?P<mk>上午|下午|中午|晚上|凌晨)?\s*(?P<h>\d{1,2})"
    r"\s*(?::\s*(?P<mi>\d{2})|點(?:\s*(?P<mi2>\d{2})\s*分?|整)?|時(?:整)?)(?!\d))"
    r"|(?P<sep>~|至|到|—|–|－|-)"
    r"|(?P<until>登錄期限|期限|截止|最晚)"
    r"|(?P<frm>起)"
)

_RECUR_MONTH_DAY = re.compile(
    r"每月\s*(?P<day>\d{1,2})\s*[日號](?P<between>.{0,14}?)"
    r"(?P<mk>上午|下午|中午)?\s*(?P<h>\d{1,2})\s*(?::\s*(?P<mi>\d{2})|[點時](?:整)?)"
    r".{0,12}?(?:開放|開始)?登錄"
)
_RECUR_NTH_WEEKDAY = re.compile(
    r"每月第\s*(?P<nth>[一二三四五1-5])\s*(?:個|週)?\s*(?:星期|週)\s*(?P<wd>[一二三四五六日天])"
    r"\D{0,10}?(?P<mk>上午|下午|中午)?\s*(?P<h>\d{1,2})\s*(?::\s*(?P<mi>\d{2})|[點時](?:整)?)"
    r".{0,12}?(?:開放|開始)?登錄"
)
_RECUR_PER_PERIOD = re.compile(
    r"(?:每月|每期|每檔|逐月|每一期)\s*(?:均|都)?\s*需?\s*(?:重新)?\s*登錄"
)

_PERIOD_LABEL = re.compile(r"(?:活動|優惠|消費)(?:期間|日期|時間)\s*[:：]?\s*")


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    start: int
    end: int
    raw: str
    year: int | None = None
    month: int | None = None
    day: int | None = None
    hour: int | None = None
    minute: int | None = None


def _hour_minute(hour_text: str, minute_text: str | None, marker: str | None) -> tuple[int, int]:
    hour = int(hour_text)
    minute = int(minute_text or 0)
    if (marker in {"下午", "晚上"} and hour < 12) or (marker == "中午" and hour < 11):
        hour += 12
    elif marker in {"上午", "凌晨"} and hour == 12:
        hour = 0
    return hour, minute


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    for match in _TOKEN.finditer(text):
        group = match.lastgroup
        span = match.span()
        raw = match.group(0)
        if match.group("date_full"):
            tokens.append(Token("DATE", *span, raw, int(match.group("fy")),
                                int(match.group("fm")), int(match.group("fd"))))
        elif match.group("date_cjk_full"):
            tokens.append(Token("DATE", *span, raw, int(match.group("cy")),
                                int(match.group("cm")), int(match.group("cd"))))
        elif match.group("date_cjk"):
            tokens.append(Token("DATE", *span, raw, None,
                                int(match.group("km")), int(match.group("kd"))))
        elif match.group("date_short"):
            tokens.append(Token("DATE", *span, raw, None,
                                int(match.group("sm")), int(match.group("sd"))))
        elif match.group("time"):
            hour, minute = _hour_minute(
                match.group("h"),
                match.group("mi") or match.group("mi2"),
                match.group("mk"),
            )
            if hour > 24 or minute > 59:
                continue
            tokens.append(Token("TIME", *span, raw, hour=hour, minute=minute))
        elif match.group("sep"):
            tokens.append(Token("SEP", *span, raw))
        elif match.group("until"):
            tokens.append(Token("UNTIL", *span, raw))
        elif group == "frm":
            tokens.append(Token("FROM", *span, raw))
    return tokens


def _cluster(tokens: list[Token], text: str) -> list[list[Token]]:
    clusters: list[list[Token]] = []
    current: list[Token] = []
    for token in tokens:
        if not current:
            current = [token]
            continue
        gap = text[current[-1].end : token.start]
        if len(gap) > MAX_TOKEN_GAP or any(ch in gap for ch in "\n。；;、"):
            clusters.append(current)
            current = [token]
        else:
            current.append(token)
    if current:
        clusters.append(current)
    return clusters


def _nearest(text: str, position: int, needles: tuple[str, ...]) -> int | None:
    """回傳 position 之前最靠近的 needle 起點，找不到回 None。"""
    window_start = max(0, position - CONTEXT_BEFORE)
    window = text[window_start:position]
    best: int | None = None
    for needle in needles:
        index = window.rfind(needle)
        if index != -1 and (best is None or index > best):
            best = index
    return None if best is None else window_start + best


def _is_registration_context(text: str, cluster: list[Token]) -> tuple[bool, bool]:
    """判斷叢集是否為登錄時間，並回傳是否命中「明確」的登錄用語。"""
    start, end = cluster[0].start, cluster[-1].end
    context = text[max(0, start - CONTEXT_BEFORE) : min(len(text), end + CONTEXT_AFTER)]
    if any(hint in context for hint in _NEGATIVE_HINTS):
        return False, False
    if not any(hint in context for hint in _REGISTRATION_HINTS):
        return False, False
    # 「活動期間」若比任何登錄字樣更靠近，這是可消費期間而非登錄期間
    spend_at = _nearest(text, start, _SPEND_PERIOD_HINTS)
    register_at = _nearest(text, start, _REGISTRATION_HINTS)
    if spend_at is not None and (register_at is None or spend_at > register_at):
        return False, False
    specific = any(marker in context for marker in _SPECIFIC_MARKERS)
    return True, specific


def _resolve_year(
    token: Token, default_year: int, reference: date | None
) -> int:
    if token.year is not None:
        return token.year
    year = default_year
    if reference is not None:
        try:
            candidate = date(year, token.month or 1, token.day or 1)
        except ValueError:
            return year
        # 跨年活動：12 月的清單提到 1/15，指的是明年
        if candidate < reference - timedelta(days=180):
            year += 1
    return year


def _make(
    token: Token, year: int, hour: int, minute: int
) -> datetime | None:
    try:
        return datetime(year, token.month or 1, token.day or 1, hour, minute, tzinfo=TAIPEI)
    except ValueError:
        return None


def _evidence(text: str, cluster: list[Token], source_url: str) -> Evidence:
    start = max(0, cluster[0].start - 60)
    end = min(len(text), cluster[-1].end + 80)
    return Evidence(text=text[start:end].strip()[:400], source_url=source_url)


def find_windows(
    raw_text: str,
    *,
    default_year: int,
    reference: date | None = None,
    source_url: str = "",
) -> list[RegistrationWindow]:
    """從自由文字抽出登錄視窗。

    ``default_year`` 用於補齊沒有年份的日期（銀行常寫「8/7 17:00 開放登錄」）。
    ``reference`` 提供跨年判斷的基準日。
    """
    text = normalize(raw_text)
    windows: list[RegistrationWindow] = []
    seen: set[tuple[str, str, str]] = set()

    for cluster in _cluster(tokenize(text), text):
        ok, specific = _is_registration_context(text, cluster)
        if not ok:
            continue
        core = [t for t in cluster if t.kind in {"DATE", "TIME", "SEP"}]
        # 一個叢集可能含多個視窗 —— 真實案例（聯邦波次登錄）：
        # 「第一波 8/15下午3點開放登錄至8/20 第二波 9/15下午3點開放登錄至10/20」
        # 兩個波次的 token 距離很近會落在同一叢集。因此在叢集內做貪婪掃描，
        # 每次取最長可比對的 pattern，而不是要求整個叢集符合單一 pattern。
        index = 0
        while index < len(core):
            for pattern in _PATTERNS:
                width = len(pattern)
                if tuple(t.kind for t in core[index : index + width]) != pattern:
                    continue
                chunk = core[index : index + width]
                before = text[max(0, chunk[0].start - 30) : chunk[0].start]
                tail = text[chunk[-1].end : chunk[-1].end + 4]
                wants_deadline = (
                    any(hint in before for hint in _DEADLINE_HINTS)
                    or bool(re.match(r"\s*[止前]", tail))
                )
                built = _assemble(
                    pattern, chunk, default_year, reference, wants_deadline=wants_deadline
                )
                index += width
                if built is None:
                    break
                kind, start, end, confidence = built
                if not specific:
                    confidence -= 0.10
                key = (
                    kind,
                    start.isoformat() if start else "",
                    end.isoformat() if end else "",
                )
                if key in seen:
                    break
                seen.add(key)
                windows.append(
                    RegistrationWindow(
                        kind=kind,
                        start=start,
                        end=end,
                        confidence=round(max(0.0, min(1.0, confidence)), 2),
                        evidence=_evidence(text, chunk, source_url),
                    )
                )
                break
            else:
                index += 1

    windows.sort(key=lambda w: (w.anchor, w.kind))
    return _drop_subsumed(windows)


def _drop_subsumed(windows: list[RegistrationWindow]) -> list[RegistrationWindow]:
    """剔除被完整區間涵蓋的單點視窗。

    真實案例（玉山活動二）：原文既寫「8/20 17:00開放登錄」也寫
    「2026/8/20 17:00~2026/8/31 23:59統一開放…登錄」。兩者是同一件事，
    但貪婪掃描會各自產生一個視窗。保留資訊較完整的 range，
    否則 UI 會顯示兩個看起來衝突的登錄時間。
    """
    ranges = [
        window
        for window in windows
        if window.kind == "range" and window.start is not None and window.end is not None
    ]
    if not ranges:
        return windows
    kept: list[RegistrationWindow] = []
    for window in windows:
        if window.kind == "range":
            kept.append(window)
            continue
        anchor = window.anchor
        covered = any(
            span.start is not None and span.end is not None and span.start <= anchor <= span.end
            for span in ranges
        )
        if not covered:
            kept.append(window)
    return kept


def _assemble(
    kinds: tuple[str, ...],
    core: list[Token],
    default_year: int,
    reference: date | None,
    *,
    wants_deadline: bool,
) -> tuple[WindowKind, datetime | None, datetime | None, float] | None:
    """把 token 叢集轉成 (kind, start, end, confidence)。

    優先權在這裡是顯式的：完整區間 > 同日區間 > 純日期區間 > 單一時點。
    舊實作靠六個 pattern 的宣告順序隱含決定，秒級時間讓第一層失配時
    會無聲降級；這裡任何降級都會反映在 confidence 上。
    """
    inferred_year = any(t.kind == "DATE" and t.year is None for t in core)
    penalty = 0.05 if inferred_year else 0.0

    def dt_at(index_date: int, index_time: int | None, fallback: time) -> datetime | None:
        token = core[index_date]
        year = _resolve_year(token, default_year, reference)
        if index_time is None:
            return _make(token, year, fallback.hour, fallback.minute)
        clock = core[index_time]
        return _make(token, year, clock.hour or 0, clock.minute or 0)

    if kinds == ("DATE", "TIME", "SEP", "DATE", "TIME"):
        start = dt_at(0, 1, time(0, 0))
        end = dt_at(3, 4, time(23, 59))
        confidence = 0.95
    elif kinds == ("DATE", "TIME", "SEP", "DATE"):
        start = dt_at(0, 1, time(0, 0))
        end = dt_at(3, None, time(23, 59))
        confidence = 0.85
    elif kinds == ("DATE", "TIME", "SEP", "TIME"):
        start = dt_at(0, 1, time(0, 0))
        clock = core[3]
        end = (
            start.replace(hour=clock.hour or 0, minute=clock.minute or 0)
            if start is not None
            else None
        )
        confidence = 0.90
    elif kinds == ("DATE", "SEP", "DATE"):
        start = dt_at(0, None, time(0, 0))
        end = dt_at(2, None, time(23, 59))
        confidence = 0.80
    elif kinds == ("DATE", "TIME"):
        moment = dt_at(0, 1, time(0, 0))
        if wants_deadline:
            return ("deadline", None, moment, 0.80 - penalty) if moment else None
        return ("opens_at", moment, None, 0.80 - penalty) if moment else None
    elif kinds == ("DATE",):
        if not wants_deadline:
            return None
        moment = dt_at(0, None, time(23, 59))
        return ("deadline", None, moment, 0.55 - penalty) if moment else None
    else:
        return None

    if start is None or end is None:
        return None
    if end < start:
        # 跨年區間：8/7 ~ 1/31 指的是隔年 1/31
        try:
            end = end.replace(year=end.year + 1)
        except ValueError:
            return None
        confidence -= 0.05
    return ("range", start, end, confidence - penalty)


def drop_period_echoes(
    windows: list[RegistrationWindow],
    period_start: date | None,
    period_end: date | None,
) -> list[RegistrationWindow]:
    """剔除「其實是活動期間」的假登錄視窗。

    真實案例（中信）：``2026/7/1~2026/9/30 限信用卡且限量需登錄(每週二開放3,000名)``
    —— 那個區間是可消費期間，真正的登錄時點是「每週二」。tokenizer 只看得到
    附近有「登錄」二字，無法分辨；但把它跟 adapter 已知的活動期間對照就很清楚。

    只在該視窗沒有明確的登錄期間用語時才剔除，避免誤刪
    「登錄期間與活動期間相同」這種合法情形。
    """
    if period_start is None or period_end is None:
        return windows
    kept: list[RegistrationWindow] = []
    for window in windows:
        echoes_period = (
            window.kind == "range"
            and window.start is not None
            and window.end is not None
            and window.start.date() == period_start
            and window.end.date() == period_end
        )
        evidence_text = window.evidence.text if window.evidence else ""
        explicit = any(marker in evidence_text for marker in ("登錄期間", "開放登錄", "登錄時間"))
        if echoes_period and not explicit:
            continue
        kept.append(window)
    return kept


def detect_recurrence(raw_text: str) -> Recurrence:
    """偵測「登錄一次不夠」的活動。

    實測全站 30.6% 的需登錄活動屬於此類，舊 schema 完全沒有表達，
    使用者會以為登錄一次就結束。這裡只做偵測不做展開 —— 展開成一串
    單次事件會讓行事曆變成垃圾，正確做法是在 .ics 輸出 RRULE。
    """
    text = normalize(raw_text)
    match = _RECUR_MONTH_DAY.search(text)
    if match:
        hour, minute = _hour_minute(match.group("h"), match.group("mi"), match.group("mk"))
        return Recurrence(
            kind="monthly",
            note=f"每月 {int(match.group('day'))} 日 {hour:02d}:{minute:02d} 開放登錄",
            confidence=0.85,
        )
    match = _RECUR_NTH_WEEKDAY.search(text)
    if match:
        hour, minute = _hour_minute(match.group("h"), match.group("mi"), match.group("mk"))
        return Recurrence(
            kind="monthly",
            note=(
                f"每月第 {match.group('nth')} 個星期{match.group('wd')} "
                f"{hour:02d}:{minute:02d} 開放登錄"
            ),
            confidence=0.80,
        )
    if _RECUR_PER_PERIOD.search(text):
        return Recurrence(kind="per_campaign_period", note="每期需重新登錄", confidence=0.70)
    return Recurrence()


def find_date_range(
    raw_text: str,
    *,
    default_year: int,
    reference: date | None = None,
    limit_chars: int = 400,
) -> tuple[date | None, date | None, float, str]:
    """沒有標籤的裸日期區間，例如 ``2026/01/01~2026/12/31``。

    只作為 find_period 失敗後的最後手段，且僅看文字開頭一段 —— 活動頁的期間
    幾乎總在標題附近，往後找容易撈到注意事項裡的其他日期。信心刻意壓低。

    實測需求：台北富邦的明細頁直接印一行日期區間，沒有「活動期間：」字樣，
    108 筆中有 83 筆因此抓不到期間。
    """
    text = normalize(raw_text)[:limit_chars]
    tokens = [t for t in tokenize(text) if t.kind in {"DATE", "SEP"}]
    for index in range(len(tokens) - 2):
        window = tokens[index : index + 3]
        if [t.kind for t in window] != ["DATE", "SEP", "DATE"]:
            continue
        first, second = window[0], window[2]
        try:
            start = date(
                _resolve_year(first, default_year, reference), first.month or 1, first.day or 1
            )
            end = date(
                _resolve_year(second, default_year, reference), second.month or 1, second.day or 1
            )
        except ValueError:
            continue
        if end < start:
            try:
                end = end.replace(year=end.year + 1)
            except ValueError:
                continue
        evidence = text[max(0, first.start - 20) : second.end + 20].strip()
        return start, end, 0.55, evidence
    return None, None, 0.0, ""


def find_period(
    raw_text: str, *, default_year: int, reference: date | None = None
) -> tuple[date | None, date | None, float, str]:
    """抽出「活動期間」（可消費期間），與登錄期間分開處理。

    回傳 (start, end, confidence, evidence)。抓不到結束日就留 None，
    不推定 —— 單一日期的活動不代表當天結束。
    """
    text = normalize(raw_text)
    for label in _PERIOD_LABEL.finditer(text):
        segment = text[label.end() : label.end() + 60]
        tokens = tokenize(segment)
        core = [t for t in tokens if t.kind in {"DATE", "SEP"}]
        kinds = tuple(t.kind for t in core)
        if kinds[:3] == ("DATE", "SEP", "DATE"):
            first, second = core[0], core[2]
            start_year = _resolve_year(first, default_year, reference)
            end_year = _resolve_year(second, default_year, reference)
            try:
                start = date(start_year, first.month or 1, first.day or 1)
                end = date(end_year, second.month or 1, second.day or 1)
            except ValueError:
                continue
            if end < start:
                end = end.replace(year=end.year + 1)
            evidence = text[label.start() : label.end() + 40].strip()
            return start, end, 0.9, evidence
        if kinds[:1] == ("DATE",):
            first = core[0]
            try:
                start = date(
                    _resolve_year(first, default_year, reference),
                    first.month or 1,
                    first.day or 1,
                )
            except ValueError:
                continue
            evidence = text[label.start() : label.end() + 40].strip()
            return start, None, 0.6, evidence
    return None, None, 0.0, ""
