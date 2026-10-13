# ADR 0004：華南與陽信使用宣告式原生瀏覽器抓取

## 決策

華南與陽信設定 `transport = "browser"`，由標準有視窗 Chromium 讀取公開官方
HTML，並交回原有 listing、runner、解析、去重與發布管線。其餘來源維持 HTTP。
陽信在 GitHub 受管 Linux runner 使用 Xvfb；華南由受管 macOS 抓取。macOS
只回傳原始 Campaign 快照，Linux 在同一次 workflow 下載、驗證並集中去重、
核對發布防護與提交資料，不需要使用者自架常駐機器。

不以「UA 矩陣全失敗」推定 IP 封鎖。2026-10-13 實測陽信的普通 HTTP、Linux
及 macOS 路徑均回 Cloudflare 403，無視窗 Chromium 也未取得活動卡片；標準
有視窗 Chromium 在 GitHub 受管 Linux 成功讀到 HTTP 200 與 39 張卡片。
正常瀏覽器可讀的證據推翻了 ADR 0002 中「必須住宅 IP」的舊推論。

華南使用保留 `1dmy` 與 `urile` 內容參數的短入口，與固定 portal state 入口的
信用卡面板 43 個連結逐項相同。單獨 HTTP 執行可取得 33 筆活動，但兩來源合併
執行時又整個入口逾時；Linux 瀏覽器仍有不穩定逾時，受管 macOS 瀏覽器完整
取得 11 個活動頁、33 筆方案，除 observed_at 外與本機 HTTP 基準逐欄相同。
因此選用已驗證的 macOS 抓取位置，保留原有 timeout 與失敗回報。

## 來源範圍與資料品質

華南仍只讀「信用卡」面板的本行活動明細，排除其他業務、外站與導覽分類頁。
陽信改用 `html_list`：39 張卡片中，29 個是逐活動官方明細；卡組織、App、公益、
紅利兌換與其他導覽入口不列為活動。卡片標題與期間可在明細文字缺席時提供
官方已公告的資訊，不把整個索引頁當成唯一活動。

HTTP／browser 比對以原始伺服器文件為準。直接序列化瀏覽器的渲染後 DOM
曾使華南由 33 筆變成 14 筆，因此不得作為解析輸入。

目前 browser 只負責正常開啟官方網址，取得最終成功文件的原始 response body，
依伺服器 charset 解碼後交給既有解析器。DOM 只用於識別尚未解除的驗證頁，
不作為活動解析輸入。陽信兩種 transport 都取得 29 個活動頁、35 筆方案，
ID、標題、期間、登錄視窗、時序、名額與門檻一致。13 個圖片型頁面的 HTML
文字不同，browser 使用官方清單的標題與期間備援，留下 68 個原文、文字雜湊、
信心、證據及標題衍生欄位差異：新戶資格與兩筆標題公告的回饋比例從官方
標題讀取。這不是逐欄相同；圖片中尚未讀取的登錄時間仍不補值。

## 保留的防護

- 初始 URL 仍須為官方白名單 HTTPS，拒絕裸 IP。
- CDP `Fetch.requestPaused` 在文件請求送出前檢查每個轉址，非官方或不安全文件
  直接中止；保留轉址預算與 8 MB 回應限制。
- 每個來源有自己的臨時 browser context，不共用或保存登入身分；不登入、不解
  CAPTCHA、不自訂瀏覽器識別。未解除驗證、401/403/429 以 blocked 回報。
- 暫時逾時與 5xx 沿用重試／節流；瀏覽器快取停用，不宣稱使用 304 舊資料。
- 跨 runner 快照必須帶時區、12 小時內且不得超過 5 分鐘未來時間；抓取位置、
  健康紀錄的來源清單、活動 ID 唯一性及實際活動／方案筆數均須符合指定來源。
  驗證失敗以 failed 與警示回報，不讓該來源消失或把舊快照當成新資料。
- 原始 0/2/4 退出碼、unexpected-exit 檢查、only 強制 dry-run、覆蓋率門檻、
  去重與資料提交條件均保留。新增恢復來源的涵蓋率檢查：上一版抓取量為 0、
  但有已發布的沿用資料時，恢復來源仍須檢查是否大幅少於已知涵蓋量；
  仍為 failed/blocked 的來源不會因此被當成新的流失。
  Xvfb 的子程序退出碼仍由 `PIPESTATUS[0]` 捕捉。

## 驗證

本機單元測試、ruff 與 21 個來源檔案的 mypy 通過。測試涵蓋來源 selector、
期間與登錄、拒絕存取不造活動、browser 初始 URL／轉址／大小限制、驗證頁攔阻、
暫時失敗重試、跨 runner 快照驗證與發布防護保留檔案。

單一來源成功不是全站完成的證據。兩來源完整 dry-run、合併後 main Scrape 的
來源健康、防護、提交及正式網站讀回須分別核對，不能用 workflow success 代替。

參考：[Playwright](https://playwright.dev/python/docs/intro)、
[Chrome CDP Fetch](https://chromedevtools.github.io/devtools-protocol/tot/Fetch/)。
