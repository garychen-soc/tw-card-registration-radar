# ADR 0003：改用 selectolax 的 Lexbor backend

## 問題與選擇

2026-10-12 的 Scrape run `38191389835` 安裝 selectolax 1.0.0 後，在載入
`listing.py` 時拋出 ImportError，尚未開始抓取。`htmltext.py` 也使用相同的
`selectolax.parser.HTMLParser`。全專案搜尋沒有其他 Modest 匯入或已移除的
`parse_fragment` 呼叫。

| 方案 | 優點 | 代價 |
| --- | --- | --- |
| 保留 Modest 並限制 `selectolax<1` | 修正小，保留舊解析行為，適合緊急回復 | 繼續依賴停止維護的 backend；無法取得 Lexbor 後續修正，日後仍須遷移 |
| 改用 `LexborHTMLParser` | 使用上游持續維護、符合 HTML5 的 backend，可使用 1.x 修正 | DOM 修復、序列化、文字切分可能不同，必須核對解析與下游結果 |

選擇第二個方案，依賴範圍為 `selectolax>=1.0.0,<2.0.0`。下一個主要版本須經
明確驗證再放行。Probe 改為安裝本專案，與其他 workflow 使用同一份依賴限制。

上游來源：
[selectolax v1.0.0 release](https://github.com/rushter/selectolax/releases/tag/v1.0.0)、
[Lexbor API](https://selectolax.readthedocs.io/en/latest/lexbor.html)。

## 行為邊界

HTTP 層已提供解碼後的 `str`；本次不改變字元編碼策略。保留完整文件解析模式
（`is_fragment=False`，預設），以維持 `body`、`root` 與 HTML 片段呼叫端的介面。
沿用 `css`、`css_first`、`attributes`、`html`、`text`、`decompose` 的既有用法。
沒有以直接回傳全文或放寬 selector 來繞過解析問題。

新增回歸測試核對繁體中文、HTML entities、換行、缺少結束標籤的表格、空文件、
script/style/noscript/template 與巢狀導覽移除、分頁面板範圍、data-link 優先序、
活動標題與清單日期、登錄時點、名額與資格，以及清單去重與鏡射頁去重。
另外從 `build_site.main()` 驗證覆蓋率崩落回傳退出碼 4，網站檔案逐位元保持不變，
且不呼叫 `write_site`。

Scrape 的兩個 job 在抓取之前執行完整 pytest。退出碼 0/2/4、unexpected exit
檢查、`--only` 強制 dry-run、資料提交条件、發布防護門檻均保留。

## 本機驗證（2026-10-12）

- Python 3.13.16：Modest/selectolax 0.4.13 與 Lexbor/selectolax 1.0.0 各通過
  266 個測試（原有 258 個，加上 8 個回歸案例）。
- Lexbor：`ruff check src tests` 通過；`mypy` 通過全部 19 個來源檔案。
- 唯讀抓取中信、國泰、玉山、富邦，涵蓋 single_page、json_api、form_paged、
  html_list/Wicket；保存 20 筆回應，讓新舊 backend 解析完全相同的輸入。
- 測試刻意限制玉山／富邦分頁上限為 2，明細每來源最多 3 筆：分別讀到
  1/147/18/90 筆清單，抽樣產出 10 個 Campaign、15 個 Offer。
  這是解析相容性抽樣，不能視為四家資料完整性驗證。富邦仍報告官方宣告
  171 筆、取得 90 筆及分頁上限警示；新舊 backend 的警示相同。
- 重播比對：清單所有欄位、20 筆回應的文字與連結、活動所有業務欄位、
  去重計數與防護決策完全相同。只有執行當下產生的 `health.checked_at` 不同。

單元測試不依賴網路或舊 backend。原始官方回應只留在本機診斷目錄，不納入版控。
GitHub 的 Python 3.11 Verify 與實際 Scrape 結果須另外核對，不能以本機抽樣替代。
