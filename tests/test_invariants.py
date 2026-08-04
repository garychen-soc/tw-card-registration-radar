from __future__ import annotations

from datetime import date, datetime

from radar import invariants
from radar.models import (
    Conditions,
    Offer,
    Period,
    Registration,
    RegistrationWindow,
    ThresholdTier,
    TimingContract,
)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _offer(**kwargs: object) -> Offer:
    base: dict[str, object] = {
        "id": "esun-test",
        "title": "測試活動",
        "period": Period(start=date(2026, 8, 1), end=date(2026, 8, 15), confidence=0.9),
        "registration": Registration(
            required=True,
            timing_contract=TimingContract(kind="retroactive_ok", confidence=0.7),
        ),
        "conditions": Conditions(),
    }
    base.update(kwargs)
    return Offer(**base)  # type: ignore[arg-type]


def test_window_far_outside_period_is_flagged() -> None:
    """真實案例：玉山 momo 活動至 8/15，卻掛著 10/17 的登錄時點 —— 根因是
    單頁多活動被合併，invariant 是最後一道網。"""
    offer = _offer(
        registration=Registration(
            required=True,
            windows=[
                RegistrationWindow(
                    kind="opens_at", start=_dt("2026-10-17T10:00:00+08:00"), confidence=0.8
                )
            ],
            timing_contract=TimingContract(kind="retroactive_ok", confidence=0.7),
        )
    )
    codes = invariants.check(offer)
    assert "window_outside_period" in codes
    invariants.apply(offer)
    assert offer.needs_review is True
    assert any("多個活動" in reason for reason in offer.review_reasons)


def test_registration_within_grace_is_accepted() -> None:
    """登錄截止設在活動結束後幾天供補登錄是合法的，不該誤報。"""
    offer = _offer(
        registration=Registration(
            required=True,
            windows=[
                RegistrationWindow(
                    kind="range",
                    start=_dt("2026-08-01T00:00:00+08:00"),
                    end=_dt("2026-08-25T23:59:00+08:00"),
                    confidence=0.95,
                )
            ],
            timing_contract=TimingContract(kind="retroactive_ok", confidence=0.7),
        )
    )
    assert "window_outside_period" not in invariants.check(offer)


def test_required_without_window_is_flagged() -> None:
    """全站 133 筆（55%）屬於此類，應進『需人工確認』分頁而非混在主列表。"""
    offer = _offer()
    codes = invariants.check(offer)
    assert "registration_without_window" in codes
    invariants.apply(offer)
    assert offer.needs_review is True


def test_unknown_end_is_informational_not_blocking() -> None:
    """只有開放時點、沒有截止 —— 資訊不完整但仍可行動，不該退到人工確認。"""
    offer = _offer(
        registration=Registration(
            required=True,
            windows=[
                RegistrationWindow(
                    kind="opens_at", start=_dt("2026-08-07T17:00:00+08:00"), confidence=0.8
                )
            ],
            timing_contract=TimingContract(kind="registration_closes_early", confidence=0.7),
        )
    )
    codes = invariants.check(offer)
    assert codes == ["registration_end_unknown"]
    invariants.apply(offer)
    assert offer.needs_review is False
    assert offer.review_reasons == ["抓到登錄開放時間，但截止時間未確認"]


def test_overlapping_windows_are_flagged() -> None:
    offer = _offer(
        registration=Registration(
            required=True,
            windows=[
                RegistrationWindow(
                    kind="range",
                    start=_dt("2026-08-01T00:00:00+08:00"),
                    end=_dt("2026-08-10T23:59:00+08:00"),
                    confidence=0.95,
                ),
                RegistrationWindow(
                    kind="range",
                    start=_dt("2026-08-05T00:00:00+08:00"),
                    end=_dt("2026-08-12T23:59:00+08:00"),
                    confidence=0.95,
                ),
            ],
            timing_contract=TimingContract(kind="retroactive_ok", confidence=0.7),
        )
    )
    assert "windows_overlap" in invariants.check(offer)


def test_non_monotonic_threshold_tiers_are_flagged() -> None:
    """階梯門檻解析錯位的偵測（聯邦的四階表格若欄位對錯就會出現）。"""
    offer = _offer(
        registration=Registration(
            required=False,
            timing_contract=TimingContract(kind="retroactive_ok", confidence=0.7),
        ),
        conditions=Conditions(
            threshold_tiers=[
                ThresholdTier(spend_twd=50000, reward_twd=1000),
                ThresholdTier(spend_twd=35000, reward_twd=700),
            ]
        ),
    )
    assert "threshold_not_monotonic" in invariants.check(offer)


def test_monotonic_tiers_pass() -> None:
    offer = _offer(
        registration=Registration(
            required=False,
            timing_contract=TimingContract(kind="retroactive_ok", confidence=0.7),
        ),
        conditions=Conditions(
            threshold_tiers=[
                ThresholdTier(spend_twd=35000, reward_twd=700, quota_seats=100),
                ThresholdTier(spend_twd=50000, reward_twd=1000, quota_seats=100),
                ThresholdTier(spend_twd=65000, reward_twd=1300, quota_seats=100),
                ThresholdTier(spend_twd=75000, reward_twd=1500, quota_seats=50),
            ]
        ),
    )
    assert "threshold_not_monotonic" not in invariants.check(offer)


def test_unknown_contract_on_required_registration_is_flagged() -> None:
    offer = _offer(
        registration=Registration(
            required=True,
            windows=[
                RegistrationWindow(
                    kind="range",
                    start=_dt("2026-08-01T00:00:00+08:00"),
                    end=_dt("2026-08-10T23:59:00+08:00"),
                    confidence=0.95,
                )
            ],
            timing_contract=TimingContract(kind="unknown"),
        )
    )
    assert "contract_unknown" in invariants.check(offer)
