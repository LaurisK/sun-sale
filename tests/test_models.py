"""Tests for models.py — data structure correctness."""
from datetime import datetime, timezone, timedelta
import pytest
from custom_components.sun_sale.models import (
    Action, HourlyPrice, TariffConfig, BatteryConfig, BatteryState,
    SolarForecast, ScheduleSlot, Schedule, EVChargerConfig, EVChargerState,
    EVChargeSlot, EVSchedule, CapacityObservation,
)


NOW = datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)


def test_action_enum_values():
    assert Action.IDLE.value == "idle"
    assert Action.CHARGE_FROM_GRID.value == "charge_from_grid"
    assert Action.DISCHARGE_TO_GRID.value == "discharge_to_grid"
    assert Action.CHARGE_FROM_SOLAR.value == "charge_from_solar"


def test_action_enum_from_value():
    assert Action("idle") == Action.IDLE
    assert Action("charge_from_grid") == Action.CHARGE_FROM_GRID


def test_hourly_price_frozen():
    hp = HourlyPrice(start=NOW, end=NOW + timedelta(hours=1), price_eur_kwh=0.10)
    with pytest.raises((AttributeError, TypeError)):
        hp.price_eur_kwh = 0.20  # type: ignore[misc]


def test_tariff_config_construction():
    tc = TariffConfig(
        distribution_fee=0.03, tax_rate=0.21, markup=0.01,
        sell_distribution_fee=0.02, sell_tax_rate=0.0, sell_markup=0.005,
    )
    assert tc.tax_rate == 0.21


def test_battery_config_frozen():
    bc = BatteryConfig(
        nominal_capacity_kwh=10.0, purchase_price_eur=5000.0,
        rated_cycle_life=6000, max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0, min_soc=0.10, max_soc=0.95,
        round_trip_efficiency=0.90,
    )
    with pytest.raises((AttributeError, TypeError)):
        bc.nominal_capacity_kwh = 20.0  # type: ignore[misc]


def test_battery_state_mutable():
    bs = BatteryState(soc=0.5, estimated_capacity_kwh=10.0)
    bs.soc = 0.6
    assert bs.soc == 0.6


def test_schedule_slot_frozen():
    slot = ScheduleSlot(
        start=NOW, end=NOW + timedelta(hours=1),
        action=Action.IDLE, power_kw=0.0,
        expected_soc_after=0.5, expected_profit_eur=0.0, reason="test",
    )
    with pytest.raises((AttributeError, TypeError)):
        slot.action = Action.CHARGE_FROM_GRID  # type: ignore[misc]


def test_capacity_observation_frozen():
    obs = CapacityObservation(
        timestamp=NOW, soc_start=0.2, soc_end=0.8,
        energy_kwh=5.5, direction="charge",
    )
    with pytest.raises((AttributeError, TypeError)):
        obs.energy_kwh = 6.0  # type: ignore[misc]


def test_ev_charger_state_optional_departure():
    ev = EVChargerState(is_plugged_in=True, soc=0.3, target_soc=0.8)
    assert ev.departure_time is None
