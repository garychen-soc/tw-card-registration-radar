"""宣告式 spec 的載入與驗證測試。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from radar.spec import load_spec, load_specs

SOURCES = Path(__file__).resolve().parents[1] / "sources"


def test_all_shipped_specs_load() -> None:
    specs = load_specs(SOURCES)
    assert {spec.id for spec in specs} == {
        "cathay",
        "ctbc",
        "esun",
        "tcbbank",
        "ubot",
        "yuanta",
    }


def test_every_spec_url_is_inside_its_own_allowlist() -> None:
    """spec 自身就是白名單的來源。entry_url／data_url／portal_url 都必須通過檢查，
    否則抓取階段才發現就太晚了。"""
    for spec in load_specs(SOURCES):
        assert spec.listing.entry_url.startswith("https://"), spec.id


def test_first_batch_covers_every_hard_case() -> None:
    """首版 6 家是刻意挑的 —— 覆蓋實測到的全部五種難題型態。"""
    specs = {spec.id: spec for spec in load_specs(SOURCES)}
    assert specs["ubot"].detail.table_tiers is True, "階梯門檻表"
    assert specs["esun"].detail.cardinality == "many", "單頁多子活動 + 秒級時間"
    assert specs["cathay"].detail.source == "api", "SPA 明細頁，只能走 API"
    assert specs["ctbc"].detail.cardinality == "many", "單頁 14 個活動"
    assert specs["yuanta"].listing.kind == "form_paged", "表單分頁"
    assert specs["tcbbank"].listing.kind == "json_api", "民國年密集"


def test_json_api_without_data_url_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(
        """
id = "bad"
bank_name = "測試"
domains = ["example.com"]
[listing]
kind = "json_api"
entry_url = "https://www.example.com/"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="data_url"):
        load_spec(path)


def test_url_outside_domains_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(
        """
id = "bad"
bank_name = "測試"
domains = ["example.com"]
[listing]
kind = "json_api"
entry_url = "https://www.example.com/"
data_url = "https://evil.com/list.json"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="白名單"):
        load_spec(path)


def test_typo_in_field_name_fails_loudly(tmp_path: Path) -> None:
    """extra="forbid" 是刻意的：打錯欄位名必須立刻失敗，
    而不是靜默忽略然後在抓取階段產出空清單。"""
    path = tmp_path / "bad.toml"
    path.write_text(
        """
id = "bad"
bank_name = "測試"
domains = ["example.com"]
[listing]
kind = "html_list"
entry_url = "https://www.example.com/"
link_pattern = "x"
item_selctor = "typo"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="item_selctor"):
        load_spec(path)


def test_html_list_without_selector_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(
        """
id = "bad"
bank_name = "測試"
domains = ["example.com"]
[listing]
kind = "html_list"
entry_url = "https://www.example.com/"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="item_selector"):
        load_spec(path)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    body = """
id = "dup"
bank_name = "測試"
domains = ["example.com"]
[listing]
kind = "json_api"
entry_url = "https://www.example.com/"
data_url = "https://www.example.com/list.json"
"""
    (tmp_path / "a.toml").write_text(body, encoding="utf-8")
    (tmp_path / "b.toml").write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match="重複的來源 id"):
        load_specs(tmp_path)
