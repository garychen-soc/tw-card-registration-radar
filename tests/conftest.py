from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from radar.transport import Response, TransportError


@dataclass
class FakeFetcher:
    """離線測試用的抓取器。

    ``pages`` 對應 URL → 內容；``failures`` 對應 URL → 要拋出的例外。
    ``requested`` 記錄實際發出的請求，用來驗證快取真的省下了讀取。
    """

    pages: dict[str, str] = field(default_factory=dict)
    failures: dict[str, TransportError] = field(default_factory=dict)
    requested: list[str] = field(default_factory=list)
    posted: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def get(
        self,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        conditional: bool = True,
    ) -> Response:
        self.requested.append(url)
        if data is not None:
            self.posted.append((url, data))
        if url in self.failures:
            raise self.failures[url]
        body = self.pages.get(url)
        if body is None:
            raise TransportError(f"FakeFetcher 未設定此 URL：{url}")
        return Response(
            requested_url=url,
            final_url=url,
            status_code=200,
            text=body,
            content_type="text/html",
            content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )
