# 刷卡登錄雷達

台灣 18 家銀行的信用卡活動「登錄時點」雷達：每天抓官方活動頁，找出**什麼時候要登錄、
登錄到什麼時候、要不要每月重登、名額多少**，發布成網站與可訂閱的行事曆（ICS）。

網站：GitHub Pages（`web/`，Astro）。行事曆：`web/public/calendar/registration.ics`。

## 架構（v2，2026-11 上線）

```
擷取器（src/radar/banks/*）      只產 Stub：標題、網址、期間取自官方列表／API 的結構化欄位
   ↓ 明細頁文字
合併（pipeline.group_docs）       條款文字相同的多張卡片＝一筆活動，其餘卡名留作別名
   ↓ 含「登錄／登記／報名」的頁才送 LLM
LLM 定位（extract/locate.py）     claude -p --json-schema：回傳逐字引句＋結構化數值，不做換算
   ↓ 內容雜湊快取（state/extractions/），內容沒變就不再呼叫
程式換算與驗證（extract/schedule.py）
                                 引句必須逐字在原文、數字必須出現在引句；年份錨定活動期間；
                                 依行政院辦公日曆順延；每月規則展開；列舉日期與規則對帳
   ↓
沿用與防護（pipeline.run）        來源整家失敗、或局部讀取失敗（某分類 403、明細逾時）時，
                                 上一輪仍有效的活動標 stale 沿用，不讓它從網站消失
   ↓ state/activities/<bank>.json（唯一正本，進版控）
輸出（emit.py）                  web/public/data/*.json、registration.ics
```

設計原則：

- **只放事實、不放推導**：時點由程式依原文換算，LLM 只負責「指出原文哪一句」。
- **抓不到就留空**：截止未公告就顯示未公告；讀不到條款就標「未確認」，不猜。
- **每個時點都附原文引句**，網站可展開核對；無法驗證的規則進「需人工確認」。

## 排程與維運

| 項目 | 說明 |
|---|---|
| 排程 | launchd `com.garychen.tw-card-radar`（`ops/com.garychen.tw-card-radar.plist`），每日 08:20 |
| 入口 | `ops/run_local.sh`：pull → `radar run` → pytest → `radar gate` → 建站 → commit → push |
| 部署 | push 後 GitHub Actions `deploy.yml` 建置 Pages；`verify.yml` 跑測試 |
| 執行紀錄 | `~/Library/Logs/tw-card-radar/run-YYYYMMDD.log` |
| LLM 用量 | 每輪寫入 `state/run.json` 的 `llm_usage` 與本機 `var/usage.jsonl`；`radar usage --days 30` |
| 通知 | macOS 通知中心（標題「刷卡登錄雷達」） |

`run_local.sh` 的保護：

- 執行期間以 `caffeinate` 防止 Mac 睡眠；整輪上限 45 分鐘（`RADAR_MAX_SECONDS`），逾時終止整棵子程序樹。
- 任何一步失敗都不 push，並還原 `state/`、`web/public/` 的未發布變更（保留 LLM 快取）、通知失敗行號；
  開頭也會清掉被強制中止留下的殘留與中斷的 rebase，避免之後每輪卡死。
- push 被拒時 rebase 重試一次；沒有新資料也會 push，補送上一輪沒推出去的 commit。

退出碼：`0` 已發布或無變化；`4` 來源大量失敗；`5` 發布前閘門擋下；`6` claude CLI 登入失效；`143` 逾時；其他＝程式或環境錯誤。

### 發布前閘門（`src/radar/gate.py`）

| 條件 | 動作 |
|---|---|
| golden 精確率 < 0.90 | **擋下**，網站維持上一版 |
| 精確率 < 0.98、召回 < 0.95、golden 頁找不到 | 通知（照常發布） |
| golden 可評分時點 < 30 個 | 通知：golden 窗口快過期，閘門快失效 |
| 上一版仍在期間內的活動消失 ≥ 10 筆或 ≥ 8% | 通知：擷取器可能壞了卻沒報錯 |
| 有來源沿用上一輪資料 | 通知 |

警告不擋發布：官方頁改版也會觸發，擋下會讓網站停更，交由人判斷。

## golden 集

`golden/golden.yaml`：47 頁、約 68 個時點，涵蓋 `golden/window.json` 的兩個月窗口。
每雙月 15 日由 Claude 例行任務 `tw-card-radar-golden-renewal` 依 `golden/RENEWAL.md` 換下一批。

**限制（請先讀）**：golden 由 Claude 子代理對照官方原文建立，評分分歧時也由 Claude 裁定，
提示詞也曾依 golden 調整，**尚未經人工抽查**（commit `5aeac381a`「待人工抽查」）。
因此 golden 的 100% 比較接近「訓練集成績」，不是獨立驗證。改善方式見下方「已知缺口」。

## 指令

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests && .venv/bin/mypy src

.venv/bin/radar run [--banks a,b] [--model sonnet] [--llm-budget 200] [--reuse-docs]
.venv/bin/radar emit                 # 只從 state 重產網站資料
.venv/bin/radar probe <bank>         # 單一銀行擷取體檢
.venv/bin/radar score                # golden 評分
.venv/bin/radar gate --previous HEAD # 發布前閘門
.venv/bin/radar evaluate --model X   # 只對 golden 頁以指定模型抽取並評分（會呼叫 LLM）
.venv/bin/radar usage --days 30      # LLM token 用量
```

抽取模型預設 Sonnet（`RADAR_LLM_MODEL`）。11-02 在 golden 上比較：Sonnet 與 Opus 同分、
費用約一半；Haiku 精確率 64%，不採用。

## 已知缺口與取捨

- **中信 `mkt.ctbcbank.com` 主題活動頁**（約 11 頁）回 JS 驗證（HTTP 202），不解驗證，這些活動未收錄；
  來源狀態顯示「正常（有缺頁）」。
- **元大**以一般瀏覽器 UA 讀取（2026-11-05 Gary 核准，只限元大）；其餘銀行用專案 UA。
- **與 v2 提案不同的地方**（未在上線時明講，於此補記）：
  - 提案原本保留 v1 regex 解析器做交叉比對，v2 已刪除，目前沒有確定性的交叉檢查。
  - 提案原本要與 Codex 版並行兩週再切換，實際於 11-05 直接切換。
  - 驗收標準原為「資料更新間隔 ≤ 12 小時」，11-06 為省 token 改成每日一次。
- **golden 獨立性**：見上方「golden 集」。建議每批抽 5～10 筆人工核對官方頁。
- **只有精確到分鐘的時點進 ICS**；只到「日」的區間只在網站顯示。

## 安全邊界

只讀取公開官方資訊。不登入銀行帳戶、不代使用者登錄活動、不解 CAPTCHA 或 JS 驗證、
不收集卡號或個人資料。「我的卡片」與「已登錄」狀態只存在瀏覽器 localStorage。
實際名額、資格與回饋一律以銀行官方公告為準。
