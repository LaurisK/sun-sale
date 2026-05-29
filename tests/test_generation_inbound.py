"""Tests for inbound/generation.py — pure Python, no HA required."""
from datetime import datetime, timedelta, timezone

from custom_components.sun_sale.contract.models import (
    GenerationHistory,
    GenerationReading,
    PriceEntry,
    PriceSeries,
    PvPowerHistory,
    PvPowerReading,
)
from custom_components.sun_sale.inbound.generation import (
    build_observed_generation_series,
)
from custom_components.sun_sale.inbound.pricing import build_price_series
from tests.conftest import BASE_DT, default_tariff_config, make_price

# BASE_DT is 2024-01-15 00:00 UTC. Use NOW for "today", YESTERDAY for the day before.
NOW = BASE_DT
TODAY = NOW.date()
YESTERDAY = TODAY - timedelta(days=1)

_NO_POWER = PvPowerHistory(samples=())
_NO_GEN = GenerationHistory(samples=())


def _hourly_72h_price_series() -> PriceSeries:
    """72h hourly grid covering yesterday 00:00 → tomorrow 23:59 (UTC)."""
    base = NOW - timedelta(days=1)
    entries = [
        PriceEntry(
            start=base + timedelta(hours=h),
            end=base + timedelta(hours=h + 1),
            price_eur_kwh=0.10,
        )
        for h in range(72)
    ]
    return build_price_series(entries, default_tariff_config(), now=NOW)


def _empty_hourly_today() -> PriceSeries:
    return build_price_series(
        [make_price(h, 0.10) for h in range(24)],
        default_tariff_config(),
        now=NOW,
    )


def _reading(t: datetime, kwh: float) -> GenerationReading:
    return GenerationReading(today_total_kwh=kwh, timestamp=t)


def _power(t: datetime, w: float) -> PvPowerReading:
    return PvPowerReading(power_w=w, timestamp=t)


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------

def test_empty_history_yields_empty_series():
    series = build_observed_generation_series(
        _NO_POWER, _NO_GEN, _empty_hourly_today().slots, now=NOW
    )
    assert series.slots == ()
    assert series.total_yesterday_kwh == 0.0
    assert series.total_today_so_far_kwh == 0.0


def test_single_counter_sample_cannot_be_differenced():
    history = GenerationHistory(samples=(_reading(NOW.replace(hour=10), 1.0),))
    series = build_observed_generation_series(
        _NO_POWER, history, _empty_hourly_today().slots, now=NOW
    )
    assert series.slots == ()


def test_empty_price_grid_yields_empty_series():
    base = NOW
    history = GenerationHistory(samples=(
        _reading(base, 0.0),
        _reading(base + timedelta(hours=1), 2.0),
    ))
    series = build_observed_generation_series(_NO_POWER, history, (), now=NOW)
    assert series.slots == ()


# ---------------------------------------------------------------------------
# Counter fallback: per-slot differencing on a single day
# ---------------------------------------------------------------------------

def test_one_interval_fully_inside_one_slot():
    # samples at 10:00 and 11:00 with delta = 2 kWh → today hour-10 slot gets 2 kWh
    now = NOW.replace(hour=12)
    history = GenerationHistory(samples=(
        _reading(NOW.replace(hour=10), 0.0),
        _reading(NOW.replace(hour=11), 2.0),
    ))
    series = build_observed_generation_series(
        _NO_POWER, history, _hourly_72h_price_series().slots, now=now
    )
    by_hour = {(s.start.date(), s.start.hour): s.generated_kwh for s in series.slots}
    assert abs(by_hour[(TODAY, 10)] - 2.0) < 1e-6
    # Other today slots are zero
    assert by_hour[(TODAY, 11)] == 0.0


def test_interval_spanning_two_slots_split_by_overlap():
    # Sample at 10:30 → 11:30, delta 4 kWh; half lands in hour-10, half in hour-11
    now = NOW.replace(hour=12)
    history = GenerationHistory(samples=(
        _reading(NOW.replace(hour=10, minute=30), 0.0),
        _reading(NOW.replace(hour=11, minute=30), 4.0),
    ))
    series = build_observed_generation_series(
        _NO_POWER, history, _hourly_72h_price_series().slots, now=now
    )
    by_hour = {(s.start.date(), s.start.hour): s.generated_kwh for s in series.slots}
    assert abs(by_hour[(TODAY, 10)] - 2.0) < 1e-6
    assert abs(by_hour[(TODAY, 11)] - 2.0) < 1e-6


def test_multiple_intervals_aggregate_within_slot():
    # Two consecutive 15-min intervals inside hour-10 add up.
    now = NOW.replace(hour=11)
    history = GenerationHistory(samples=(
        _reading(NOW.replace(hour=10, minute=0), 0.0),
        _reading(NOW.replace(hour=10, minute=15), 0.5),
        _reading(NOW.replace(hour=10, minute=30), 1.2),
    ))
    series = build_observed_generation_series(
        _NO_POWER, history, _hourly_72h_price_series().slots, now=now
    )
    slot_10 = next(s for s in series.slots if s.start == NOW.replace(hour=10))
    assert abs(slot_10.generated_kwh - 1.2) < 1e-6


# ---------------------------------------------------------------------------
# Counter fallback: midnight reset handling
# ---------------------------------------------------------------------------

def test_reset_handled_by_per_day_grouping():
    # Yesterday ended at 8.0 kWh; today began at 0.5 kWh. With the
    # today_total(end) − today_total(start) semantics, the reset is handled
    # implicitly: yesterday's counter is treated as 0 at UTC midnight, and so
    # is today's.
    yesterday_dt = NOW - timedelta(days=1)
    now = NOW.replace(hour=2)
    history = GenerationHistory(samples=(
        _reading(yesterday_dt.replace(hour=20), 6.0),
        _reading(yesterday_dt.replace(hour=21), 8.0),
        _reading(NOW.replace(hour=0, minute=30), 0.5),
        _reading(NOW.replace(hour=1, minute=30), 1.5),
    ))
    series = build_observed_generation_series(
        _NO_POWER, history, _hourly_72h_price_series().slots, now=now
    )
    by_hour = {(s.start.date(), s.start.hour): s.generated_kwh for s in series.slots}
    # Yesterday hour-20 covered the 6→8 delta = 2 kWh
    assert abs(by_hour[(YESTERDAY, 20)] - 2.0) < 1e-6
    # No samples past 21:00 yesterday → counter is clamped to last value,
    # so no further kWh is attributed to yesterday's late hours.
    assert by_hour[(YESTERDAY, 21)] == 0.0
    assert by_hour[(YESTERDAY, 22)] == 0.0
    assert by_hour[(YESTERDAY, 23)] == 0.0
    # Today hour-0: counter goes from 0 (midnight anchor) to interp(01:00)=1.0
    assert abs(by_hour[(TODAY, 0)] - 1.0) < 1e-6
    # Today hour-1: interp(01:00)=1.0 → clamp(02:00)=1.5 = 0.5
    assert abs(by_hour[(TODAY, 1)] - 0.5) < 1e-6


# ---------------------------------------------------------------------------
# Window: yesterday 00:00 → now
# ---------------------------------------------------------------------------

def test_slots_in_tomorrow_excluded():
    now = NOW.replace(hour=10)
    history = GenerationHistory(samples=(
        _reading(NOW.replace(hour=8), 0.0),
        _reading(NOW.replace(hour=9), 1.5),
    ))
    series = build_observed_generation_series(
        _NO_POWER, history, _hourly_72h_price_series().slots, now=now
    )
    tomorrow_date = (TODAY + timedelta(days=1))
    assert all(s.start.date() != tomorrow_date for s in series.slots)


def test_slots_starting_at_or_after_now_excluded():
    now = NOW.replace(hour=10, minute=30)
    history = GenerationHistory(samples=(
        _reading(NOW.replace(hour=9), 0.0),
        _reading(NOW.replace(hour=10), 1.0),
    ))
    series = build_observed_generation_series(
        _NO_POWER, history, _hourly_72h_price_series().slots, now=now
    )
    # Hour-10 slot starts at 10:00 (< 10:30), so it's included.
    # Hour-11 starts at 11:00 (>= 10:30), so it's excluded.
    starts = {s.start for s in series.slots}
    assert NOW.replace(hour=10) in starts
    assert NOW.replace(hour=11) not in starts


def test_slots_before_yesterday_midnight_excluded():
    # Sample two days ago shouldn't produce a slot.
    two_days_ago = NOW - timedelta(days=2)
    now = NOW.replace(hour=12)
    history = GenerationHistory(samples=(
        _reading(two_days_ago.replace(hour=10), 0.0),
        _reading(two_days_ago.replace(hour=11), 2.0),
        _reading(NOW.replace(hour=8), 0.0),
        _reading(NOW.replace(hour=9), 1.0),
    ))
    series = build_observed_generation_series(
        _NO_POWER, history, _hourly_72h_price_series().slots, now=now
    )
    assert all(s.start >= NOW - timedelta(days=1) for s in series.slots)


# ---------------------------------------------------------------------------
# Per-day totals (counter fallback)
# ---------------------------------------------------------------------------

def test_totals_split_between_yesterday_and_today():
    yesterday_dt = NOW - timedelta(days=1)
    now = NOW.replace(hour=12)
    history = GenerationHistory(samples=(
        _reading(yesterday_dt.replace(hour=10), 0.0),
        _reading(yesterday_dt.replace(hour=11), 3.0),
        _reading(NOW.replace(hour=10), 0.0),
        _reading(NOW.replace(hour=11), 2.0),
    ))
    series = build_observed_generation_series(
        _NO_POWER, history, _hourly_72h_price_series().slots, now=now
    )
    assert abs(series.total_yesterday_kwh - 3.0) < 1e-6
    assert abs(series.total_today_so_far_kwh - 2.0) < 1e-6


def test_source_is_inverter_on_every_slot():
    now = NOW.replace(hour=12)
    history = GenerationHistory(samples=(
        _reading(NOW.replace(hour=10), 0.0),
        _reading(NOW.replace(hour=11), 1.0),
    ))
    series = build_observed_generation_series(
        _NO_POWER, history, _hourly_72h_price_series().slots, now=now
    )
    assert all(s.source == "inverter" for s in series.slots)


# ---------------------------------------------------------------------------
# Counter fallback: quarter-hour price grid
# ---------------------------------------------------------------------------

def test_resamples_onto_quarter_hour_grid():
    # 1h interval (10:00–11:00) carrying 4 kWh → on 15-min grid: 4 × 1 kWh
    quarter_entries = [
        PriceEntry(
            start=NOW + timedelta(minutes=15 * q),
            end=NOW + timedelta(minutes=15 * (q + 1)),
            price_eur_kwh=0.10,
        )
        for q in range(96)
    ]
    ps = build_price_series(quarter_entries, default_tariff_config(), now=NOW)
    now = NOW.replace(hour=12)
    history = GenerationHistory(samples=(
        _reading(NOW.replace(hour=10), 0.0),
        _reading(NOW.replace(hour=11), 4.0),
    ))
    series = build_observed_generation_series(
        _NO_POWER, history, ps.slots, now=now
    )
    slots_10 = [s for s in series.slots if NOW.replace(hour=10) <= s.start < NOW.replace(hour=11)]
    assert len(slots_10) == 4
    for s in slots_10:
        assert abs(s.generated_kwh - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# PV power averaging (primary path)
# ---------------------------------------------------------------------------

def test_power_averaging_one_sample_per_slot():
    # 2000 W reading at 10:30 → 1h slot → 2.0 kWh
    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(NOW.replace(hour=10, minute=30), 2000.0),
    ))
    series = build_observed_generation_series(
        power_history, _NO_GEN, _hourly_72h_price_series().slots, now=now
    )
    slot_10 = next(s for s in series.slots if s.start == NOW.replace(hour=10))
    assert abs(slot_10.generated_kwh - 2.0) < 1e-6


def test_power_averaging_multiple_samples_averaged():
    # 1000 W and 3000 W in hour-10 → average 2000 W → 2.0 kWh
    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(NOW.replace(hour=10, minute=10), 1000.0),
        _power(NOW.replace(hour=10, minute=50), 3000.0),
    ))
    series = build_observed_generation_series(
        power_history, _NO_GEN, _hourly_72h_price_series().slots, now=now
    )
    slot_10 = next(s for s in series.slots if s.start == NOW.replace(hour=10))
    assert abs(slot_10.generated_kwh - 2.0) < 1e-6


def test_power_slot_with_no_samples_gives_zero():
    # Only hour-10 has a reading; hour-11 should be 0.
    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(NOW.replace(hour=10, minute=30), 1000.0),
    ))
    series = build_observed_generation_series(
        power_history, _NO_GEN, _hourly_72h_price_series().slots, now=now
    )
    slot_11 = next(s for s in series.slots if s.start == NOW.replace(hour=11))
    assert slot_11.generated_kwh == 0.0


def test_power_path_covers_yesterday_slots():
    # Power readings from yesterday should produce kWh for yesterday's slots.
    yesterday_dt = NOW - timedelta(days=1)
    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(yesterday_dt.replace(hour=10, minute=30), 4000.0),
    ))
    series = build_observed_generation_series(
        power_history, _NO_GEN, _hourly_72h_price_series().slots, now=now
    )
    slot = next(
        s for s in series.slots
        if s.start.date() == YESTERDAY and s.start.hour == 10
    )
    assert abs(slot.generated_kwh - 4.0) < 1e-6


def test_power_path_takes_precedence_over_counter():
    # Both available: power path should win (counter only used for correction).
    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(NOW.replace(hour=10, minute=30), 1000.0),
    ))
    # Counter says 5.0 kWh in the same window — should NOT become slot value
    # (correction is outside [0.5, 2.0] of 1.0, so it's skipped).
    gen_history = GenerationHistory(samples=(
        _reading(NOW.replace(hour=11), 5.0),
    ))
    series = build_observed_generation_series(
        power_history, gen_history, _hourly_72h_price_series().slots, now=now
    )
    slot_10 = next(s for s in series.slots if s.start == NOW.replace(hour=10))
    # 1000 W × 1 h / 1000 = 1.0 kWh (counter not used as slot value)
    assert abs(slot_10.generated_kwh - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# End-of-day correction
# ---------------------------------------------------------------------------

def test_correction_scales_today_slots_to_counter_total():
    # Power says 1.0 kWh; counter says 1.1 kWh → 10% upward correction.
    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(NOW.replace(hour=10, minute=30), 1000.0),
    ))
    gen_history = GenerationHistory(samples=(
        _reading(NOW.replace(hour=11), 1.1),
    ))
    series = build_observed_generation_series(
        power_history, gen_history, _hourly_72h_price_series().slots, now=now
    )
    today_sum = sum(
        s.generated_kwh for s in series.slots if s.start.date() == TODAY
    )
    assert abs(today_sum - 1.1) < 1e-3


def test_correction_does_not_modify_yesterday_slots():
    # Counter is only for today; yesterday should remain at raw power average.
    yesterday_dt = NOW - timedelta(days=1)
    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(yesterday_dt.replace(hour=10, minute=30), 2000.0),
        _power(NOW.replace(hour=10, minute=30), 1000.0),
    ))
    gen_history = GenerationHistory(samples=(
        _reading(NOW.replace(hour=11), 1.5),
    ))
    series = build_observed_generation_series(
        power_history, gen_history, _hourly_72h_price_series().slots, now=now
    )
    yest_slot = next(
        s for s in series.slots
        if s.start.date() == YESTERDAY and s.start.hour == 10
    )
    # Yesterday slot unchanged: 2000 W × 1 h / 1000 = 2.0 kWh
    assert abs(yest_slot.generated_kwh - 2.0) < 1e-6


def test_correction_skipped_when_factor_out_of_range():
    # Counter says 5× the power average → factor = 5.0 > 2.0, correction skipped.
    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(NOW.replace(hour=10, minute=30), 1000.0),
    ))
    gen_history = GenerationHistory(samples=(
        _reading(NOW.replace(hour=11), 5.0),
    ))
    series = build_observed_generation_series(
        power_history, gen_history, _hourly_72h_price_series().slots, now=now
    )
    slot_10 = next(s for s in series.slots if s.start == NOW.replace(hour=10))
    # Raw power average: 1.0 kWh (no correction applied)
    assert abs(slot_10.generated_kwh - 1.0) < 1e-6


def test_correction_skipped_when_no_counter_reading():
    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(NOW.replace(hour=10, minute=30), 1000.0),
    ))
    series = build_observed_generation_series(
        power_history, _NO_GEN, _hourly_72h_price_series().slots, now=now
    )
    slot_10 = next(s for s in series.slots if s.start == NOW.replace(hour=10))
    assert abs(slot_10.generated_kwh - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# Node wiring
# ---------------------------------------------------------------------------

def test_observed_generation_node_produces_series_from_primary_and_secondary():
    import asyncio

    from custom_components.sun_sale.contract.models import (
        ObservedGenerationSeries,
        SunSaleConfig,
    )
    from custom_components.sun_sale.pipeline.dag_engine import NodeContext
    from custom_components.sun_sale.pipeline.nodes import ObservedGenerationNode

    now = NOW.replace(hour=12)
    history = GenerationHistory(samples=(
        _reading(NOW.replace(hour=10), 0.0),
        _reading(NOW.replace(hour=11), 2.5),
    ))
    config = SunSaleConfig(tariff=None, battery=None)
    ctx = NodeContext(
        primary={GenerationHistory: history, PvPowerHistory: _NO_POWER},
        secondary={PriceSeries: _hourly_72h_price_series()},
        config=config,
        now=now,
    )

    node = ObservedGenerationNode()
    asyncio.run(node.run(ctx))

    series = ctx.secondary[ObservedGenerationSeries]
    assert isinstance(series, ObservedGenerationSeries)
    slot_10 = next(s for s in series.slots if s.start == NOW.replace(hour=10))
    assert abs(slot_10.generated_kwh - 2.5) < 1e-6
    assert abs(series.total_today_so_far_kwh - 2.5) < 1e-6
