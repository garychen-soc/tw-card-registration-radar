#!/usr/bin/env python3
"""診斷用工具：對某個來源的入口頁試一組 User-Agent 與標頭組合，印出各自的結果。

**為什麼需要它。** 本專案有三次「本機通、CI 被擋」或反過來的情形（元大在 CI 通
但住宅 IP 被 403、第一銀行的舊註記說 CI 會被拒但其實不會、陽信在 CI 被擋而本機
兩種 UA 都通）。這類問題本機重現不了，而每個假設各開一次 Scrape 執行太貴 ——
一次執行只能驗一個假設。這支程式把整個矩陣壓進**一次** CI 執行。

唯讀：只對 spec 宣告的 entry_url 發 GET，不寫任何檔案，不改任何設定。
請求量刻意很小（組合數 × 1 次），且沿用專案的 per-host 節流。

    python scripts/probe_source.py sunny
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from radar.spec import load_spec  # noqa: E402
from radar.transport import DEFAULT_HEADERS, USER_AGENT, is_allowed, tls_context  # noqa: E402

# 只列真實瀏覽器與本專案自己的 UA。刻意不冒充搜尋引擎爬蟲 —— 那是欺騙網站
# 對 Googlebot 的特殊待遇，不是「讓正當的讀取被正確識別」。
AGENTS: tuple[tuple[str, str], ...] = (
    ("專案預設（含識別後綴）", USER_AGENT),
    (
        "Chrome 126 / macOS",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    ),
    (
        "Chrome 141 / Windows",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    ),
    (
        "Safari 17 / macOS",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    ),
    (
        "Firefox 130 / Windows",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    ),
    (
        "Chrome 141 / Android",
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36",
    ),
)

# 真實 Chrome 一定會送的那組 fetch metadata 與 client hints。少了它們是很常見的
# 機器人訊號 —— 「UA 說是 Chrome 但沒有 sec-* 標頭」比單看 UA 更容易被判定。
CHROME_EXTRA = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="141", "Not?A_Brand";v="24", "Google Chrome";v="141"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec_id")
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--pause", type=float, default=2.0, help="每次請求之間的間隔秒數")
    args = parser.parse_args()

    spec = load_spec(ROOT / "sources" / f"{args.spec_id}.toml")
    url = spec.listing.data_url or spec.listing.entry_url
    if not is_allowed(url, [d.lower() for d in spec.domains]):
        print(f"入口頁未通過白名單，不探測：{url}")
        return 1

    print(f"來源 {spec.id}（{spec.bank_name}）")
    print(f"目標 {url}\n")
    print(f"{'UA':26s} {'額外標頭':10s} {'結果':>28s}")
    print("─" * 70)

    for label, agent in AGENTS:
        for extra_label, extra in (("無", {}), ("Chrome 全套", CHROME_EXTRA)):
            headers = {**DEFAULT_HEADERS, **extra, "User-Agent": agent}
            time.sleep(args.pause)
            try:
                with httpx.Client(
                    follow_redirects=True, timeout=args.timeout, verify=tls_context()
                ) as client:
                    response = client.get(url, headers=headers)
                outcome = f"HTTP {response.status_code}  {len(response.text):,} bytes"
            except httpx.HTTPError as exc:
                outcome = f"{type(exc).__name__}: {str(exc)[:40]}"
            print(f"{label:26s} {extra_label:10s} {outcome:>28s}")

    print("\n判讀：若所有組合都是同一種失敗 → 封鎖看的是 IP，換 UA 無效。")
    print("     若某些組合成功 → 把成功的那組寫進 spec 的 user_agent。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
