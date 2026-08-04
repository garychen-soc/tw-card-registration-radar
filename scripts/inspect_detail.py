#!/usr/bin/env python3
"""開發用工具：把單一官方明細頁跑完整條解析鏈並印出結果。

用途是在寫或調整 spec 時驗證解析行為，也用來為 golden corpus 挑選節錄。
唯讀，不寫任何檔案。解析邏輯完全來自 ``radar.runner.build_offers``，
不在這裡另寫一份 —— 否則腳本會與正式管線分歧。

    python scripts/inspect_detail.py esun "https://www.esunbank.com/zh-tw/..."
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from radar.htmltext import to_text  # noqa: E402
from radar.report import describe_offer  # noqa: E402
from radar.runner import build_offers  # noqa: E402
from radar.segment import sections  # noqa: E402
from radar.spec import load_spec  # noqa: E402
from radar.transport import Fetcher  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec_id")
    parser.add_argument("url")
    parser.add_argument("--today", default=date.today().isoformat())
    args = parser.parse_args()

    spec = load_spec(ROOT / "sources" / f"{args.spec_id}.toml")
    today = date.fromisoformat(args.today)

    with Fetcher(spec.domains) as fetcher:
        response = fetcher.get(args.url, conditional=False)

    text = to_text(response.text)
    print(f"來源 {spec.id}（{spec.bank_name}）  HTTP {response.status_code}")
    print(f"最終 URL {response.final_url}")
    print(f"純文字 {len(text):,} 字  |  段落 {sorted(sections(text))}")
    print()

    offers = build_offers(
        spec,
        url=response.final_url,
        html=response.text,
        text=text,
        today=today,
    )
    print(f"切出 {len(offers)} 個子活動")
    for offer in offers:
        print("─" * 78)
        for line in describe_offer(offer):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
