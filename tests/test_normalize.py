from __future__ import annotations

from radar.parse.normalize import normalize, normalize_inline


def test_roc_year_becomes_gregorian() -> None:
    assert "2026年1月1日" in normalize("115年1月1日")
    assert "2026/8/7" in normalize("115/8/7")


def test_roc_year_bounded_to_plausible_range() -> None:
    """『滿 150 元』不是民國年。舊實作用 `1\\d{2}` 會誤判。"""
    assert "150 元" in normalize("滿 150 元")
    assert "2061" not in normalize("滿 150 元")


def test_seconds_are_collapsed_on_both_sides() -> None:
    """一行裡兩個秒級時間都要收斂 —— 這是舊實作區間解析失敗的直接原因。"""
    assert normalize("2026/8/7 17:00:00~2026/8/31 23:59:00") == (
        "2026/8/7 17:00~2026/8/31 23:59"
    )


def test_fullwidth_characters_are_folded() -> None:
    folded = normalize("２０２６／８／７　１７：００")
    assert "2026/8/7" in folded
    assert "17:00" in folded


def test_fullwidth_tilde_becomes_ascii() -> None:
    assert "~" in normalize("8/1～8/31")


def test_chinese_punctuation_is_preserved() -> None:
    """evidence 要呈現給使用者核對，中文標點不得被折成半角。

    這排除了 unicodedata.normalize("NFKC") —— 它會把「，。（）！？」一併轉成 ASCII。
    """
    body = "單筆滿10,000元享5%回饋，適用一次付清（含分期）。詳見活動辦法！"
    assert normalize(body) == body


def test_amount_range_is_not_corrupted() -> None:
    """不做 `至`→`~` 的全域替換。舊實作會把金額區間也改掉。"""
    assert "滿10,000元至20,000元" in normalize("單筆滿10,000元至20,000元")


def test_normalize_inline_flattens_newlines() -> None:
    assert "\n" not in normalize_inline("活動期間\n2026/8/1~2026/8/31")


def test_invisible_characters_are_stripped() -> None:
    """實測聯邦頁面有子活動的首行只是一個 BOM，不清掉會產出空標題。"""
    assert normalize("﻿夏日回饋​活動") == "夏日回饋活動"
