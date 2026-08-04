"""與銀行無關的純解析函式。

adapter 負責「怎麼拿到文字」，這裡負責「文字代表什麼」。兩者分開才能讓
17 家銀行共用同一套時間與條件解析，也才能用 golden corpus 做回歸。
"""

from .contract import derive as derive_contract
from .contract import spend_window
from .datetimes import detect_recurrence, drop_period_echoes, find_period, find_windows
from .normalize import normalize, normalize_inline

__all__ = [
    "derive_contract",
    "detect_recurrence",
    "drop_period_echoes",
    "find_period",
    "find_windows",
    "normalize",
    "normalize_inline",
    "spend_window",
]
