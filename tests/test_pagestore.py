from __future__ import annotations

from pathlib import Path

from radar.pagestore import PageStore

URL = "https://www.example.com/promo/1"


def test_roundtrip(tmp_path: Path) -> None:
    store = PageStore(tmp_path)
    assert store.get(URL) is None
    assert store.has(URL) is False
    assert store.put(URL, "<html>內容</html>") is True
    assert store.get(URL) == "<html>內容</html>"
    assert store.has(URL) is True


def test_no_store_is_respected(tmp_path: Path) -> None:
    """主機明確要求 no-store 時不保存 —— 實測中信、元大都送這個指示。"""
    store = PageStore(tmp_path)
    assert store.put(URL, "<html>x</html>", cache_control="no-store, max-age=0") is False
    assert store.get(URL) is None


def test_other_cache_directives_are_allowed(tmp_path: Path) -> None:
    store = PageStore(tmp_path)
    assert store.put(URL, "<html>x</html>", cache_control="max-age=300") is True
    assert store.get(URL) is not None


def test_urls_are_isolated(tmp_path: Path) -> None:
    store = PageStore(tmp_path)
    store.put(URL, "A")
    store.put("https://www.example.com/promo/2", "B")
    assert store.get(URL) == "A"
    assert store.get("https://www.example.com/promo/2") == "B"


def test_overwrite_replaces_content(tmp_path: Path) -> None:
    store = PageStore(tmp_path)
    store.put(URL, "舊版")
    store.put(URL, "新版")
    assert store.get(URL) == "新版"


def test_missing_directory_is_created(tmp_path: Path) -> None:
    store = PageStore(tmp_path / "deep" / "nested")
    assert store.put(URL, "x") is True
    assert store.get(URL) == "x"
