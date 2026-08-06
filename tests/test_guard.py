"""發布防護測試。重點是補上前身的逐來源破口。"""

from __future__ import annotations

from typing import Any

from radar.guard import assess, describe
from radar.models import SourceHealth


def _health(**counts: int) -> list[SourceHealth]:
    return [
        SourceHealth(
            bank_id=bank_id,
            bank_name=bank_id.upper(),
            requested_url=f"https://www.{bank_id}.example/",
            status="complete" if offers else "failed",
            campaign_count=offers,
            offer_count=offers,
        )
        for bank_id, offers in counts.items()
    ]


def _one(bank_id: str, offers: int, status: str = "complete") -> SourceHealth:
    """單一來源的健康度，可指定狀態 —— failed／blocked 的筆數本來就是 0。"""
    return SourceHealth(
        bank_id=bank_id,
        bank_name=bank_id.upper(),
        requested_url=f"https://www.{bank_id}.example/",
        status=status,  # type: ignore[arg-type]
        campaign_count=offers,
        offer_count=offers,
    )


def _index(**counts: int) -> dict[str, Any]:
    return {
        "counts": {"offers": sum(counts.values())},
        "sources": [
            {"bank_id": bank_id, "offer_count": offers} for bank_id, offers in counts.items()
        ],
    }


def test_healthy_run_passes() -> None:
    health = _health(a=100, b=200, c=50)
    guard = assess(health=health, current_offers=350, previous_index=_index(a=98, b=205, c=48))
    assert guard.status == "passed"
    assert guard.reason_codes == []


def test_largest_source_collapsing_is_caught_by_per_source_check() -> None:
    """前身的破口：台北富邦 212 筆（全站最大來源）整個歸零時，
    全站跌幅僅 19.8% < 50%，會靜默發布。"""
    previous = _index(fubon=212, taishin=198, ctbc=121, esun=182, others=358)
    health = _health(fubon=0, taishin=198, ctbc=121, esun=182, others=358)
    current_offers = 198 + 121 + 182 + 358

    guard = assess(health=health, current_offers=current_offers, previous_index=previous)

    assert guard.status == "blocked"
    assert "per_source_coverage_regression" in guard.reason_codes
    assert "total_coverage_regression" not in guard.reason_codes, "全站跌幅本身不足以擋下"
    assert guard.total_drop_percent < 25
    assert [item.bank_id for item in guard.regressions] == ["fubon"]
    assert guard.regressions[0].drop_percent == 100.0
    assert guard.published_snapshot_preserved is True


def test_moderate_per_source_dip_is_tolerated() -> None:
    """來源筆數本來就會小幅波動（活動到期），不該一有下降就阻擋。"""
    previous = _index(a=100, b=100)
    health = _health(a=75, b=100)
    guard = assess(health=health, current_offers=175, previous_index=previous)
    assert guard.status == "passed"


def test_small_sources_are_exempt_from_regression_check() -> None:
    """基數太小的來源（例如 4 → 1）跌幅百分比沒有意義，會製造雜訊。"""
    previous = _index(a=200, tiny=4)
    health = _health(a=200, tiny=1)
    guard = assess(health=health, current_offers=201, previous_index=previous)
    assert guard.status == "passed"
    assert guard.regressions == []


def test_systemic_failure_blocks_even_without_previous_snapshot() -> None:
    """首次執行沒有上一版可比，仍要能靠來源失敗率擋下環境問題。

    實測 2026-08-04 那次：16/17 來源全掛，但 fallback 把舊活動撈回來，
    跌幅指標是 0.0% —— 只有來源失敗率這條擋得住。
    """
    health = _health(a=0, b=0, c=0, d=0, e=0, f=100)
    guard = assess(health=health, current_offers=100, previous_index=None)
    assert guard.status == "blocked"
    assert "systemic_source_failure" in guard.reason_codes
    assert "catastrophic_source_failure" in guard.reason_codes
    assert guard.published_snapshot_preserved is False, "沒有上一版可保留"


def test_two_failures_out_of_seventeen_is_tolerated() -> None:
    """陽信與第一銀行長期被 Cloudflare 阻擋，這是已知狀態，不該擋下整次發布。"""
    counts = {f"bank{index}": 30 for index in range(15)}
    counts["sunny"] = 0
    counts["first"] = 0
    health = _health(**counts)
    guard = assess(health=health, current_offers=450, previous_index=None)
    assert guard.status == "passed"


def test_blocked_sources_count_toward_systemic_failure() -> None:
    """status=blocked（官方頁給了不可信任的位址）也是不可用，要計入。"""
    health = [
        SourceHealth(
            bank_id=f"b{index}",
            bank_name=f"B{index}",
            requested_url="https://www.example.com/",
            status="blocked" if index < 3 else "complete",
            offer_count=0 if index < 3 else 50,
        )
        for index in range(6)
    ]
    guard = assess(health=health, current_offers=150, previous_index=None)
    assert guard.source_blocked == 3
    assert "systemic_source_failure" in guard.reason_codes


def test_total_collapse_is_blocked() -> None:
    previous = _index(a=300, b=300)
    health = _health(a=300, b=0)
    guard = assess(health=health, current_offers=300, previous_index=previous)
    assert guard.status == "blocked"
    assert "total_coverage_regression" in guard.reason_codes


def test_describe_passed_and_blocked() -> None:
    passed = assess(health=_health(a=10), current_offers=10, previous_index=_index(a=10))
    assert any("發布防護通過" in line for line in describe(passed))

    blocked = assess(
        health=_health(a=0, b=0, c=0), current_offers=0, previous_index=_index(a=50, b=50, c=50)
    )
    text = "\n".join(describe(blocked))
    assert "未覆寫網站資料" in text
    assert "已保留上一版" in text


def test_a_small_tail_of_failed_sources_does_not_veto_the_rest() -> None:
    """實測 2026-08-06 的 CI 執行：華南逾時（30 筆）、陽信被 datacenter IP 封鎖
    （24 筆），合計佔全站 1,538 筆的 3.5%，卻把其餘 15 家的更新全部擋掉。

    這兩家的狀態已經有來源健康度、警示與網站「來源狀態」面板三個出口；擋下發布
    不會多提供資訊，而陽信的封鎖是持續性的，等於永久停更。
    """
    previous = _index(hncb=30, sunny=24, cathay=127, fubon=215, esun=255, rest=887)
    guard = assess(
        health=[
            _one("hncb", 0, "failed"),
            _one("sunny", 0, "blocked"),
            _one("cathay", 127),
            _one("fubon", 215),
            _one("esun", 255),
            _one("rest", 887),
        ],
        current_offers=127 + 215 + 255 + 887,
        previous_index=previous,
    )
    assert guard.status == "passed"
    assert guard.regressions == []
    assert guard.source_failed == 1
    assert guard.source_blocked == 1


def test_a_source_reporting_complete_but_collapsing_still_blocks() -> None:
    """靜默的覆蓋率流失沒有別的訊號，必須照擋 —— 這是這條檢查的本業。

    即使同一次執行裡另有來源以 failed 大聲回報，也不影響這條判斷。
    """
    previous = _index(hncb=30, cathay=127, rest=1000)
    guard = assess(
        health=[_one("hncb", 0, "failed"), _one("cathay", 3), _one("rest", 1000)],
        current_offers=1003,
        previous_index=previous,
    )
    assert guard.status == "blocked"
    assert "per_source_coverage_regression" in guard.reason_codes
    assert [r.bank_id for r in guard.regressions] == ["cathay"]


def test_a_large_failed_source_still_blocks() -> None:
    """前身 2026-08-01 的案例：台北富邦 212 筆佔全站 19.8%，抓取失敗歸零。
    使用者會少掉五分之一的活動，而網站看起來完全正常 —— 這種規模必須擋。"""
    previous = _index(fubon=212, taishin=198, ctbc=121, esun=182, others=358)
    guard = assess(
        health=[
            _one("fubon", 0, "failed"),
            _one("taishin", 198),
            _one("ctbc", 121),
            _one("esun", 182),
            _one("others", 358),
        ],
        current_offers=198 + 121 + 182 + 358,
        previous_index=previous,
    )
    assert guard.status == "blocked"
    assert [r.bank_id for r in guard.regressions] == ["fubon"]


def test_too_many_unusable_sources_still_blocks() -> None:
    """放行單獨幾家不等於放行「一次掛一片」—— systemic 那條仍然生效。"""
    guard = assess(
        health=[
            _one("a", 0, "failed"),
            _one("b", 0, "blocked"),
            _one("c", 0, "failed"),
            _one("d", 10),
        ],
        current_offers=10,
        previous_index=None,
    )
    assert guard.status == "blocked"
    assert "systemic_source_failure" in guard.reason_codes
