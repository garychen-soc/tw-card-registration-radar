"""頁面文字快取。

**為什麼快取文字而不是快取解析結果。** 前身（以及本專案的第一版）快取的是
推導出來的活動資料：清單指紋未變就沿用上一版的 Offer。那個做法有一個致命
缺陷 —— 解析器的修正無法傳播。今天修好 ``HH:MM:SS`` 的解析，指紋未變的頁面
還會繼續掛著錯的登錄時間最多 30 天。這正是「衍生狀態被凍結」那一類問題，
用快取把它重新引進來很不划算。

改成快取**輸入**（頁面文字）而不是**輸出**（解析結果）：每次執行都用當下的
解析器重新推導，修正立即生效於全部頁面；快取只用來省下重新下載。

**為什麼要存文字才能用條件式 GET。** 304 回應不帶 body。若不另存文字，
收到 304 就無事可做（本專案第一版就因此在第二次執行時把 223 筆掉成 92 筆）。
存了文字，304 就變成「用存的文字重新推導」。

**實測涵蓋率。** 六家來源裡只有國泰世華與聯邦提供 ETag／Last-Modified，
其餘四家送 ``no-store``／``private``。所以這個快取是免費紅利而非依賴 ——
沒命中就重抓，對正確性零影響。

**尊重 no-store。** 明確送 ``no-store`` 的主機不保存 body。
"""

from __future__ import annotations

import hashlib
from pathlib import Path


class PageStore:
    """以 URL 為鍵的頁面文字存放。內容不進版控（見 .gitignore）。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.root / digest[:2] / f"{digest}.txt"

    def get(self, url: str) -> str | None:
        path = self._path(url)
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def put(self, url: str, text: str, *, cache_control: str = "") -> bool:
        """存下頁面文字。回傳是否真的存了。

        主機明確要求 ``no-store`` 時不保存 —— 那是它對快取的明示意願，
        即使我們存的是衍生文字而非原始回應。
        """
        if "no-store" in cache_control.lower():
            return False
        path = self._path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(text, encoding="utf-8")
        except OSError:
            return False
        return True

    def has(self, url: str) -> bool:
        return self._path(url).exists()
