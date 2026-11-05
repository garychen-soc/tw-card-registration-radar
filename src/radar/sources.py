"""建立擷取器與對應的抓取器，並提供 ``probe``（單一銀行的快速體檢）。"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from . import banks
from .banks.base import Adapter
from .transport import Fetcher


@contextmanager
def open_adapter(bank_id: str) -> Iterator[Adapter]:
    cls = banks.load(bank_id)
    fetcher: Fetcher
    if cls.transport == "browser":
        from .browser import BrowserFetcher

        fetcher = BrowserFetcher(
            cls.domains, min_host_interval=cls.min_host_interval, user_agent=cls.user_agent
        )
    else:
        fetcher = Fetcher(
            cls.domains, min_host_interval=cls.min_host_interval, user_agent=cls.user_agent
        )
    with fetcher:
        yield cls(fetcher)


def probe(bank_id: str, *, details: int = 2) -> dict[str, object]:
    """列表筆數、標題樣本、前幾筆明細的條款長度。開發擷取器時用。"""
    with open_adapter(bank_id) as adapter:
        stubs = adapter.discover()
        keys = [s.key for s in stubs]
        sample = []
        for stub in stubs[:details]:
            terms = adapter.terms(stub) if stub.detail_text is None else None
            text = stub.detail_text if terms is None else terms.text
            sample.append(
                {
                    "title": stub.title,
                    "url": stub.url,
                    "period": [str(stub.period_start), str(stub.period_end)],
                    "terms_chars": len(text or ""),
                    "terms_head": (text or "")[:160],
                    "detail_title": terms.title if terms else "",
                }
            )
        return {
            "bank": bank_id,
            "stubs": len(stubs),
            "duplicate_keys": len(keys) - len(set(keys)),
            "skipped": adapter.skipped[:10],
            "no_period": sum(1 for s in stubs if s.period_end is None),
            "titles": [s.title for s in stubs[:40]],
            "sample": sample,
        }


if __name__ == "__main__":
    print(json.dumps(probe(sys.argv[1]), ensure_ascii=False, indent=1))
