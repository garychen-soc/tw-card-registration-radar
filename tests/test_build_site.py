"""The publishing entry point must preserve the site when coverage collapses."""

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from radar.models import Campaign, Offer, SourceHealth
from radar.runner import SourceResult
from radar.spec import SourceSpec


def _module():
    module_spec = importlib.util.spec_from_file_location(
        "build_site", Path(__file__).resolve().parents[1] / "scripts" / "build_site.py",
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 10, 13, tzinfo=UTC)


def _delegated() -> SourceSpec:
    return SourceSpec.model_validate({
        "id": "demo", "bank_name": "示範銀行", "domains": ["example.com"],
        "runner": "cloud-macos",
        "listing": {"kind": "single_page", "entry_url": "https://example.com/"},
    })


def _snapshot() -> dict:
    campaign = Campaign(
        id="demo-one", bank_id="demo", bank_name="示範銀行", title="活動一",
        source_url="https://example.com/one", observed_at=NOW,
        offers=[Offer(id="demo-one-1", title="方案一")],
    )
    health = SourceHealth(
        bank_id="demo", bank_name="示範銀行", requested_url="https://example.com/",
        status="complete", campaign_count=1, offer_count=1, observed_at=NOW,
    )
    return {
        "generated_at": NOW.isoformat(), "runner": "cloud-macos",
        "campaigns": [campaign.model_dump(mode="json")],
        "health": [health.model_dump(mode="json")], "alerts": [],
    }


def test_fresh_delegated_snapshot_preserves_campaigns(tmp_path: Path) -> None:
    payload = _snapshot()
    path = tmp_path / "macos.json"
    path.write_text(json.dumps(payload))
    campaigns, health, alerts = _module()._read_snapshot(path, [_delegated()], NOW)
    assert [item.model_dump(mode="json") for item in campaigns] == payload["campaigns"]
    assert [item.model_dump(mode="json") for item in health] == payload["health"]
    assert not alerts


@pytest.mark.parametrize("case", [
    "missing_file", "bad_json", "missing_time", "naive_time", "stale_time", "future_time",
    "wrong_runner", "missing_health", "duplicate_health", "foreign_health",
    "foreign_campaign", "foreign_alert", "duplicate_campaign", "wrong_campaign_count",
    "wrong_offer_count", "invalid_model",
])
def test_untrusted_snapshot_records_failed_source(tmp_path: Path, case: str) -> None:
    payload = _snapshot()
    path = tmp_path / "macos.json"
    if case == "missing_time":
        del payload["generated_at"]
    elif case == "naive_time":
        payload["generated_at"] = NOW.replace(tzinfo=None).isoformat()
    elif case == "stale_time":
        payload["generated_at"] = (NOW - timedelta(hours=12, seconds=1)).isoformat()
    elif case == "future_time":
        payload["generated_at"] = (NOW + timedelta(minutes=5, seconds=1)).isoformat()
    elif case == "wrong_runner":
        payload["runner"] = "self-hosted"
    elif case == "missing_health":
        payload["health"] = []
    elif case == "duplicate_health":
        payload["health"] *= 2
    elif case == "foreign_health":
        payload["health"][0]["bank_id"] = "other"
    elif case == "foreign_campaign":
        payload["campaigns"][0]["bank_id"] = "other"
    elif case == "foreign_alert":
        payload["alerts"] = [{"type": "source_failed", "bank_id": "other", "message": "x"}]
    elif case == "duplicate_campaign":
        payload["campaigns"] *= 2
    elif case == "wrong_campaign_count":
        payload["health"][0]["campaign_count"] = 2
    elif case == "wrong_offer_count":
        payload["health"][0]["offer_count"] = 2
    elif case == "invalid_model":
        payload["health"][0]["status"] = "invented"
    if case != "missing_file":
        path.write_text("{" if case == "bad_json" else json.dumps(payload))

    campaigns, health, alerts = _module()._read_snapshot(
        path, [_delegated()], NOW, runner_label="受管 macOS runner",
    )
    assert not campaigns
    assert [(item.bank_id, item.status) for item in health] == [("demo", "failed")]
    assert "受管 macOS runner" in health[0].message
    assert [(item.bank_id, item.type) for item in alerts] == [("demo", "source_failed")]


def test_upstream_failed_health_is_not_relabelled_complete(tmp_path: Path) -> None:
    payload = _snapshot()
    payload["campaigns"] = []
    payload["health"][0].update(status="failed", campaign_count=0, offer_count=0)
    path = tmp_path / "macos.json"
    path.write_text(json.dumps(payload))
    campaigns, health, _ = _module()._read_snapshot(path, [_delegated()], NOW)
    assert not campaigns
    assert health[0].status == "failed"


def test_cloud_can_validate_only_delegated_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_site = _module()
    payload = _snapshot()
    payload["generated_at"] = datetime.now(UTC).isoformat()
    path = tmp_path / "macos.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(build_site, "ROOT", tmp_path)
    monkeypatch.setattr(build_site, "SITE", tmp_path / "public")
    monkeypatch.setattr(build_site, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(build_site, "load_specs", lambda _: [_delegated()])
    fetcher = Mock()
    monkeypatch.setattr(build_site, "source_fetcher", fetcher)
    monkeypatch.setattr(sys, "argv", [
        "build_site.py", "--runner", "cloud", "--only", "demo", "--dry-run",
        "--macos-snapshot-in", str(path),
    ])
    assert build_site.main() == 0
    fetcher.assert_not_called()


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
