"""Tests for schedule.py — SoC-bucketed DP scheduler.

Pure Python, no HA required. These tests check schedule-level invariants
(chronology, SoC bounds, profit accounting, mode selection under price
extremes). Per-mode energy physics is covered by tests/test_slot_physics.py.
"""
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.sun_sale.contract.models import (
    BaseLoadProfile,
    BaseLoadSlot,
    DayClass,
    GenerationSeries,
    GenerationSlot,
    ProfitabilityScore,
    SolarForecast,
    StorageMode,
    TariffConfig,
)
from custom_components.sun_sale.inbound.pricing import build_price_series
from custom_components.sun_sale.pipeline.battery import degradation_cost_per_kwh
from custom_components.sun_sale.pipeline.calculation import calculate
from custom_components.sun_sale.pipeline.schedule import optimize_schedule
from tests.conftest import (
    BASE_DT,
    default_battery_config,
    default_battery_state,
    default_tariff_config,
    make_price,
    make_solar,
)

NOW = BASE_DT  # 2024-01-15 00:00 UTC


def _make_gen_series(solar: list[SolarForecast]) -> GenerationSeries:
    """Build a GenerationSeries from a list of SolarForecast entries."""
    slots = tuple(
        GenerationSlot(start=sf.start, end=sf.end, expected_kwh=sf.generation_kwh)
        for sf in solar
    )
    return GenerationSeries(slots=slots)


def run(prices, solar=None, soc=0.50, battery_config=None, tariff_config=None):
    """Convenience wrapper that wires up the pipeline pieces and runs the DP."""
    bc = battery_config or default_battery_config()
    tc = tariff_config or default_tariff_config()
    state = default_battery_state(soc)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    price_series = build_price_series(prices, tc, now=NOW)
    gen_series = _make_gen_series(solar or [])
    calc = calculate(price_series, gen_series, state, NOW)
    return optimize_schedule(price_series, calc, bc, state, deg, NOW)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_prices_returns_empty_schedule():
    result = run([])
    assert result.slots == []
    assert result.total_expected_profit_eur == 0.0


def test_single_price_emits_one_slot():
    """A flat one-slot horizon at min_soc: no arbitrage, no battery moves."""
    bc = default_battery_config()
    result = run([make_price(0, 0.10)], soc=bc.min_soc, battery_config=bc)
    assert len(result.slots) == 1
    assert result.slots[0].mode in {StorageMode.STORE, StorageMode.STBY}
    assert result.slots[0].power_kw == pytest.approx(0.0)


def test_flat_prices_no_charge_at_min_soc():
    """Flat prices at min_soc → no GULP (spread doesn't recoup deg × 2)."""
    # At min_soc the DP has nothing to DUMP; a GULP would need a future DUMP to
    # recoup, which the spread doesn't justify.
    bc = default_battery_config()
    prices = [make_price(h, 0.10) for h in range(24)]
    result = run(prices, soc=bc.min_soc, battery_config=bc)
    assert all(s.mode not in {StorageMode.GULP, StorageMode.DUMP} for s in result.slots)


# ---------------------------------------------------------------------------
# Core optimisation
# ---------------------------------------------------------------------------


def test_obvious_buy_low_sell_high():
    """One very cheap hour followed later by one very expensive hour → trade."""
    prices = (
        [make_price(0, 0.01)]
        + [make_price(h, 0.10) for h in range(1, 12)]
        + [make_price(12, 0.50)]
        + [make_price(h, 0.10) for h in range(13, 24)]
    )
    result = run(prices)
    slot_0 = next(s for s in result.slots if s.start.hour == 0)
    slot_12 = next(s for s in result.slots if s.start.hour == 12)
    assert slot_0.mode == StorageMode.GULP
    assert slot_12.mode == StorageMode.DUMP


def test_spread_below_degradation_does_not_charge_from_empty():
    """Small spread at min_soc → no GULP (no future DUMP pays for the charge)."""
    # Degradation ~ 5000/(6000*10*2) = 0.0417 EUR/storage-kWh; spread of 0.02
    # is well below the round-trip degradation hurdle.
    bc = default_battery_config()
    prices = [make_price(h, 0.10 if h < 12 else 0.12) for h in range(24)]
    result = run(prices, soc=bc.min_soc, battery_config=bc)
    assert not any(s.mode == StorageMode.GULP for s in result.slots)


def test_charge_before_discharge():
    """Chronological constraint: the DP cannot DUMP before it has charged."""
    prices = (
        [make_price(0, 0.01)]
        + [make_price(h, 0.50) for h in range(1, 4)]
        + [make_price(h, 0.10) for h in range(4, 24)]
    )
    result = run(prices, soc=0.20)    # near-empty so DP must charge first
    charge_slots = [s for s in result.slots if s.mode == StorageMode.GULP]
    discharge_slots = [s for s in result.slots if s.mode == StorageMode.DUMP]
    if charge_slots and discharge_slots:
        assert min(s.start for s in charge_slots) < min(s.start for s in discharge_slots)


def test_multi_leg_arbitrage_visited():
    """Two cheap→expensive cycles in the horizon: DP should exploit both."""
    # Cheap at 0 and 12; expensive at 6 and 18 (>>deg + efficiency loss).
    prices = []
    for h in range(24):
        if h in (0, 12):
            spot = 0.01
        elif h in (6, 18):
            spot = 0.60
        else:
            spot = 0.10
        prices.append(make_price(h, spot))
    result = run(prices, soc=0.30)
    discharge_hours = sorted(s.start.hour for s in result.slots if s.mode == StorageMode.DUMP)
    # Both peaks should be exploited.
    assert 6 in discharge_hours
    assert 18 in discharge_hours


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


def test_no_charge_when_full():
    """At max_soc, DP cannot GULP (no headroom to put kWh in)."""
    bc = default_battery_config()
    prices = [make_price(0, 0.01)] + [make_price(h, 0.50) for h in range(1, 4)]
    result = run(prices, soc=bc.max_soc, battery_config=bc)
    # Slot 0 has no headroom — GULP would charge 0 kWh. DP may pick anything
    # neutral (STORE/STBY); the key is it does not waste a "trade" attempt here.
    slot_0 = next(s for s in result.slots if s.start.hour == 0)
    assert slot_0.power_kw == 0.0 or slot_0.mode != StorageMode.GULP


def test_no_discharge_when_empty():
    """At min_soc, DP cannot DUMP (no battery to drain)."""
    bc = default_battery_config()
    prices = [make_price(0, 0.50)] + [make_price(h, 0.01) for h in range(1, 4)]
    result = run(prices, soc=bc.min_soc, battery_config=bc)
    slot_0 = next(s for s in result.slots if s.start.hour == 0)
    assert slot_0.power_kw == 0.0 or slot_0.mode != StorageMode.DUMP


def test_soc_stays_within_bounds_throughout():
    """Forward-rolled SoC must respect the configured envelope."""
    bc = default_battery_config()
    prices = [make_price(h, 0.01 if h < 6 else 0.50) for h in range(24)]
    result = run(prices, soc=0.5, battery_config=bc)
    for slot in result.slots:
        assert bc.min_soc - 1e-6 <= slot.expected_soc_after <= bc.max_soc + 1e-6, (
            f"SoC {slot.expected_soc_after} out of bounds at {slot.start}"
        )


def test_power_does_not_exceed_max():
    """Battery flow per slot stays within the configured power limit."""
    bc = default_battery_config()
    prices = (
        [make_price(0, 0.01)]
        + [make_price(h, 0.10) for h in range(1, 23)]
        + [make_price(23, 0.50)]
    )
    result = run(prices, battery_config=bc)
    for slot in result.slots:
        if slot.mode == StorageMode.GULP:
            assert slot.power_kw <= bc.max_charge_power_kw + 1e-6
        elif slot.mode == StorageMode.DUMP:
            assert slot.power_kw <= bc.max_discharge_power_kw + 1e-6


# ---------------------------------------------------------------------------
# Solar handling
# ---------------------------------------------------------------------------


def test_solar_at_negative_sell_does_not_export():
    """Solar slot inside a negative-sell window → DP must not DUMP or SELL.

    Both would push solar to the grid at a loss. STORE/HOARD/STBY are
    the acceptable choices.
    """
    tc = _high_sell_fee_config()
    bc = default_battery_config()
    prices = [make_price(h, 0.05) for h in range(4)]    # spot 0.05 → sell ≈ −0.15
    solar = [make_solar(2, 2.0)]
    result = run(prices, solar=solar, soc=bc.min_soc, battery_config=bc, tariff_config=tc)
    solar_slot = next(s for s in result.slots if s.start.hour == 2)
    assert solar_slot.mode not in {StorageMode.DUMP, StorageMode.SELL}


def test_no_solar_at_min_soc_picks_passive_modes():
    """No solar at min_soc → DP has nothing to do, picks STORE or STBY."""
    bc = default_battery_config()
    prices = [make_price(h, 0.10) for h in range(4)]
    result = run(prices, solar=[], soc=bc.min_soc, battery_config=bc)
    for slot in result.slots:
        assert slot.mode in {StorageMode.STORE, StorageMode.STBY}
        assert slot.power_kw == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------


def test_total_profit_equals_sum_of_slots():
    """schedule.total_expected_profit_eur is the sum of slot profits."""
    prices = (
        [make_price(0, 0.01)]
        + [make_price(h, 0.10) for h in range(1, 23)]
        + [make_price(23, 0.50)]
    )
    result = run(prices)
    expected = sum(s.expected_profit_eur for s in result.slots)
    assert result.total_expected_profit_eur == pytest.approx(expected, abs=1e-9)


def test_degradation_cost_stored_in_schedule():
    """Schedule reports the deg-cost it ran with for downstream traceability."""
    prices = [make_price(h, 0.10) for h in range(4)]
    result = run(prices)
    assert result.degradation_cost_per_kwh > 0


# ---------------------------------------------------------------------------
# Negative-sell handling (former feed-in lockout)
# ---------------------------------------------------------------------------


def _high_sell_fee_config() -> TariffConfig:
    """A tariff with a fat sell fee so low-spot hours have negative sell."""
    return TariffConfig(
        distribution_fee=0.0, tax_rate=0.0, markup=0.0,
        sell_distribution_fee=0.20, sell_tax_rate=0.0, sell_markup=0.0,
    )


def _run_with_negative_sell_window(locked_hours: list[int]):
    """Drive the DP with a price series that has negative-sell windows."""
    tc = _high_sell_fee_config()
    prices = [make_price(h, 0.05 if h in locked_hours else 0.30) for h in range(24)]
    return run(prices, tariff_config=tc, soc=0.50)


def test_no_dump_inside_negative_sell_window():
    """DUMP would lose money at sell < 0 → DP refuses to pick it."""
    locked = list(range(10, 14))
    result = _run_with_negative_sell_window(locked)
    discharge_hours = [s.start.hour for s in result.slots if s.mode == StorageMode.DUMP]
    assert all(h not in locked for h in discharge_hours), (
        f"DUMP inside locked hours: {[h for h in discharge_hours if h in locked]}"
    )


def test_negative_sell_slot_does_not_export():
    """Inside the negative-sell window the chosen mode is HOARD/STBY/STORE.

    Whichever it is, the slot's power_kw must be 0 — no battery cycling
    and no grid export.
    """
    locked = [12]
    result = _run_with_negative_sell_window(locked)
    locked_slot = next(s for s in result.slots if s.start.hour == 12)
    assert locked_slot.mode in {StorageMode.HOARD, StorageMode.STBY, StorageMode.STORE}
    assert locked_slot.power_kw == pytest.approx(0.0)


def test_dump_allowed_outside_negative_sell_window():
    """A genuine peak outside the lockout still triggers DUMP."""
    tc = _high_sell_fee_config()
    prices = (
        [make_price(0, 0.01)]
        + [make_price(h, 0.10) for h in range(1, 12)]
        + [make_price(12, 0.05)]                           # locked-out hour
        + [make_price(h, 0.10) for h in range(13, 23)]
        + [make_price(23, 0.80)]                            # huge sell
    )
    result = run(prices, tariff_config=tc, soc=0.30)
    discharge_slots = [s for s in result.slots if s.mode == StorageMode.DUMP]
    assert discharge_slots, "Expected at least one DUMP slot at hour 23"
    assert all(s.start.hour != 12 for s in discharge_slots)


# ---------------------------------------------------------------------------
# Charging-profile back-compat
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BaseLoadProfile wiring (phase 3)
# ---------------------------------------------------------------------------


def _flat_baseload(kw: float) -> BaseLoadProfile:
    """Build a BaseLoadProfile that returns the same baseload kW every hour."""
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
        sample_count=2400,
        distinct_days=30,
        computed_at=NOW,
    )


def test_baseload_increases_grid_import_when_battery_empty():
    """At min_soc with baseload, STBY/STORE slots import baseload from grid."""
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(bc.min_soc)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    prices = [make_price(h, 0.05) for h in range(4)]    # low spot, low sell
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series([])
    calc = calculate(ps, gen, state, NOW)
    profile = _flat_baseload(0.5)    # 0.5 kW baseload always on

    result = optimize_schedule(
        ps, calc, bc, state, deg, NOW,
        base_load_profile=profile, local_tz=timezone.utc,
    )
    # Expected baseload draw per hour = 0.5 kWh. Schedule reports per-slot reward;
    # at min_soc with no solar and no headroom-useful trade, baseload import shows up
    # as a small negative reward on every slot.
    assert all(s.expected_profit_eur < 0 for s in result.slots), (
        "Every slot should incur baseload import cost"
    )


def test_baseload_does_not_change_pure_arbitrage_decision():
    """A small baseload doesn't stop the DP from spotting big arbitrage spreads."""
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(bc.min_soc)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    prices = (
        [make_price(0, 0.01)]
        + [make_price(h, 0.10) for h in range(1, 12)]
        + [make_price(12, 0.50)]
        + [make_price(h, 0.10) for h in range(13, 24)]
    )
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series([])
    calc = calculate(ps, gen, state, NOW)
    profile = _flat_baseload(0.2)

    result = optimize_schedule(
        ps, calc, bc, state, deg, NOW,
        base_load_profile=profile, local_tz=timezone.utc,
    )
    slot_0 = next(s for s in result.slots if s.start.hour == 0)
    slot_12 = next(s for s in result.slots if s.start.hour == 12)
    assert slot_0.mode == StorageMode.GULP
    assert slot_12.mode == StorageMode.DUMP


def test_baseload_omitted_still_produces_schedule():
    """Calling without BaseLoadProfile / local_tz uses zero baseload silently."""
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(bc.min_soc)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    prices = [make_price(h, 0.10) for h in range(4)]
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series([])
    calc = calculate(ps, gen, state, NOW)
    result = optimize_schedule(ps, calc, bc, state, deg, NOW)
    assert len(result.slots) == 4


# ---------------------------------------------------------------------------
# Terminal value, profitability tilt, mode-change penalty (phase 4)
# ---------------------------------------------------------------------------


def _profit_score(score: float) -> ProfitabilityScore:
    """Build a ProfitabilityScore with the given score and neutral metadata."""
    return ProfitabilityScore(
        score=score,
        today_peak_eur_kwh=0.5,
        today_class=DayClass.WEEKDAY,
        class_medians={DayClass.WEEKDAY: 0.4},
        window_days=30,
        computed_at=NOW,
    )


def test_terminal_value_discourages_unnecessary_drain():
    """At soc=0.5, flat positive prices: terminal value keeps the DP from
    DUMPing the whole pack for tiny gains; charge is preserved."""
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(0.50)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    prices = [make_price(h, 0.10) for h in range(24)]    # flat
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series([])
    calc = calculate(ps, gen, state, NOW)

    result = optimize_schedule(ps, calc, bc, state, deg, NOW)
    # With terminal value, the final SoC should be close to the starting SoC —
    # not drained to min_soc as it would be without look-ahead.
    final_soc = result.slots[-1].expected_soc_after
    assert final_soc >= 0.30, (
        f"Terminal value should prevent full drain; final SoC={final_soc}"
    )


def test_profitability_low_score_boosts_end_soc_value():
    """A low profitability score should make the DP retain more charge.

    Concretely: same prices, low score should leave higher end-SoC than high
    score.
    """
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(0.50)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    prices = [make_price(h, 0.10) for h in range(12)]
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series([])
    calc = calculate(ps, gen, state, NOW)

    low = optimize_schedule(
        ps, calc, bc, state, deg, NOW, profitability_score=_profit_score(0.05),
    )
    high = optimize_schedule(
        ps, calc, bc, state, deg, NOW, profitability_score=_profit_score(0.95),
    )
    assert low.slots[-1].expected_soc_after >= high.slots[-1].expected_soc_after


def test_mode_change_penalty_dampens_flapping():
    """With the mode-change penalty, the schedule should change modes less often."""
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(0.50)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    # Alternating price spread small enough to be marginal — without penalty the
    # DP might flap every other slot; with penalty, it consolidates.
    prices = [make_price(h, 0.10 if h % 2 == 0 else 0.12) for h in range(24)]
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series([])
    calc = calculate(ps, gen, state, NOW)

    no_penalty = optimize_schedule(
        ps, calc, bc, state, deg, NOW, mode_change_penalty=0.0,
    )
    with_penalty = optimize_schedule(
        ps, calc, bc, state, deg, NOW, mode_change_penalty=0.05,
    )
    changes_off = sum(
        1 for i in range(1, len(no_penalty.slots))
        if no_penalty.slots[i].mode != no_penalty.slots[i - 1].mode
    )
    changes_on = sum(
        1 for i in range(1, len(with_penalty.slots))
        if with_penalty.slots[i].mode != with_penalty.slots[i - 1].mode
    )
    assert changes_on <= changes_off


def test_current_mode_prevents_first_slot_penalty():
    """When current_mode matches the chosen first-slot mode, no penalty fires.

    Concretely: starting in STORE with a price profile that picks STORE first
    yields the same first-slot reward as starting in None (the sentinel).
    """
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(bc.min_soc)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    prices = [make_price(h, 0.10) for h in range(4)]    # no GULP/DUMP attractive
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series([])
    calc = calculate(ps, gen, state, NOW)

    no_prev = optimize_schedule(ps, calc, bc, state, deg, NOW)
    with_prev = optimize_schedule(
        ps, calc, bc, state, deg, NOW,
        current_mode=no_prev.slots[0].mode,
    )
    assert no_prev.slots[0].expected_profit_eur == pytest.approx(
        with_prev.slots[0].expected_profit_eur,
    )


def test_charging_profile_argument_is_accepted_but_ignored():
    """The DP does not consume ChargingProfile; passing one must not error."""
    from custom_components.sun_sale.contract.models import (
        ChargeMode,
        ChargingProfile,
        ChargingProfileSlot,
    )

    prices = [make_price(h, 0.10) for h in range(4)]
    solar = [make_solar(2, 2.0)]
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(0.50)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series(solar)
    calc = calculate(ps, gen, state, NOW)
    solar_start = solar[0].start
    profile = ChargingProfile(
        slots=(
            ChargingProfileSlot(
                start=solar_start,
                end=solar_start + timedelta(hours=1),
                mode=ChargeMode.NO_EXPORT,
                expected_kwh=2.0,
                sell_eur_kwh=-0.01,
            ),
        ),
        free_capacity_kwh=5.0,
        today_remaining_generation_kwh=2.0,
        solar_exceeds_capacity=False,
        allocated_solar_kwh=2.0,
        total_no_export_kwh=2.0,
        computed_at=NOW,
    )

    # Should run without error and produce a schedule of the right size.
    result = optimize_schedule(ps, calc, bc, state, deg, NOW, charging_profile=profile)
    assert len(result.slots) == len(ps.slots)
