"""宣告式來源設定。

前身每家銀行一個手寫 extractor（``extract_yuanta`` 205 行、``extract_esun`` 231 行），
差異只在清單 selector 與分頁機制，其餘約 120 行的「查快取 → 併發抓明細 →
組活動 → 算來源健康」完全重複。17 家中只有 9 家共用了後來抽出的通用管線，
修一個共通缺陷要改 9 處。

這裡把「怎麼拿到文字」宣告成資料。新增一家銀行是加一份 TOML 加一個 fixture，
不是加 200 行 Python。三種清單型態涵蓋實測到的所有情形：

``json_api``
    官方直接提供 JSON 清單端點（國泰世華、玉山、台中銀、聯邦）。
``html_list``
    清單在 HTML 裡，用正則或 selector 取出（元大、中信）。
``form_paged``
    需要 POST 表單狀態才能翻頁（台北富邦、元大的分頁）。

``extra="forbid"`` 是刻意的：TOML 打錯欄位名必須立刻失敗，而不是靜默忽略
然後在抓取階段產出空清單。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ListingKind = Literal["json_api", "html_list", "form_paged"]
DetailSource = Literal["html", "api", "none"]
Cardinality = Literal["one", "many"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListingSpec(Strict):
    kind: ListingKind
    entry_url: str = Field(description="人可讀的官方入口，UI 顯示與來源健康用")
    data_url: str = ""
    item_selector: str = ""
    link_pattern: str = ""
    items_path: list[str] = Field(default_factory=list)
    fields: dict[str, str] = Field(default_factory=dict)
    categories: list[str] = Field(default_factory=list)
    form_fields: dict[str, str] = Field(default_factory=dict)
    max_pages: int = 1

    @model_validator(mode="after")
    def _requires_source(self) -> ListingSpec:
        if self.kind == "json_api" and not self.data_url:
            raise ValueError("json_api 清單必須指定 data_url")
        if self.kind in {"html_list", "form_paged"} and not (
            self.item_selector or self.link_pattern
        ):
            raise ValueError(f"{self.kind} 清單必須指定 item_selector 或 link_pattern")
        return self


class DetailSpec(Strict):
    source: DetailSource = "html"
    cardinality: Cardinality = "one"
    boundary: str = Field(
        default="",
        description="子活動邊界正則。留空代表用 segment 的通用候選錨點。",
    )
    table_tiers: bool = Field(
        default=False,
        description="明細頁是否有 dt/th 階梯門檻表（實測僅聯邦如此）。",
    )


class RegistrationSpec(Strict):
    portal_url: str = ""
    portal_kind: Literal["activity_specific", "bank_portal", "unknown"] = "unknown"
    portal_hint: str = ""


class ConditionsSpec(Strict):
    known_cards: list[str] = Field(
        default_factory=list,
        description=(
            "已確認存在的卡別名稱。刻意不用通用的「XX卡」正則 —— 那會把"
            "「簽帳金融卡、公司卡及採購卡等，恕不適用」這種排除條款抓成適用卡別。"
            "只填實際在官方頁觀察到的名稱，不得憑印象填寫。"
        ),
    )


class SourceSpec(Strict):
    id: str
    bank_name: str
    domains: list[str] = Field(min_length=1)
    note: str = ""
    listing: ListingSpec
    detail: DetailSpec = Field(default_factory=DetailSpec)
    registration: RegistrationSpec = Field(default_factory=RegistrationSpec)
    conditions: ConditionsSpec = Field(default_factory=ConditionsSpec)

    @model_validator(mode="after")
    def _entry_url_within_domains(self) -> SourceSpec:
        from .transport import is_allowed

        if not is_allowed(self.listing.entry_url, self.domains):
            raise ValueError(
                f"{self.id}: entry_url 不在 domains 白名單內或非 https：{self.listing.entry_url}"
            )
        for url in (self.listing.data_url, self.registration.portal_url):
            if url and not is_allowed(url, self.domains):
                raise ValueError(f"{self.id}: URL 不在 domains 白名單內或非 https：{url}")
        return self


def load_spec(path: Path) -> SourceSpec:
    return SourceSpec.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))


def load_specs(directory: Path) -> list[SourceSpec]:
    specs = [load_spec(path) for path in sorted(directory.glob("*.toml"))]
    seen: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            raise ValueError(f"重複的來源 id：{spec.id}")
        seen.add(spec.id)
    return specs
