"""Choose the declared source transport for production and diagnostic scripts."""

from .browser import BrowserFetcher
from .spec import SourceSpec
from .transport import DEFAULT_TIMEOUT, Fetcher, HttpCache


def source_fetcher(spec: SourceSpec, *, cache: HttpCache | None = None) -> Fetcher:
    backend = BrowserFetcher if spec.transport == "browser" else Fetcher
    return backend(
        spec.domains, cache=cache, user_agent=spec.user_agent or None,
        timeout=spec.timeout_seconds or DEFAULT_TIMEOUT,
    )
