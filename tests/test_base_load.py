"""Tests for pipeline/base_load.py — pure Python, no HA mocking needed."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.sun_sale.contract.models import (
    BatteryConfig,
    BatteryStatus,
    HouseholdLoadHistory,
    HouseholdLoadSample,
)
from custom_components.sun_sale.pipeline.base_load import (
    DEFAULT_STUB_KW,
    MIN_BUCKET_SAMPLES,
    MIN_HISTORY_DAYS,
    SIMULATION_STEP_MINUTES,
    _percentile,
    build_base_load_profile,
    estimate_battery_runtime,
)


UTC = timezone.utc
RIGA = ZoneInfo("Europe/Riga")    # UTC+2 in winter, UTC+3 in summer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _samples(start: datetime, count: int, step_minutes: int, load_kw_fn):
    """Build `count` samples starting at `start`, stepping `step_minutes`.
    `load_kw_fn(i)` decides the load for sample i (0-indexed)."""
    out = []
    for i in range(count):
        ts = start + timedelta(minutes=step_minutes * i)
        out.append(HouseholdLoadSample(timestamp=ts, load_kw=load_kw_fn(i)))
    return out


def _history(samples):
    return HouseholdLoadHistory(samples=tuple(sorted(samples, key=lambda s: s.timestamp)))


def _battery_status(soc: float = 0.8, total_capacity_kwh: float = 10.0):
    return BatteryStatus(
        total_capacity_kwh=total_capacity_kwh,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        soc=soc,
        remaining_capacity_kwh=soc * total_capacity_kwh,
    )


def _battery_config(min_soc: float = 0.10):
    return BatteryConfig(
        nominal_capacity_kwh=10.0,
        purchase_price_eur=5000.0,
        rated_cycle_life=6000,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        min_soc=min_soc,
        max_soc=0.95,
        round_trip_efficiency=0.90,
    )


# ---------------------------------------------------------------------------
# _percentile
# ---------------------------------------------------------------------------

def test_percentile_empty_returns_zero():
    assert _percentile([], 0.5) == 0.0


def test_percentile_single_value():
    assert _percentile([2.5], 0.10) == 2.5
    assert _percentile([2.5], 0.99) == 2.5


def test_percentile_p10_of_ten_values():
    # Sorted [1..10], P10 → rank=0.9 → between idx 0 (=1) and idx 1 (=2) at 0.9
    # = 1 + (2-1)*0.9 = 1.9
    assert _percentile(list(range(1, 11)), 0.10) == pytest.approx(1.9)


def test_percentile_p100_returns_max():
    assert _percentile([1.0, 2.0, 3.0], 1.0) == 3.0


def test_percentile_p0_returns_min():
    assert _percentile([3.0, 1.0, 2.0], 0.0) == 1.0


# ---------------------------------------------------------------------------
# build_base_load_profile — sparsity
# ---------------------------------------------------------------------------

def test_empty_history_returns_sparse_profile_with_stub():
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    profile = build_base_load_profile(_history([]), RIGA, now=now)

    assert profile.confidence is None
    assert profile.sample_count == 0
    assert profile.distinct_days == 0
    assert len(profile.slots) == 24
    assert profile.fallback_kw == DEFAULT_STUB_KW
    for slot in profile.slots:
        assert slot.is_fallback
        assert slot.baseload_kw == DEFAULT_STUB_KW
        assert slot.sample_count == 0


def test_below_min_history_days_returns_sparse():
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    # 3 days × 24h × 12 samples/h, all at 0.5 kW
    samples = _samples(now - timedelta(days=3), 3 * 24 * 12, 5, lambda i: 0.5)
    profile = build_base_load_profile(_history(samples), RIGA, now=now)

    assert profile.distinct_days < MIN_HISTORY_DAYS
    assert profile.confidence is None
    # Sparse profile uses overall P10 as the floor — all samples are 0.5,
    # so the floor is 0.5 (not the stub).
    assert profile.fallback_kw == pytest.approx(0.5)
    for slot in profile.slots:
        assert slot.baseload_kw == pytest.approx(0.5)
        assert slot.is_fallback


def test_minimum_history_yields_confidence():
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    # Build samples across MIN_HISTORY_DAYS UTC days × 24h. Note: 24h of UTC
    # spans MIN_HISTORY_DAYS+1 local-tz dates because UTC 22–23 fall into the
    # next Riga calendar date — distinct_days will be slightly larger than the
    # number of UTC days.
    samples = []
    for d in range(MIN_HISTORY_DAYS):
        day_start = now - timedelta(days=MIN_HISTORY_DAYS - d)
        for h in range(24):
            samples.append(HouseholdLoadSample(
                timestamp=day_start.replace(hour=h, minute=0, second=0, microsecond=0),
                load_kw=0.3,
            ))
    profile = build_base_load_profile(_history(samples), RIGA, now=now, window_days=30)

    assert profile.confidence is not None
    assert profile.distinct_days >= MIN_HISTORY_DAYS
    assert profile.confidence == pytest.approx(profile.distinct_days / 30.0)
    assert 0.0 < profile.confidence <= 1.0


# ---------------------------------------------------------------------------
# build_base_load_profile — bucketing
# ---------------------------------------------------------------------------

def test_buckets_by_local_hour_not_utc():
    """A sample at 00:00 UTC in Riga (+2 winter) belongs to local hour 2."""
    now = datetime(2026, 1, 30, 12, tzinfo=UTC)
    # 14 distinct days, enough to exit sparsity. Put 10 samples at 00:00 UTC
    # on each day (= 02:00 Riga in winter) with a distinctive value.
    samples = []
    for d in range(14):
        day = now - timedelta(days=14 - d)
        for j in range(10):
            samples.append(HouseholdLoadSample(
                timestamp=day.replace(hour=0, minute=j * 5, second=0, microsecond=0),
                load_kw=0.7,
            ))
    # Add filler 0.1 kW samples in other UTC hours to keep buckets non-empty
    for d in range(14):
        day = now - timedelta(days=14 - d)
        for h in range(1, 24):
            for j in range(10):
                samples.append(HouseholdLoadSample(
                    timestamp=day.replace(hour=h, minute=j * 5, second=0, microsecond=0),
                    load_kw=0.1,
                ))
    profile = build_base_load_profile(_history(samples), RIGA, now=now)

    # Local hour 2 (UTC 0 + 2h winter) should hold the 0.7 samples
    assert profile.slots[2].baseload_kw == pytest.approx(0.7, abs=0.01)
    assert not profile.slots[2].is_fallback
    # UTC-hour 0 is local hour 2, so local hour 0 should hold the 0.1 filler
    assert profile.slots[0].baseload_kw == pytest.approx(0.1, abs=0.01)


def test_sparse_bucket_uses_fallback():
    """A bucket with < MIN_BUCKET_SAMPLES uses the cross-bucket fallback."""
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    samples = []
    for d in range(14):
        day = now - timedelta(days=14 - d)
        # Put 10 samples in every hour EXCEPT local hour 5
        for h in range(24):
            local_h = (h + 3) % 24    # UTC→Riga summer offset +3
            if local_h == 5:
                continue
            for j in range(10):
                samples.append(HouseholdLoadSample(
                    timestamp=day.replace(hour=h, minute=j * 5, second=0, microsecond=0),
                    load_kw=0.3,
                ))
    profile = build_base_load_profile(_history(samples), RIGA, now=now)

    # Local hour 5 should be fallback; all others populated
    sparse_slot = profile.slots[5]
    assert sparse_slot.is_fallback
    assert sparse_slot.sample_count < MIN_BUCKET_SAMPLES
    assert sparse_slot.baseload_kw == pytest.approx(profile.fallback_kw)


def test_p10_rejects_outlier_spikes():
    """A single spike in a bucket of mostly-low values shouldn't shift P10."""
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    samples = []
    for d in range(14):
        day = now - timedelta(days=14 - d)
        for h in range(24):
            # 10 baseline samples of 0.2 kW per hour
            for j in range(10):
                samples.append(HouseholdLoadSample(
                    timestamp=day.replace(hour=h, minute=j * 5, second=0, microsecond=0),
                    load_kw=0.2,
                ))
            # One occasional spike to 5.0 kW
            samples.append(HouseholdLoadSample(
                timestamp=day.replace(hour=h, minute=55, second=0, microsecond=0),
                load_kw=5.0,
            ))
    profile = build_base_load_profile(_history(samples), RIGA, now=now)

    # P10 should be near 0.2, not anywhere near the spike
    for slot in profile.slots:
        assert slot.baseload_kw == pytest.approx(0.2, abs=0.05)


def test_samples_outside_window_excluded():
    """Samples older than window_days don't influence the profile."""
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    # 60 days ago: huge constant load
    ancient = _samples(now - timedelta(days=60), 100, 5, lambda i: 9.0)
    # last 14 days: low constant load
    recent = []
    for d in range(14):
        day = now - timedelta(days=14 - d)
        for h in range(24):
            for j in range(10):
                recent.append(HouseholdLoadSample(
                    timestamp=day.replace(hour=h, minute=j * 5),
                    load_kw=0.2,
                ))
    profile = build_base_load_profile(
        _history(ancient + recent), RIGA, now=now, window_days=30,
    )
    assert profile.overall_p10_kw == pytest.approx(0.2, abs=0.05)


# ---------------------------------------------------------------------------
# BaseLoadProfile.at
# ---------------------------------------------------------------------------

def test_profile_at_uses_local_hour():
    now = datetime(2026, 1, 30, 12, tzinfo=UTC)
    profile = build_base_load_profile(_history([]), RIGA, now=now)
    # Even with stub profile, .at() should return the slot for the local hour
    t = datetime(2026, 1, 30, 0, tzinfo=UTC)   # 02:00 Riga winter
    assert profile.at(t, RIGA) == profile.slots[2].baseload_kw


# ---------------------------------------------------------------------------
# estimate_battery_runtime
# ---------------------------------------------------------------------------

def _flat_profile(kw: float, now: datetime) -> "BaseLoadProfile":
    """A profile where every hour returns `kw`, regardless of bucketing."""
    # Use empty history → sparse stub profile, then re-build a fake profile by
    # overriding the fallback. Simpler: call the real builder with synthetic
    # samples that produce exactly `kw` everywhere.
    from custom_components.sun_sale.contract.models import (
        BaseLoadProfile,
        BaseLoadSlot,
    )
    slots = tuple(
        BaseLoadSlot(hour=h, baseload_kw=kw, sample_count=100, is_fallback=False)
        for h in range(24)
    )
    return BaseLoadProfile(
        slots=slots,
        fallback_kw=kw,
        overall_p10_kw=kw,
        overall_median_kw=kw,
        confidence=1.0,
        sample_count=100 * 24,
        distinct_days=30,
        computed_at=now,
    )


def test_runtime_zero_when_soc_at_min():
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    status = _battery_status(soc=0.10)
    cfg = _battery_config(min_soc=0.10)
    profile = _flat_profile(0.5, now)

    est = estimate_battery_runtime(status, cfg, profile, RIGA, now)
    assert est.remaining_kwh_usable == 0.0
    assert est.runtime_minutes == 0.0
    assert est.until == now


def test_runtime_constant_drain():
    """10 kWh battery, min_soc 10% → 9 kWh usable, 1 kW baseload → 9 hours."""
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    status = _battery_status(soc=1.0, total_capacity_kwh=10.0)
    cfg = _battery_config(min_soc=0.10)
    profile = _flat_profile(1.0, now)

    est = estimate_battery_runtime(status, cfg, profile, RIGA, now)
    assert est.remaining_kwh_usable == pytest.approx(9.0)
    assert est.runtime_minutes == pytest.approx(9 * 60, abs=SIMULATION_STEP_MINUTES)
    assert est.until is not None
    assert (est.until - now).total_seconds() / 60 == pytest.approx(
        9 * 60, abs=SIMULATION_STEP_MINUTES,
    )
    assert est.avg_drain_kw_next_hour == pytest.approx(1.0)


def test_runtime_partial_step_interpolation():
    """Verify the final mid-step interpolation lands at the right minute."""
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    # usable = (0.105 - 0.10) * 10 = 0.05 kWh, drain 1.0 kW → 3 min.
    status = _battery_status(soc=0.105, total_capacity_kwh=10.0)
    cfg = _battery_config(min_soc=0.10)
    profile = _flat_profile(1.0, now)

    est = estimate_battery_runtime(status, cfg, profile, RIGA, now)
    assert est.runtime_minutes == pytest.approx(3.0, abs=0.1)


def test_runtime_horizon_limits_simulation():
    """If horizon ends before drain, runtime is None."""
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    status = _battery_status(soc=1.0, total_capacity_kwh=100.0)  # huge battery
    cfg = _battery_config(min_soc=0.10)
    profile = _flat_profile(0.5, now)

    est = estimate_battery_runtime(
        status, cfg, profile, RIGA, now, horizon_hours=2,
    )
    # 100 * 0.9 = 90 kWh usable; at 0.5 kW = 180 hours. Horizon = 2h.
    assert est.runtime_minutes is None
    assert est.until is None
    assert est.horizon_hours == 2


def test_runtime_drain_follows_profile_per_hour():
    """Profile with varying per-hour baseload drains at the correct rate."""
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    status = _battery_status(soc=1.0, total_capacity_kwh=10.0)
    cfg = _battery_config(min_soc=0.10)

    # Build a profile where local hour 14 (Riga UTC+3 summer = UTC 11) draws 4 kW,
    # all other hours 0.5 kW. now is UTC 12 = Riga 15:00 → first hour at local 15.
    from custom_components.sun_sale.contract.models import (
        BaseLoadProfile,
        BaseLoadSlot,
    )
    slots = tuple(
        BaseLoadSlot(
            hour=h,
            baseload_kw=4.0 if h == 15 else 0.5,
            sample_count=100, is_fallback=False,
        )
        for h in range(24)
    )
    profile = BaseLoadProfile(
        slots=slots, fallback_kw=0.5, overall_p10_kw=0.5, overall_median_kw=0.5,
        confidence=1.0, sample_count=2400, distinct_days=30, computed_at=now,
    )

    est = estimate_battery_runtime(status, cfg, profile, RIGA, now)
    # First hour drains at 4 kW (local 15:00) → 4 kWh in hour 1
    # Remaining 5 kWh @ 0.5 kW = 10 hours → total runtime ≈ 11 hours
    assert est.runtime_minutes == pytest.approx(11 * 60, abs=SIMULATION_STEP_MINUTES)
    assert est.avg_drain_kw_next_hour == pytest.approx(4.0)
