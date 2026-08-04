"""文字正規化 —— 所有時間與條件解析的前置步驟。

與舊實作最重要的兩點差異：

1. `HH:MM:SS` 在這裡就收斂成 `HH:MM`。舊實作的 range pattern 寫成
   `(\\d{1,2})[:點](\\d{2})?\\s*~`，遇到銀行常用的秒級寫法會失配，
   整段區間退化成「只有起點」，真實的登錄截止時間被丟棄。實測玉山明細頁
   有 21 處秒級寫法、中信 4 處，全站 388 個登錄視窗中 348 個（90%）
   因此帶著編造的結束時間。

2. **不做** `至`／`到` → `~` 的全域替換。舊實作全域替換，會把
   「單筆滿 10,000 元至 20,000 元」也改掉。這裡把範圍連接詞留給
   tokenizer 當成一種 token 判讀，只在日期時間的上下文中才視為區間分隔。
"""

from __future__ import annotations

import re

# 民國年合理範圍：100–159 → 2011–2070。舊實作用 `1\d{2}` 會把
# 「滿 150 元」這類數字誤判成民國年。
_ROC_YEAR = re.compile(r"(?<!\d)(1[0-5]\d)(?=\s*[年/\-－]\s*\d{1,2}\s*[月/\-－])")
_HMS = re.compile(r"(?<!\d)(\d{1,2}:\d{2}):\d{2}(?!\d)")
_WHITESPACE = re.compile(r"[ \t　\xa0]+")
_BLANK_LINES = re.compile(r"\n{2,}")

# 刻意不使用 unicodedata.normalize("NFKC")。NFKC 對 CJK 太激進 —— 它會把
# 全角中文標點「，。（）！？」一併轉成 ASCII，而 evidence 是要呈現給使用者
# 核對的原文，把中文逗號變成半角逗號是不能接受的。這裡只折疊解析真正需要的字元。
_FOLD = str.maketrans(
    {
        **{chr(0xFF10 + i): str(i) for i in range(10)},  # ０-９
        **{chr(0xFF21 + i): chr(0x41 + i) for i in range(26)},  # Ａ-Ｚ
        **{chr(0xFF41 + i): chr(0x61 + i) for i in range(26)},  # ａ-ｚ
        "：": ":",
        "／": "/",
        "　": " ",  # U+3000 ideographic space
        "～": "~",
        "〜": "~",
        "⁓": "~",
        "－": "-",  # U+FF0D fullwidth hyphen-minus
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "％": "%",
        "＄": "$",
        "＋": "+",
        "＝": "=",
        # em dash 與 en dash 保留原樣，由 tokenizer 視為範圍分隔
    }
)


def normalize(text: str) -> str:
    """回傳可供解析的正規化文字。

    偏移量會因替換而改變，因此 evidence 一律取自正規化後的文字切片
    —— evidence 的用途是讓人核對，不是位元組級的來源追溯。
    """
    value = text.translate(_FOLD)
    value = _ROC_YEAR.sub(lambda m: str(int(m.group(1)) + 1911), value)
    # 秒級收斂需重複套用：`17:00:00~23:59:00` 兩處都要處理
    while True:
        collapsed = _HMS.sub(r"\1", value)
        if collapsed == value:
            break
        value = collapsed
    value = _WHITESPACE.sub(" ", value)
    value = _BLANK_LINES.sub("\n", value)
    return value.strip()


def normalize_inline(text: str) -> str:
    """正規化並攤平成單行，供 evidence 節錄與指紋計算使用。"""
    return _WHITESPACE.sub(" ", normalize(text).replace("\n", " ")).strip()
