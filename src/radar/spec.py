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
    需要 POST 表單狀態才能翻頁（元大、玉山）。
``single_page``
    入口頁本身就是全部內容，沒有逐活動的網址（中信的 LINE Pay 頁一頁 14 個
    活動，頁上的連結全指向 LINE 的網域，不是活動明細）。

``extra="forbid"`` 是刻意的：TOML 打錯欄位名必須立刻失敗，而不是靜默忽略
然後在抓取階段產出空清單。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ListingKind = Literal["json_api", "html_list", "form_paged", "single_page"]
DetailSource = Literal["html", "api", "none"]
Cardinality = Literal["one", "many"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListingSpec(Strict):
    kind: ListingKind
    entry_url: str = Field(description="人可讀的官方入口，UI 顯示與來源健康用")
    data_url: str = ""
    item_selector: str = ""
    title_selector: str = ""
    summary_selector: str = ""
    link_pattern: str = ""
    post_url: str = Field(
        default="",
        description="form_paged 的 POST 目標。留空則 POST 回 entry_url（元大式）；"
        "指定另一個端點則第一頁也用 POST（玉山式）。",
    )
    form_data: dict[str, str] = Field(
        default_factory=dict, description="每次請求都要附帶的固定表單參數"
    )
    total_pattern: str = Field(
        default="",
        description="從回應中取出總筆數的正則（group 1）。用於總頁數 = "
        "總筆數 ÷ 首頁筆數，適用於只公告總筆數而非總頁數的端點。",
    )
    pagination_kind: Literal["none", "wicket_ajax"] = Field(
        default="none",
        description="具名的分頁策略。wicket_ajax 用於 Apache Wicket 的 Ajax 分頁"
        "（台北富邦），它的分頁連結帶頁面版本狀態，必須改寫 URL 並附 Wicket 標頭。",
    )
    pagination_base: str = Field(
        default="",
        description="wicket_ajax 的 Wicket-Ajax-BaseURL 標頭值，例如 promotion/Result。",
    )
    category_codes: list[str] = Field(
        default_factory=list,
        description="要逐一請求的分類代碼，搭配 url_template 使用（台新分 A–I 九類）。"
        "與 categories 不同 —— 後者是 json_api 的分類白名單過濾。",
    )
    url_template: str = Field(
        default="",
        description="分類清單的網址樣板，用 {category} 佔位（搭配 category_codes）。",
    )
    scope_selector: str = Field(
        default="",
        description="把清單 HTML 限縮到這個容器再找連結。與 detail 的同名欄位同義。",
    )
    scope_tab_label: str = Field(
        default="",
        description="分頁面板的標籤文字（例如「信用卡」）。找 aria-label 等於它的 "
        "tab，再取 aria-controls 指到的面板。華南把全行各業務的活動放在同一頁的"
        "不同分頁裡，不限縮會混進存款、貸款、保險的連結。",
    )
    items_path: list[str] = Field(default_factory=list)
    fields: dict[str, str] = Field(
        default_factory=dict,
        description="欄位映射。值可用點號指向巢狀路徑，例如 "
        "`start = \"campaignProps.startDate\"`。",
    )
    url_strip_prefix: str = Field(
        default="",
        description="從清單取得的路徑要去掉的前綴。國泰世華給的是 AEM 內部路徑 "
        "`/content/cub-aem-cs/zh-tw/...`，公開網址要去掉它。",
    )
    url_base: str = Field(default="", description="去掉前綴後要接上的公開網址前綴")
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
    scope_selector: str = Field(
        default="",
        description="把分析範圍限縮到這個 CSS 容器。找不到就用整頁（降級而非失敗）。",
    )
    scope_tab_label: str = Field(
        default="",
        description="分頁面板的間接指向：找 aria-label 等於此值的 tab，"
        "再取它 aria-controls 指到的容器。實測華南的活動全在「信用卡」分頁裡。",
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
    user_agent: str = Field(
        default="",
        description="覆寫 User-Agent。只在銀行的防護會擋掉帶專案標識的 UA 時使用，"
        "並須在 note 說明原因 —— 預設一律用可識別的 UA。",
    )
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
        for url in (
            self.listing.data_url,
            self.listing.post_url,
            self.registration.portal_url,
        ):
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
