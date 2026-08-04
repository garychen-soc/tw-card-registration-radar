"""單一 HTTP 客戶端。

前身有四套近乎相同的抓取實作（``FetchSession``、``PersistentHTTPSession``、
``SystemCurlSession``、模組級 ``fetch_text`` 加 ``_fetch_with_system_curl``），
redirect 迴圈、cookie 處理與白名單檢查各寫了 2–4 次。這裡只有一套。

三個關鍵行為，都來自實測到的問題：

**白名單貫穿 redirect 每一跳，且拒絕裸 IP。**
彰化銀行官方頁曾吐出 ``http://10.100.6.38/frontend/bonusDetail.jsp?id=3450``
（內網 IP + 明文 http）。拒絕是正確的，白名單不放寬。

**非法連結是「跳過這一筆」，不是「整次執行失敗」。**
前身在該筆連結被拒時例外往上冒，導致整次更新 exit 1，其餘 16 家一起失敗。
這裡用 ``BlockedURL`` 這個獨立的例外型別表達「這一筆不可用，但流程應該繼續」，
由呼叫端逐筆捕捉並記錄到來源健康。

**條件式 GET 與 per-host 節流。**
帶 ETag／Last-Modified 重訪未變更的頁面只花一次 304，並且對同一主機的
請求間隔設下限，避免對銀行網站造成不必要的壓力。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 "
    "TwCardRegistrationRadar/0.1 (+https://github.com/garychen-soc/tw-card-registration-radar)"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.7",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
}
MAX_REDIRECTS = 10
MAX_BYTES = 8_000_000
MIN_HOST_INTERVAL = 0.7


class TransportError(Exception):
    """抓取失敗的基底。"""


class BlockedURL(TransportError):
    """連結未通過安全檢查。

    刻意與 ``FetchFailed`` 分開：這代表「官方頁自身給了不該跟隨的連結」，
    處置是跳過該筆並記錄警示，不是把整個來源判定為失效。
    """


class FetchFailed(TransportError):
    """網路或 HTTP 層失敗（逾時、5xx、連線中斷）。"""


@dataclass(frozen=True)
class Response:
    requested_url: str
    final_url: str
    status_code: int
    text: str
    content_type: str
    content_hash: str
    cache_control: str = ""
    not_modified: bool = False

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as exc:
            raise FetchFailed(f"官方端點未回傳合法 JSON：{self.final_url}") from exc


def is_allowed(url: str, allowed_domains: list[str]) -> bool:
    """https + 網域在白名單內 + 不是裸 IP。"""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    try:
        # 裸 IP 一律拒絕。銀行不會用 IP 提供正式服務，出現就是官方頁的部署異常。
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains)


@dataclass
class HttpCache:
    """ETag / Last-Modified 的持久化存放，供條件式 GET 使用。"""

    path: Path | None = None
    entries: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> HttpCache:
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
        else:
            raw = {}
        entries = {k: v for k, v in raw.items() if isinstance(v, dict)}
        return cls(path=path, entries=entries)

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.entries, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def validators(self, url: str) -> dict[str, str]:
        entry = self.entries.get(url, {})
        headers: dict[str, str] = {}
        if etag := entry.get("etag"):
            headers["If-None-Match"] = etag
        if modified := entry.get("last_modified"):
            headers["If-Modified-Since"] = modified
        return headers

    def remember(self, url: str, response: httpx.Response, content_hash: str) -> None:
        entry: dict[str, str] = {"content_hash": content_hash}
        if etag := response.headers.get("ETag"):
            entry["etag"] = etag
        if modified := response.headers.get("Last-Modified"):
            entry["last_modified"] = modified
        self.entries[url] = entry

    def content_hash(self, url: str) -> str:
        return self.entries.get(url, {}).get("content_hash", "")

    def has_validators(self, url: str) -> bool:
        """該 URL 上次是否回傳了 ETag／Last-Modified。

        沒有驗證標頭就永遠不可能收到 304，快取頁面內容純屬浪費 ——
        實測六家來源只有兩家提供，其餘送 no-store／private。
        """
        entry = self.entries.get(url, {})
        return bool(entry.get("etag") or entry.get("last_modified"))


class Fetcher:
    """對單一來源（銀行）的抓取器。網域白名單綁在實例上。"""

    def __init__(
        self,
        allowed_domains: list[str],
        *,
        cache: HttpCache | None = None,
        timeout: float = 25.0,
        min_host_interval: float = MIN_HOST_INTERVAL,
        user_agent: str | None = None,
    ) -> None:
        self.domains = [item.lower().rstrip(".") for item in allowed_domains]
        self.cache = cache or HttpCache()
        self.min_host_interval = min_host_interval
        headers = dict(DEFAULT_HEADERS)
        if user_agent:
            headers["User-Agent"] = user_agent
        self._client = httpx.Client(
            follow_redirects=False,
            timeout=timeout,
            headers=headers,
        )
        self._last_request: dict[str, float] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _throttle(self, host: str) -> None:
        previous = self._last_request.get(host)
        now = time.monotonic()
        if previous is not None:
            wait = self.min_host_interval - (now - previous)
            if wait > 0:
                time.sleep(wait)
        self._last_request[host] = time.monotonic()

    def get(
        self,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        conditional: bool = True,
        max_bytes: int = MAX_BYTES,
    ) -> Response:
        """抓取單一 URL。

        Raises:
            BlockedURL: 起始或 redirect 途中的任一 URL 未通過安全檢查。
            FetchFailed: 網路、HTTP 或大小限制問題。
        """
        if not is_allowed(url, self.domains):
            raise BlockedURL(f"URL 未通過安全檢查（需為官方網域的 https，且非裸 IP）：{url}")

        request_headers = dict(headers or {})
        if conditional and data is None:
            request_headers.update(self.cache.validators(url))

        current = url
        method = "POST" if data is not None else "GET"
        body = data

        for _ in range(MAX_REDIRECTS + 1):
            host = urllib.parse.urlsplit(current).hostname or ""
            self._throttle(host)
            try:
                response = self._client.request(
                    method, current, data=body, headers=request_headers
                )
            except httpx.HTTPError as exc:
                raise FetchFailed(f"抓取失敗 {current}：{exc}") from exc

            if response.status_code == 304:
                return Response(
                    requested_url=url,
                    final_url=current,
                    status_code=304,
                    text="",
                    content_type=response.headers.get("content-type", "").split(";")[0],
                    content_hash=self.cache.content_hash(url),
                    cache_control=response.headers.get("cache-control", ""),
                    not_modified=True,
                )

            if 300 <= response.status_code < 400 and "location" in response.headers:
                target = urllib.parse.urljoin(current, response.headers["location"])
                if not is_allowed(target, self.domains):
                    raise BlockedURL(
                        f"官方頁導向不可信任的位址（非官方網域、非 https 或為裸 IP）：{target}"
                    )
                current = target
                if response.status_code in {301, 302, 303}:
                    method = "GET"
                    body = None
                continue

            if response.status_code >= 400:
                raise FetchFailed(f"抓取失敗 {current}：HTTP {response.status_code}")

            content = response.content
            if len(content) > max_bytes:
                raise FetchFailed(f"回應超過 {max_bytes} bytes：{current}")
            content_hash = hashlib.sha256(content).hexdigest()
            if conditional and data is None:
                self.cache.remember(url, response, content_hash)
            return Response(
                requested_url=url,
                final_url=current,
                status_code=response.status_code,
                text=response.text,
                content_type=response.headers.get("content-type", "").split(";")[0],
                content_hash=content_hash,
                cache_control=response.headers.get("cache-control", ""),
            )

        raise FetchFailed(f"redirect 次數過多：{url}")
