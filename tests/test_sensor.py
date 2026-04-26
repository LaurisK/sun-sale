"""Tests for sensor.py — native_value properties for all sensor entities."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from custom_components.sun_sale.models import (
    Action,
    EVChargeSlot,
    EVSchedule,
    Schedule,
    ScheduleSlot,
    TariffResult,
)
from custom_components.sun_sale.sensor import (
    CurrentActionSensor,
    CurrentBuyPriceSensor,
    CurrentSellPriceSensor,
    DegradationCostSensor,
    EstimatedCapacitySensor,
    EVChargeCostSensor,
    EVChargingSensor,
    ExpectedProfitSensor,
    NextActionSensor,
)

BASE = datetime(2024, 1, 15, 0, 0, tzinfo=timezone.utc)


def make_slot(hour: int, action: Action = Action.IDLE, profit: float = 0.0) -> ScheduleSlot:
    start = BASE.replace(hour=hour)
    return ScheduleSlot(
        start=start,
        end=start + timedelta(hours=1),
        action=action,
        power_kw=3.0,
        expected_soc_after=0.5,
        expected_profit_eur=profit,
        reason="test",
    )


def make_schedule(slots: list[ScheduleSlot]) -> Schedule:
    return Schedule(
        slots=slots,
        total_expected_profit_eur=sum(s.expected_profit_eur for s in slots),
        degradation_cost_per_kwh=0.02,
        computed_at=BASE,
    )


def make_ev_schedule(slots: list[EVChargeSlot]) -> EVSchedule:
    return EVSchedule(
        slots=slots,
        total_cost_eur=sum(s.cost_eur for s in slots),
        total_energy_kwh=sum(s.charge_power_kw for s in slots),
        computed_at=BASE,
    )


def make_ev_slot(hour: int, power: float, cost: float) -> EVChargeSlot:
    start = BASE.replace(hour=hour)
    return EVChargeSlot(
        start=start,
        end=start + timedelta(hours=1),
        charge_power_kw=power,
        cost_eur=cost,
    )


def make_tariff(hour: int, buy: float = 0.12, sell: float = 0.06) -> TariffResult:
    return TariffResult(hour=BASE.replace(hour=hour), spot_price=0.08, buy_price=buy, sell_price=sell)


def make_coord(data: dict | None = None) -> MagicMock:
    coord = MagicMock()
    coord.data = data
    return coord


def make_entry(entry_id: str = "entry1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


# ---------------------------------------------------------------------------
# CurrentActionSensor
# ---------------------------------------------------------------------------

def test_current_action_idle_when_no_data():
    sensor = CurrentActionSensor(make_coord(None), make_entry())
    assert sensor.native_value == Action.IDLE.value


def test_current_action_idle_when_empty_schedule():
    schedule = make_schedule([])
    sensor = CurrentActionSensor(make_coord({"schedule": schedule}), make_entry())
    assert sensor.native_value == Action.IDLE.value


def test_current_action_returns_first_slot_action_as_fallback():
    slots = [make_slot(0, Action.CHARGE_FROM_GRID), make_slot(1, Action.DISCHARGE_TO_GRID)]
    sensor = CurrentActionSensor(make_coord({"schedule": make_schedule(slots)}), make_entry())
    assert sensor.native_value == Action.CHARGE_FROM_GRID.value


# ---------------------------------------------------------------------------
# NextActionSensor
# ---------------------------------------------------------------------------

def test_next_action_idle_when_no_data():
    sensor = NextActionSensor(make_coord(None), make_entry())
    assert sensor.native_value == Action.IDLE.value


def test_next_action_returns_current_when_no_change():
    slots = [make_slot(0, Action.IDLE), make_slot(1, Action.IDLE)]
    sensor = NextActionSensor(make_coord({"schedule": make_schedule(slots)}), make_entry())
    assert sensor.native_value == Action.IDLE.value


# ---------------------------------------------------------------------------
# ExpectedProfitSensor
# ---------------------------------------------------------------------------

def test_expected_profit_zero_when_no_data():
    sensor = ExpectedProfitSensor(make_coord(None), make_entry())
    assert sensor.native_value == 0.0


def test_expected_profit_sums_today_slots():
    today = datetime.now(timezone.utc).date()
    start1 = datetime(today.year, today.month, today.day, 10, tzinfo=timezone.utc)
    start2 = datetime(today.year, today.month, today.day, 11, tzinfo=timezone.utc)
    slot1 = ScheduleSlot(start=start1, end=start1 + timedelta(hours=1),
                         action=Action.IDLE, power_kw=0, expected_soc_after=0.5,
                         expected_profit_eur=0.10, reason="")
    slot2 = ScheduleSlot(start=start2, end=start2 + timedelta(hours=1),
                         action=Action.IDLE, power_kw=0, expected_soc_after=0.5,
                         expected_profit_eur=0.20, reason="")
    schedule = make_schedule([slot1, slot2])
    sensor = ExpectedProfitSensor(make_coord({"schedule": schedule}), make_entry())
    assert abs(sensor.native_value - 0.30) < 1e-6


# ---------------------------------------------------------------------------
# DegradationCostSensor
# ---------------------------------------------------------------------------

def test_degradation_cost_none_when_no_data():
    sensor = DegradationCostSensor(make_coord(None), make_entry())
    assert sensor.native_value is None


def test_degradation_cost_returned():
    sensor = DegradationCostSensor(make_coord({"degradation_cost": 0.041667}), make_entry())
    assert abs(sensor.native_value - 0.041667) < 1e-5


# ---------------------------------------------------------------------------
# EstimatedCapacitySensor
# ---------------------------------------------------------------------------

def test_estimated_capacity_none_when_no_data():
    sensor = EstimatedCapacitySensor(make_coord(None), make_entry())
    assert sensor.native_value is None


def test_estimated_capacity_returned():
    sensor = EstimatedCapacitySensor(make_coord({"estimated_capacity": 9.75}), make_entry())
    assert abs(sensor.native_value - 9.75) < 1e-6


# ---------------------------------------------------------------------------
# CurrentBuyPriceSensor / CurrentSellPriceSensor
# ---------------------------------------------------------------------------

def test_buy_price_none_when_no_data():
    sensor = CurrentBuyPriceSensor(make_coord(None), make_entry())
    assert sensor.native_value is None


def test_buy_price_uses_first_tariff_as_fallback():
    tariffs = [make_tariff(0, buy=0.15), make_tariff(1, buy=0.12)]
    sensor = CurrentBuyPriceSensor(make_coord({"tariffs": tariffs}), make_entry())
    assert abs(sensor.native_value - 0.15) < 1e-6


def test_sell_price_uses_first_tariff_as_fallback():
    tariffs = [make_tariff(0, sell=0.07)]
    sensor = CurrentSellPriceSensor(make_coord({"tariffs": tariffs}), make_entry())
    assert abs(sensor.native_value - 0.07) < 1e-6


def test_sell_price_none_when_no_data():
    sensor = CurrentSellPriceSensor(make_coord(None), make_entry())
    assert sensor.native_value is None


# ---------------------------------------------------------------------------
# EVChargingSensor
# ---------------------------------------------------------------------------

def test_ev_charging_off_when_no_ev_schedule():
    sensor = EVChargingSensor(make_coord({"ev_schedule": None}), make_entry())
    assert sensor.native_value == "off"


def test_ev_charging_off_when_no_data():
    sensor = EVChargingSensor(make_coord(None), make_entry())
    assert sensor.native_value == "off"


# ---------------------------------------------------------------------------
# EVChargeCostSensor
# ---------------------------------------------------------------------------

def test_ev_charge_cost_zero_when_no_ev_schedule():
    sensor = EVChargeCostSensor(make_coord({"ev_schedule": None}), make_entry())
    assert sensor.native_value == 0.0


def test_ev_charge_cost_returns_total():
    ev_schedule = make_ev_schedule([make_ev_slot(10, 7.4, 0.85), make_ev_slot(11, 7.4, 0.82)])
    sensor = EVChargeCostSensor(make_coord({"ev_schedule": ev_schedule}), make_entry())
    assert abs(sensor.native_value - 1.67) < 1e-4
