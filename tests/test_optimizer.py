"""Tests for optimizer.py — pure Python, no HA required."""
from datetime import datetime, timedelta, timezone
import pytest
from custom_components.sun_sale.battery import degradation_cost_per_kwh
from custom_components.sun_sale.models import Action, SolarForecast
from custom_components.sun_sale.optimizer import _simulate_soc, optimize_schedule
from custom_components.sun_sale.tariff import compute_tariffs
from tests.conftest import (
    BASE_DT,
    default_battery_config,
    default_battery_state,
    default_tariff_config,
    make_price,
    make_solar,
)

NOW = BASE_DT  # 2024-01-15 00:00 UTC


def run(prices, solar=None, soc=0.50, battery_config=None, tariff_config=None):
    bc = battery_config or default_battery_config()
    tc = tariff_config or default_tariff_config()
    tariffs = compute_tariffs(prices, tc)
    state = default_battery_state(soc)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    return optimize_schedule(tariffs, solar or [], bc, state, deg, NOW)


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
