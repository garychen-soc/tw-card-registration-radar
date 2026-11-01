"""活動期間（日期區間）解析，供各銀行擷取器讀列表卡片與 API 欄位。

只處理「活動期間」這種日期粒度的區間，不處理登錄時點 —— 登錄時點由
``radar.extract`` 以原文引句為準計算，兩者刻意分開。
"""

from __future__ import annotations

import re
from datetime import date

from .textnorm import normalize

_SEP = r"\s*(?:~|-|–|—|至|到|起至|起~)\s*"
_YMD = (
    r"(?P<{p}y>20\d{{2}})\s*[/.\-年]\s*(?P<{p}m>\d{{1,2}})\s*[/.\-月]\s*(?P<{p}d>\d{{1,2}})\s*日?"
)
_MD = r"(?:(?P<{p}y>20\d{{2}})\s*[/.\-年]\s*)?(?P<{p}m>\d{{1,2}})\s*[/.\-月]\s*(?P<{p}d>\d{{1,2}})\s*日?"

_RANGE = re.compile(_YMD.format(p="a") + r"(?:\s*\([^)]{1,3}\))?" + _SEP + _MD.format(p="b"))
_UNTIL = re.compile(r"(?:即日起|即日)" + _SEP + _YMD.format(p="b"))
_SINGLE_END = re.compile(r"(?:至|到|~)\s*" + _YMD.format(p="b") + r"\s*(?:止|為止)?")


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_period(text: str) -> tuple[date | None, date | None]:
    """從一段文字取出第一個活動期間。取不到就回 ``(None, None)``，不猜。

    >>> parse_period("活動期間：115/10/1-12/31")
    (datetime.date(2026, 10, 1), datetime.date(2026, 12, 31))
    >>> parse_period("2026.09.01(二)~2026.11.30(一)")
    (datetime.date(2026, 9, 1), datetime.date(2026, 11, 30))
    >>> parse_period("即日起~2026/12/31")
    (None, datetime.date(2026, 12, 31))
    """
    value = normalize(text)
    # 星期括號「(二)」會擋在日期與分隔符之間
    value = re.sub(r"\(\s*[一二三四五六日天]\s*\)", "", value)
    match = _RANGE.search(value)
    if match:
        ay, am, ad = int(match["ay"]), int(match["am"]), int(match["ad"])
        by = int(match["by"]) if match["by"] else ay
        bm, bd = int(match["bm"]), int(match["bd"])
        start = _mk(ay, am, ad)
        end = _mk(by, bm, bd)
        if start and end and end < start and not match["by"]:
            end = _mk(ay + 1, bm, bd)
        if start and end and end >= start:
            return start, end
    match = _UNTIL.search(value) or _SINGLE_END.search(value)
    if match:
        return None, _mk(int(match["by"]), int(match["bm"]), int(match["bd"]))
    return None, None
