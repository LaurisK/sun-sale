"""Tests for ev_scheduler.py — pure Python, no HA required."""
from datetime import datetime, timedelta, timezone
import pytest
from custom_components.sun_sale.pipeline.ev_scheduler import _cheapest_slots, schedule_ev_charge
from custom_components.sun_sale.contract.models import EVChargerConfig, EVChargerState
from custom_components.sun_sale.inbound.pricing import build_price_series
from tests.conftest import BASE_DT, default_ev_config, default_tariff_config, make_price

NOW = BASE_DT  # 2024-01-15 00:00 UTC


def make_price_series(prices):
    return build_price_series(prices, default_tariff_config(), now=NOW)


def ev_state(plugged=True, soc=0.3, target=0.8, departure=None):
    return EVChargerState(
        is_plugged_in=plugged, soc=soc, target_soc=target, departure_time=departure
    )


# ---------------------------------------------------------------------------
# Not plugged in / already charged
# ---------------------------------------------------------------------------

def test_not_plugged_in_returns_empty():
    ps = make_price_series([make_price(h, 0.10) for h in range(8)])
    result = schedule_ev_charge(ps, default_ev_config(), ev_state(plugged=False), NOW)
    assert result.slots == []
    assert result.total_cost_eur == 0.0
    assert result.total_energy_kwh == 0.0


def test_already_at_target_returns_empty():
    ps = make_price_series([make_price(h, 0.10) for h in range(8)])
    result = schedule_ev_charge(ps, default_ev_config(), ev_state(soc=0.8, target=0.8), NOW)
    assert result.slots == []


def test_soc_above_target_returns_empty():
    ps = make_price_series([make_price(h, 0.10) for h in range(8)])
    result = schedule_ev_charge(ps, default_ev_config(), ev_state(soc=0.9, target=0.8), NOW)
    assert result.slots == []


# ---------------------------------------------------------------------------
# Cheapest slot selection
# ---------------------------------------------------------------------------

def test_cheapest_slots_selected():
    # Hours 3 and 4 are cheapest
    ps = make_price_series([make_price(h, 0.02 if h in (3, 4) else 0.15) for h in range(8)])
    result = schedule_ev_charge(ps, default_ev_config(), ev_state(soc=0.55, target=0.80), NOW)
    charging = [s for s in result.slots if s.charge_power_kw > 0]
    charging_hours = {s.start.hour for s in charging}
    assert 3 in charging_hours
    assert 4 in charging_hours


def test_cheapest_slots_helper_returns_n():
    ps = make_price_series([make_price(h, float(h)) for h in range(10)])
    result = _cheapest_slots(list(ps.slots), 3, NOW, None)
    assert len(result) == 3
    hours = {s.start.hour for s in result}
    assert hours == {0, 1, 2}


def test_cheapest_slots_respects_end():
    ps = make_price_series([make_price(h, 0.10) for h in range(10)])
    end = NOW.replace(hour=5)
    result = _cheapest_slots(list(ps.slots), 3, NOW, end)
    assert all(s.start.hour < 5 for s in result)


# ---------------------------------------------------------------------------
# Departure constraint
# ---------------------------------------------------------------------------

def test_respects_departure_time():
    # Hours 6–10 are cheapest, but departure is at hour 6
    ps = make_price_series([make_price(h, 0.15 if h < 6 else 0.01) for h in range(12)])
    departure = NOW.replace(hour=6)
    result = schedule_ev_charge(
        ps, default_ev_config(),
        ev_state(soc=0.3, target=0.8, departure=departure), NOW,
    )
    charging = [s for s in result.slots if s.charge_power_kw > 0]
    for slot in charging:
        assert slot.start < departure, f"Charging slot {slot.start} after departure {departure}"


def test_no_departure_uses_full_window():
    ps = make_price_series([make_price(h, 0.10) for h in range(24)])
    result = schedule_ev_charge(ps, default_ev_config(), ev_state(), NOW)
    assert len(result.slots) == 24


# ---------------------------------------------------------------------------
# Energy and cost calculations
# ---------------------------------------------------------------------------

def test_energy_needed_calculation():
    # target=0.8, soc=0.2, capacity=60kWh → 36 kWh needed
    ps = make_price_series([make_price(h, 0.10) for h in range(24)])
    result = schedule_ev_charge(ps, default_ev_config(), ev_state(soc=0.2, target=0.8), NOW)
    assert abs(result.total_energy_kwh - 36.0) < 0.5


def test_slot_cost_equals_power_times_price():
    ps = make_price_series([make_price(h, 0.10 + h * 0.01) for h in range(8)])
    result = schedule_ev_charge(ps, default_ev_config(), ev_state(soc=0.5, target=0.7), NOW)
    for slot in result.slots:
        if slot.charge_power_kw > 0:
            matching = ps.slot_at(slot.start)
            assert matching is not None
            expected_cost = slot.charge_power_kw * matching.buy_eur_kwh
            assert abs(slot.cost_eur - expected_cost) < 1e-9


def test_total_cost_matches_slot_sum():
    ps = make_price_series([make_price(h, 0.10) for h in range(12)])
    result = schedule_ev_charge(ps, default_ev_config(), ev_state(), NOW)
    slot_sum = sum(s.cost_eur for s in result.slots)
    assert abs(result.total_cost_eur - slot_sum) < 1e-9


def test_total_energy_matches_slot_sum():
    ps = make_price_series([make_price(h, 0.10) for h in range(12)])
    result = schedule_ev_charge(ps, default_ev_config(), ev_state(), NOW)
    slot_sum = sum(s.charge_power_kw for s in result.slots)
    assert abs(result.total_energy_kwh - slot_sum) < 1e-9


def test_zero_charge_slots_have_zero_cost():
    ps = make_price_series([make_price(h, 0.10) for h in range(8)])
    result = schedule_ev_charge(ps, default_ev_config(), ev_state(soc=0.5, target=0.6), NOW)
    for slot in result.slots:
        if slot.charge_power_kw == 0.0:
            assert slot.cost_eur == 0.0
