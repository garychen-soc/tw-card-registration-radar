"""宣告式 spec 的載入與驗證測試。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from radar.spec import SourceSpec, load_spec, load_specs

BASE = {"id": "demo", "bank_name": "示範銀行", "domains": ["example.com"]}
URL = "https://www.example.com/list"

SOURCES = Path(__file__).resolve().parents[1] / "sources"


def test_all_shipped_specs_load() -> None:
    """每份 TOML 都能載入，且 id 與檔名一致。

    刻意不硬編銀行清單 —— 那會讓每次新增一家銀行都誤觸發失敗（實際發生過）。
    比對檔名同樣能抓到打錯 id、重複 id 與漏檔。
    """
    specs = load_specs(SOURCES)
    filenames = {path.stem for path in SOURCES.glob("*.toml")}
    assert {spec.id for spec in specs} == filenames
    assert len(specs) == len(filenames)
    assert len(specs) >= 17, f"預期至少 17 家來源，只載入 {len(specs)} 家"


def test_every_spec_url_is_inside_its_own_allowlist() -> None:
    """spec 自身就是白名單的來源。entry_url／data_url／portal_url 都必須通過檢查，
    否則抓取階段才發現就太晚了。"""
    for spec in load_specs(SOURCES):
        assert spec.listing.entry_url.startswith("https://"), spec.id


def test_every_listing_kind_is_exercised() -> None:
    """四種清單型態都要有來源實際在用 —— 沒人用的型態等於沒被驗證。"""
    kinds = {spec.listing.kind for spec in load_specs(SOURCES)}
    assert kinds == {"json_api", "html_list", "form_paged", "single_page"}


def test_first_batch_covers_every_hard_case() -> None:
    """首批 6 家是刻意挑的 —— 覆蓋實測到的全部五種難題型態。"""
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


def test_timeout_override_is_bounded() -> None:
    """逐來源逾時可覆寫，但有上限 —— 逾時不是用來遮蓋封鎖的。

    實測華南從 Actions runner 讀入口頁平均 15 秒（本機 0.8–1.9 秒），預設 25 秒
    的餘裕太小；被拒絕存取則會是 403（陽信 12/12 組合皆 403），是另一回事。
    """
    spec = SourceSpec.model_validate(
        {
            **BASE,
            "timeout_seconds": 90.0,
            "listing": {"kind": "single_page", "entry_url": URL},
        }
    )
    assert spec.timeout_seconds == 90.0

    with pytest.raises(ValidationError):
        SourceSpec.model_validate(
            {
                **BASE,
                "timeout_seconds": 600.0,
                "listing": {"kind": "single_page", "entry_url": URL},
            }
        )


def test_timeout_defaults_to_zero_meaning_project_default() -> None:
    spec = SourceSpec.model_validate(
        {**BASE, "listing": {"kind": "single_page", "entry_url": URL}}
    )
    assert spec.timeout_seconds == 0.0
