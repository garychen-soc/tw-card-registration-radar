# golden 換新流程

golden 是「官方原文人工核對的登錄時點」，用來量測抽取管線的精確率與召回率；
`ops/run_local.sh` 每輪以它把關發布。每批只涵蓋兩個月（`golden/window.json`），
所以要**在窗口結束前**換下一批。排程：每逢雙數月 15 日由 Claude 例行任務
`tw-card-radar-golden-renewal` 照本文件執行（12/15 換 1–2 月、2/15 換 3–4 月……）。

## 鐵則

1. **獨立性**：判定時點時只讀官方原文（`var/docs/<bank>.json` 的 `text`，或重新抓官方頁）。
   不得讀 `state/`、`web/public/data/`、`state/extractions/` 或任何本管線／v1／Codex 的輸出——
   golden 抄自受測對象就失去意義。
2. **只動 golden**：只改 `golden/golden.yaml`、`golden/window.json`、`var/golden_src/`。
   評分發現抽取錯誤時**不修程式**，列入報告交給 Gary 另開 session。
3. **逐字引用**：每個 quote 必須逐字出現在 `var/golden_src/<id>.txt`。
4. **不碰個資與登入**：只讀公開頁面。

## 步驟

0. 前置
   - `cd ~/Projects/tw-card-registration-radar`
   - 確認沒有更新程序在跑：`pgrep -f "radar run"` 無結果、`state/.run.lock` 不存在
     （有就等 5 分鐘再查，最多 30 分鐘；仍在跑就中止並回報）。
   - `git switch main && git pull --rebase`
   - `var/docs/*.json` 的修改時間須在 36 小時內（每日 08:20 的排程會更新）；否則中止並回報。
1. 決定新窗口：本月 15 日執行 → 下個月 1 日到下下個月最後一天。
2. 挑候選（目標 45–50 筆）
   - 從 `var/docs/<bank>.json` 的 `docs[].text` 找出提到「登錄」且新窗口內有登錄時點的活動。
   - 18 家銀行每家至少 1 筆（該家真的沒有需登錄活動就記在報告）。
   - 涵蓋難度標籤（沿用 golden.yaml 既有的 `difficulty` 詞彙）：至少各 2 筆
     `monthly`、`holiday_shift` 或 `holiday_no_shift`、`multi_sub`、`negative`（不需登錄）、
     `unspecified_time`、`enumerated_dates`、`month_end`、`quota`、`window_crosses_range`、
     `example_sentence`。
   - 上一批仍在新窗口內有時點的活動可保留，但必須用今天的原文重新核對並更新 `verified_at`。
3. 每筆寫入
   - 原文全文存 `var/golden_src/<id>.txt`（來自 `var/docs` 的 `text`，不改字）。
   - 依 `golden.yaml` 檔頭的記法約定寫條目（時間到分鐘、`+08:00`、假日順延只在條款明寫時套用，
     假日表 `data/holidays/<年>.csv`）。`verified_at` 填今天。
4. 換窗口：改 `golden/window.json` 的 `start`／`end`。舊窗口的條目全部移除或已重新核對。
5. 驗證：`.venv/bin/python golden/check.py` 必須 `RESULT: PASS`。
6. 評分：`.venv/bin/radar score`。對每個 `wrong`／`missed`：
   - 回原文逐字重讀。golden 錯 → 改 golden，`note` 寫更正理由。
   - 抽取錯 → golden 不動，記入報告（活動、golden 時點、抽取時點、原文 quote）。
7. `.venv/bin/python -m pytest -q` 全過後提交並推送：
   `git add golden && git commit -m "golden: 換新 <start>~<end>（N 筆）" && git push`
   推送前再確認一次步驟 0 的鎖；被拒就 `git pull --rebase` 後重推。
8. 回報：筆數、各銀行分布、標籤覆蓋、精確率／召回率、golden 更正清單、抽取錯誤清單，
   並發 `osascript -e 'display notification "…" with title "刷卡登錄雷達"'`。
