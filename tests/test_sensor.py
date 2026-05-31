"""Tests for sensor.py — native_value properties for all sensor entities."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from custom_components.sun_sale.contract.models import (
    Schedule,
    ScheduleSlot,
    StorageMode,
    TariffResult,
)
from custom_components.sun_sale.inbound.pricing import build_price_series
from custom_components.sun_sale.sensor import (
    CurrentActionSensor,
    CurrentBuyPriceSensor,
    CurrentSellPriceSensor,
    DegradationCostSensor,
    EstimatedCapacitySensor,
    ExpectedProfitSensor,
    NextActionSensor,
)
from tests.conftest import default_tariff_config, make_price

BASE = datetime(2024, 1, 15, 0, 0, tzinfo=timezone.utc)


def make_slot(hour: int, mode: StorageMode = StorageMode.AUTO, profit: float = 0.0) -> ScheduleSlot:
    start = BASE.replace(hour=hour)
    return ScheduleSlot(
        start=start,
        end=start + timedelta(hours=1),
        mode=mode,
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


def make_tariff(hour: int, buy: float = 0.12, sell: float = 0.06) -> TariffResult:
    return TariffResult(hour=BASE.replace(hour=hour), spot_price=0.08, buy_price=buy, sell_price=sell)


def _make_price_series_for_hour(hour: int, buy_eur_kwh: float = 0.12, sell_eur_kwh: float = 0.06):
    """Create a minimal PriceSeries with one slot matching the given buy/sell prices."""
    from custom_components.sun_sale.contract.models import PriceSlot, PriceSeries
    from datetime import timedelta
    start = BASE.replace(hour=hour)
    slot = PriceSlot(
        start=start, end=start + timedelta(hours=1),
        buy_eur_kwh=buy_eur_kwh, sell_eur_kwh=sell_eur_kwh, spot_eur_kwh=0.08,
        sources=("nordpool", "tariff"),
    )
    return PriceSeries(slots=(slot,), resolution=timedelta(hours=1), computed_at=BASE)


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
    assert sensor.native_value == StorageMode.AUTO.value


def test_current_action_idle_when_empty_schedule():
    schedule = make_schedule([])
    sensor = CurrentActionSensor(make_coord({"schedule": schedule}), make_entry())
    assert sensor.native_value == StorageMode.AUTO.value


def test_current_action_returns_first_slot_mode_as_fallback():
    slots = [make_slot(0, StorageMode.GridCharge), make_slot(1, StorageMode.Discharge)]
    sensor = CurrentActionSensor(make_coord({"schedule": make_schedule(slots)}), make_entry())
    assert sensor.native_value == StorageMode.GridCharge.value


# ---------------------------------------------------------------------------
# NextActionSensor
# ---------------------------------------------------------------------------

def test_next_action_idle_when_no_data():
    sensor = NextActionSensor(make_coord(None), make_entry())
    assert sensor.native_value == StorageMode.AUTO.value


def test_next_action_returns_current_when_no_change():
    slots = [make_slot(0, StorageMode.AUTO), make_slot(1, StorageMode.AUTO)]
    sensor = NextActionSensor(make_coord({"schedule": make_schedule(slots)}), make_entry())
    assert sensor.native_value == StorageMode.AUTO.value


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
                         mode=StorageMode.AUTO, power_kw=0, expected_soc_after=0.5,
                         expected_profit_eur=0.10, reason="")
    slot2 = ScheduleSlot(start=start2, end=start2 + timedelta(hours=1),
                         mode=StorageMode.AUTO, power_kw=0, expected_soc_after=0.5,
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


def test_buy_price_uses_first_slot_as_fallback():
    ps = _make_price_series_for_hour(0, buy_eur_kwh=0.15)
    sensor = CurrentBuyPriceSensor(make_coord({"pricing": ps}), make_entry())
    assert abs(sensor.native_value - 0.15) < 1e-6


def test_sell_price_uses_first_slot_as_fallback():
    ps = _make_price_series_for_hour(0, sell_eur_kwh=0.07)
    sensor = CurrentSellPriceSensor(make_coord({"pricing": ps}), make_entry())
    assert abs(sensor.native_value - 0.07) < 1e-6


def test_sell_price_none_when_no_data():
    sensor = CurrentSellPriceSensor(make_coord(None), make_entry())
    assert sensor.native_value is None


