"""Base load estimation + battery-runtime forecast.

Pure Python — no Home Assistant imports, no third-party deps.

Two public functions:

  - `build_base_load_profile(history, local_tz, ...)` — low-percentile per
    local hour-of-day across a rolling window of measured samples.
  - `estimate_battery_runtime(status, config, profile, ...)` —
    forward-simulate **pure baseload drain** from `now` and return the time
    until the battery hits `min_soc`. Solar generation and scheduled
    actions are intentionally NOT modelled: the output is a worst-case
    "household-only depletion" reserve, comparable cycle-to-cycle.

Design decisions (see docs/base_load_missing.md §1):

  - No day-class bucketing — 24 hour-of-day buckets only.
  - All bucket keys computed in local time; storage stays tz-aware UTC.
  - Runtime estimate ignores both forecast solar and the optimizer Schedule.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Iterable

from ..contract.models import (
    BaseLoadProfile,
    BaseLoadSlot,
    BatteryConfig,
    BatteryRuntimeEstimate,
    BatteryStatus,
    HouseholdLoadHistory,
)


# Below this many distinct local-date days in the window, the profile is
# considered too sparse to bucket: confidence=None, every slot uses fallback_kw.
MIN_HISTORY_DAYS = 7

# Buckets with fewer samples than this fall back to the cross-bucket fallback.
MIN_BUCKET_SAMPLES = 6

# "Minimum" floor = 10th percentile of bucket samples. Rejects sensor noise
# and momentary dips below the true baseline.
DEFAULT_PERCENTILE = 0.10

# Rolling window of samples considered.
DEFAULT_WINDOW_DAYS = 30

# Conservative cross-bucket fallback for sparse buckets — wider than the
# per-bucket P10 because we have less signal to lean on.
DEFAULT_FALLBACK_PERCENTILE = 0.20

# Last-resort default when there is no history at all (fresh install).
# Matches `inbound/battery._DEFAULT_HOUSEHOLD_LOAD_KW`.
DEFAULT_STUB_KW = 0.2

# How far ahead the runtime estimate simulates.
DEFAULT_HORIZON_HOURS = 48

# Simulation step. 5 min matches the coordinator update interval and gives
# sub-percent precision on the "until" timestamp.
SIMULATION_STEP_MINUTES = 5


# ---------------------------------------------------------------------------
# Profile construction
# ---------------------------------------------------------------------------

def build_base_load_profile(
    history: HouseholdLoadHistory,
    local_tz: tzinfo,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    percentile: float = DEFAULT_PERCENTILE,
) -> BaseLoadProfile:
    """Build a 24-bucket (local hour) low-percentile baseload profile from rolling samples.

    Args:
        history: Rolling household-load sample history.
        local_tz: Timezone used to convert sample timestamps to local hours.
        now: Reference time for the rolling window; defaults to UTC now.
        window_days: How many past days of samples to include.
        percentile: Percentile used per bucket (default P10 rejects transient dips).

    Returns:
        BaseLoadProfile with 24 slots (one per local hour) and summary diagnostics.
        Uses fallback_kw for sparse buckets or when history is insufficient.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    cutoff = now - timedelta(days=window_days)
    window_samples = [s for s in history.samples if s.timestamp >= cutoff]

    sample_count = len(window_samples)
    all_values = [s.load_kw for s in window_samples]
    distinct_days = len({s.timestamp.astimezone(local_tz).date() for s in window_samples})

    overall_p10 = _percentile(all_values, 0.10) if all_values else DEFAULT_STUB_KW
    overall_median = _percentile(all_values, 0.50) if all_values else DEFAULT_STUB_KW

    if distinct_days < MIN_HISTORY_DAYS:
        fallback = overall_p10 if all_values else DEFAULT_STUB_KW
        slots = tuple(
            BaseLoadSlot(hour=h, baseload_kw=fallback, sample_count=0, is_fallback=True)
            for h in range(24)
        )
        return BaseLoadProfile(
            slots=slots,
            fallback_kw=fallback,
            overall_p10_kw=overall_p10,
            overall_median_kw=overall_median,
            confidence=None,
            sample_count=sample_count,
            distinct_days=distinct_days,
            computed_at=now,
        )

    buckets: dict[int, list[float]] = {h: [] for h in range(24)}
    for s in window_samples:
        h = s.timestamp.astimezone(local_tz).hour
        buckets[h].append(s.load_kw)

    fallback_kw = _percentile(all_values, DEFAULT_FALLBACK_PERCENTILE)

    slots = tuple(
        BaseLoadSlot(
            hour=h,
            baseload_kw=fallback_kw if len(buckets[h]) < MIN_BUCKET_SAMPLES
                       else _percentile(buckets[h], percentile),
            sample_count=len(buckets[h]),
            is_fallback=len(buckets[h]) < MIN_BUCKET_SAMPLES,
        )
        for h in range(24)
    )

    return BaseLoadProfile(
        slots=slots,
        fallback_kw=fallback_kw,
        overall_p10_kw=overall_p10,
        overall_median_kw=overall_median,
        confidence=min(1.0, distinct_days / float(window_days)),
        sample_count=sample_count,
        distinct_days=distinct_days,
        computed_at=now,
    )


# ---------------------------------------------------------------------------
# Runtime estimation
# ---------------------------------------------------------------------------

def estimate_battery_runtime(
    battery_status: BatteryStatus,
    battery_config: BatteryConfig,
    profile: BaseLoadProfile,
    local_tz: tzinfo,
    now: datetime,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
) -> BatteryRuntimeEstimate:
    """Forward-simulate pure baseload drain to estimate how long the battery lasts.

    Steps through SIMULATION_STEP_MINUTES intervals, subtracting the per-hour
    baseload at each step. Solar and scheduled charge/discharge are intentionally
    ignored — this is a worst-case "household-only" reserve estimate.

    Args:
        battery_status: Live SoC and configured total capacity.
        battery_config: Battery limits (min_soc used as the depletion threshold).
        profile: 24-bucket baseload profile supplying per-hour drain rates.
        local_tz: Timezone for mapping simulation steps to local hours.
        now: Start of the simulation.
        horizon_hours: How far ahead to simulate before giving up.

    Returns:
        BatteryRuntimeEstimate with runtime_minutes/until set to None when the
        battery does not deplete within the horizon.
    """
    usable_kwh = max(
        0.0,
        (battery_status.soc - battery_config.min_soc) * battery_status.total_capacity_kwh,
    )

    step = timedelta(minutes=SIMULATION_STEP_MINUTES)
    step_hours = SIMULATION_STEP_MINUTES / 60.0
    horizon_end = now + timedelta(hours=horizon_hours)
    one_hour_after_now = now + timedelta(hours=1)

    drain_kw_first_hour: list[float] = []
    remaining = usable_kwh
    elapsed_minutes = 0.0
    t = now

    while t < horizon_end:
        drain_kw = profile.at(t, local_tz)

        if t < one_hour_after_now:
            drain_kw_first_hour.append(drain_kw)

        drain_kwh = drain_kw * step_hours

        if remaining <= 0:
            return BatteryRuntimeEstimate(
                remaining_kwh_usable=usable_kwh,
                avg_drain_kw_next_hour=_mean(drain_kw_first_hour),
                runtime_minutes=elapsed_minutes,
                until=t,
                horizon_hours=horizon_hours,
                computed_at=now,
            )

        if drain_kwh > 0 and drain_kwh >= remaining:
            fraction = remaining / drain_kwh
            partial = SIMULATION_STEP_MINUTES * fraction
            return BatteryRuntimeEstimate(
                remaining_kwh_usable=usable_kwh,
                avg_drain_kw_next_hour=_mean(drain_kw_first_hour),
                runtime_minutes=elapsed_minutes + partial,
                until=t + timedelta(minutes=partial),
                horizon_hours=horizon_hours,
                computed_at=now,
            )

        remaining -= drain_kwh
        elapsed_minutes += SIMULATION_STEP_MINUTES
        t += step

    return BatteryRuntimeEstimate(
        remaining_kwh_usable=usable_kwh,
        avg_drain_kw_next_hour=_mean(drain_kw_first_hour),
        runtime_minutes=None,
        until=None,
        horizon_hours=horizon_hours,
        computed_at=now,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _percentile(values: list[float], p: float) -> float:
    """Compute a linear-interpolation percentile.

    Args:
        values: Non-empty list of floats (unsorted is fine).
        p: Percentile in [0, 1].

    Returns:
        Interpolated percentile value, or 0.0 for empty input.
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    sorted_v = sorted(values)
    n = len(sorted_v)
    rank = p * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * frac


def _mean(values: Iterable[float]) -> float:
    """Return the arithmetic mean of values, or 0.0 for empty input.

    Args:
        values: Iterable of floats.

    Returns:
        Mean value, or 0.0 when values is empty.
    """
    seq = list(values)
    return sum(seq) / len(seq) if seq else 0.0
