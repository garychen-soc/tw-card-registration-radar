"""The publishing entry point must preserve the site when coverage collapses."""

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from radar.models import SourceHealth
from radar.runner import SourceResult
from radar.spec import SourceSpec


def test_blocked_build_preserves_published_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_spec = importlib.util.spec_from_file_location(
        "build_site", Path(__file__).resolve().parents[1] / "scripts" / "build_site.py",
    )
    assert module_spec is not None and module_spec.loader is not None
    build_site = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(build_site)
    site = tmp_path / "public"
    index_path = site / "data" / "index.json"
    index_path.parent.mkdir(parents=True)
    original = json.dumps({
        "counts": {"offers": 10},
        "sources": [{"bank_id": "demo", "offer_count": 10}],
    }).encode()
    index_path.write_bytes(original)
    catalog = site / "data" / "demo.json"
    catalog.write_bytes(b'{"published": true}')
    spec = SourceSpec.model_validate({
        "id": "demo", "bank_name": "示範銀行", "domains": ["example.com"],
        "listing": {"kind": "single_page", "entry_url": "https://example.com/"},
    })
    result = SourceResult(health=SourceHealth(
        bank_id="demo", bank_name="示範銀行", requested_url="https://example.com/",
        status="complete", offer_count=0, observed_at=datetime(2026, 10, 12, tzinfo=UTC),
    ))
    monkeypatch.setattr(build_site, "ROOT", tmp_path)
    monkeypatch.setattr(build_site, "SITE", site)
    monkeypatch.setattr(build_site, "INDEX_PATH", index_path)
    report_path = tmp_path / "reports" / "latest.json"
    monkeypatch.setattr(build_site, "REPORT_PATH", report_path)
    monkeypatch.setattr(build_site, "load_specs", lambda _path: [spec])
    monkeypatch.setattr(build_site, "run_source", lambda *_args, **_kwargs: result)
    writer = Mock()
    monkeypatch.setattr(build_site, "write_site", writer)
    monkeypatch.setattr(sys, "argv", ["build_site.py", "--today", "2026-10-12"])

    assert build_site.main() == 4
    writer.assert_not_called()
    assert index_path.read_bytes() == original
    assert catalog.read_bytes() == b'{"published": true}'
    report = json.loads(report_path.read_text())
    assert report["guard"]["status"] == "blocked"
    assert report["guard"]["published_snapshot_preserved"] is True
