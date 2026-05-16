"""Tests for optimizer.py — pure Python, no HA required."""
from datetime import datetime, timedelta, timezone
import pytest
from custom_components.sun_sale.pipeline.battery import degradation_cost_per_kwh
from custom_components.sun_sale.pipeline.calculator import calculate
from custom_components.sun_sale.contract.models import Action, GenerationSeries, GenerationSlot, SolarForecast
from custom_components.sun_sale.pipeline.optimizer import _simulate_soc, optimize_schedule
from custom_components.sun_sale.inbound.pricing import build_price_series
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
    slots = tuple(
        GenerationSlot(start=sf.start, end=sf.end, expected_kwh=sf.generation_kwh,
                       source="forecast_solar", confidence=None)
        for sf in solar
    )
    return GenerationSeries(slots=slots, primary="forecast_solar", overlays=(), computed_at=NOW)


def run(prices, solar=None, soc=0.50, battery_config=None, tariff_config=None):
    bc = battery_config or default_battery_config()
    tc = tariff_config or default_tariff_config()
    state = default_battery_state(soc)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    price_series = build_price_series(prices, tc, now=NOW)
    gen_series = _make_gen_series(solar or [])
    calc = calculate(price_series, gen_series, state, None, NOW)
    return optimize_schedule(price_series, calc, bc, state, deg, NOW)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_tariffs_returns_empty_schedule():
    result = run([])
    assert result.slots == []
    assert result.total_expected_profit_eur == 0.0


def test_single_price_results_in_idle():
    result = run([make_price(0, 0.10)])
    assert len(result.slots) == 1
    assert result.slots[0].action == Action.IDLE


def test_all_same_price_no_trade():
    prices = [make_price(h, 0.10) for h in range(24)]
    result = run(prices)
    # Flat prices → no spread → all idle
    assert all(s.action == Action.IDLE for s in result.slots)


# ---------------------------------------------------------------------------
# Core optimisation
# ---------------------------------------------------------------------------

def test_obvious_buy_low_sell_high():
    prices = [make_price(0, 0.01)] + [make_price(h, 0.10) for h in range(1, 12)] + [make_price(12, 0.50)] + [make_price(h, 0.10) for h in range(13, 24)]
    result = run(prices)
    slot_0 = next(s for s in result.slots if s.start.hour == 0)
    slot_12 = next(s for s in result.slots if s.start.hour == 12)
    assert slot_0.action == Action.CHARGE_FROM_GRID
    assert slot_12.action == Action.DISCHARGE_TO_GRID


def test_spread_below_degradation_stays_idle():
    # Degradation cost ~= 5000/(6000*10*2) = 0.0417 EUR/kWh
    # Spread of 0.02 EUR → not profitable after degradation + efficiency loss
    prices = [make_price(h, 0.10 if h < 12 else 0.12) for h in range(24)]
    result = run(prices)
    assert all(s.action == Action.IDLE for s in result.slots)


def test_positive_profit_produces_trades():
    prices = [make_price(0, 0.01)] + [make_price(h, 0.10) for h in range(1, 23)] + [make_price(23, 0.50)]
    result = run(prices)
    charge_slots = [s for s in result.slots if s.action == Action.CHARGE_FROM_GRID]
    discharge_slots = [s for s in result.slots if s.action == Action.DISCHARGE_TO_GRID]
    assert len(charge_slots) >= 1
    assert len(discharge_slots) >= 1


def test_charge_before_discharge():
    """Chronological constraint: buy hour must precede sell hour."""
    prices = [make_price(0, 0.01)] + [make_price(h, 0.50) for h in range(1, 4)] + [make_price(h, 0.10) for h in range(4, 24)]
    result = run(prices)
    charge_slots = [s for s in result.slots if s.action == Action.CHARGE_FROM_GRID]
    discharge_slots = [s for s in result.slots if s.action == Action.DISCHARGE_TO_GRID]
    if charge_slots and discharge_slots:
        assert min(s.start for s in charge_slots) < min(s.start for s in discharge_slots)


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------

def test_respects_max_soc_no_charge_when_full():
    bc = default_battery_config()
    prices = [make_price(0, 0.01)] + [make_price(h, 0.50) for h in range(1, 4)]
    result = run(prices, soc=bc.max_soc, battery_config=bc)
    assert all(s.action != Action.CHARGE_FROM_GRID for s in result.slots)


def test_respects_min_soc_no_discharge_when_empty():
    bc = default_battery_config()
    prices = [make_price(0, 0.50)] + [make_price(h, 0.01) for h in range(1, 4)]
    result = run(prices, soc=bc.min_soc, battery_config=bc)
    assert all(s.action != Action.DISCHARGE_TO_GRID for s in result.slots)


def test_soc_stays_within_bounds_throughout():
    bc = default_battery_config()
    prices = [make_price(h, 0.01 if h < 6 else 0.50) for h in range(24)]
    result = run(prices, soc=0.5, battery_config=bc)
    for slot in result.slots:
        assert bc.min_soc - 1e-6 <= slot.expected_soc_after <= bc.max_soc + 1e-6, (
            f"SoC {slot.expected_soc_after} out of bounds at {slot.start}"
        )


def test_power_does_not_exceed_max():
    bc = default_battery_config()
    prices = [make_price(0, 0.01)] + [make_price(h, 0.10) for h in range(1, 23)] + [make_price(23, 0.50)]
    result = run(prices, battery_config=bc)
    for slot in result.slots:
        if slot.action == Action.CHARGE_FROM_GRID:
            assert slot.power_kw <= bc.max_charge_power_kw + 1e-6
        elif slot.action == Action.DISCHARGE_TO_GRID:
            assert slot.power_kw <= bc.max_discharge_power_kw + 1e-6


# ---------------------------------------------------------------------------
# Solar
# ---------------------------------------------------------------------------

def test_solar_slot_gets_charge_from_solar_action():
    prices = [make_price(h, 0.10) for h in range(4)]
    solar = [make_solar(2, 2.0)]
    result = run(prices, solar=solar)
    solar_slot = next(s for s in result.slots if s.start.hour == 2)
    assert solar_slot.action == Action.CHARGE_FROM_SOLAR


def test_no_solar_no_charge_from_solar():
    prices = [make_price(h, 0.10) for h in range(4)]
    result = run(prices, solar=[])
    assert all(s.action != Action.CHARGE_FROM_SOLAR for s in result.slots)


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------

def test_total_profit_equals_sum_of_slots():
    prices = [make_price(0, 0.01)] + [make_price(h, 0.10) for h in range(1, 23)] + [make_price(23, 0.50)]
    result = run(prices)
    expected = sum(s.expected_profit_eur for s in result.slots)
    assert abs(result.total_expected_profit_eur - expected) < 1e-9


def test_degradation_cost_stored_in_schedule():
    prices = [make_price(h, 0.10) for h in range(4)]
    result = run(prices)
    assert result.degradation_cost_per_kwh > 0


# ---------------------------------------------------------------------------
# _simulate_soc helper
# ---------------------------------------------------------------------------

def test_simulate_soc_within_bounds():
    result = _simulate_soc([0.5, -0.4], 0.5, 10.0, 0.0, 1.0)
    assert result is not None
    assert abs(result[0] - 0.55) < 1e-9
    assert abs(result[1] - 0.51) < 1e-9


def test_simulate_soc_returns_none_on_overflow():
    # Charging 2 kWh into a 10 kWh battery at 0.9 SoC → would exceed 1.0
    result = _simulate_soc([2.0], 0.9, 10.0, 0.0, 1.0)
    assert result is None


def test_simulate_soc_returns_none_on_underflow():
    result = _simulate_soc([-5.0], 0.3, 10.0, 0.0, 1.0)
    assert result is None


# ---------------------------------------------------------------------------
# sell_allowed flag respected
# ---------------------------------------------------------------------------

def _run_with_negative_sell_window(locked_hours: list[int]) -> "Schedule":
    """Run optimizer with some hours marked sell_allowed=False."""
    from custom_components.sun_sale.contract.models import TariffConfig
    from custom_components.sun_sale.pipeline.calculator import calculate
    from custom_components.sun_sale.inbound.pricing import build_price_series

    # High sell fee makes sell_eur_kwh negative for hours with low spot price
    tc = TariffConfig(
        distribution_fee=0.0, tax_rate=0.0, markup=0.0,
        sell_distribution_fee=0.20, sell_tax_rate=0.0, sell_markup=0.0,
    )
    # Low spot in locked hours → negative sell; high elsewhere → cheap buy opportunity
    prices = [make_price(h, 0.05 if h in locked_hours else 0.30) for h in range(24)]
    bc = default_battery_config()
    state = default_battery_state(0.50)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    from custom_components.sun_sale.pipeline.battery import degradation_cost_per_kwh
    deg = degradation_cost_per_kwh(bc, state)

    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series([])
    calc = calculate(ps, gen, state, None, NOW)
    return optimize_schedule(ps, calc, bc, state, deg, NOW)


def test_no_discharge_inside_lockout_window():
    locked = list(range(10, 14))
    result = _run_with_negative_sell_window(locked)
    discharge_slots = [s for s in result.slots if s.action == Action.DISCHARGE_TO_GRID]
    for slot in discharge_slots:
        assert slot.start.hour not in locked, (
            f"DISCHARGE_TO_GRID at hour {slot.start.hour} is inside a locked-out window"
        )


def test_discharge_allowed_outside_lockout_window():
    """Only hour 12 locked; a profitable trade can still occur at other hours."""
    from custom_components.sun_sale.contract.models import TariffConfig
    from custom_components.sun_sale.pipeline.calculator import calculate
    from custom_components.sun_sale.inbound.pricing import build_price_series
    from custom_components.sun_sale.pipeline.battery import degradation_cost_per_kwh

    # Zero fees so buy ≈ spot, sell ≈ spot; gives large spread
    tc = TariffConfig(
        distribution_fee=0.0, tax_rate=0.0, markup=0.0,
        sell_distribution_fee=0.20, sell_tax_rate=0.0, sell_markup=0.0,
    )
    # Hour 12: spot=0.05 → sell=0.05-0.20=-0.15 (locked); spot=0.01 elsewhere (cheap buy)
    # Hour 23: spot=0.80 → sell=0.80-0.20=0.60 (very profitable sell)
    prices = (
        [make_price(0, 0.01)]
        + [make_price(h, 0.10) for h in range(1, 12)]
        + [make_price(12, 0.05)]     # locked-out (sell negative)
        + [make_price(h, 0.10) for h in range(13, 23)]
        + [make_price(23, 0.80)]     # high sell price
    )
    bc = default_battery_config()
    state = default_battery_state(0.50)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)

    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series([])
    calc = calculate(ps, gen, state, None, NOW)
    result = optimize_schedule(ps, calc, bc, state, deg, NOW)

    discharge_slots = [s for s in result.slots if s.action == Action.DISCHARGE_TO_GRID]
    assert any(s.start.hour != 12 for s in discharge_slots), (
        "Expected at least one DISCHARGE_TO_GRID slot outside the locked hour"
    )
    assert all(s.start.hour != 12 for s in discharge_slots), (
        "No DISCHARGE_TO_GRID slot should be at the locked hour 12"
    )
