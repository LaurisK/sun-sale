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


# ---------------------------------------------------------------------------
# Observed-series sensors (live / slot-sum / baked-yesterday)
# ---------------------------------------------------------------------------

from custom_components.sun_sale.contract.const import (
    SOURCE_KIND_DEDICATED_SENSOR,
    SOURCE_KIND_SNAPSHOT,
)
from custom_components.sun_sale.contract.models import (
    BakedDayRecord,
    BakedObservedHistory,
    ObservedGenerationSeries,
    ObservedGenerationSlot,
    ObservedGridSeries,
    ObservedGridSlot,
    SlotKwh,
)
from custom_components.sun_sale.sensor import (
    TodayExportedLiveSensor,
    TodayExportedSlotSumSensor,
    TodayGenerationLiveSensor,
    TodayGenerationSlotSumSensor,
    TodayImportedLiveSensor,
    TodayImportedSlotSumSensor,
    YesterdayExportedBakedSensor,
    YesterdayGenerationBakedSensor,
    YesterdayImportedBakedSensor,
)


def _coord_with_local_tz(data: dict | None = None) -> MagicMock:
    """Build a coordinator stub with both .data and the local_tz config field."""
    coord = MagicMock()
    coord.data = data
    coord._sun_sale_config.local_tz = timezone.utc
    return coord


# --- live-counter sensors ---


def test_today_generation_live_returns_counter_kwh():
    """Reads ``today_generation_live_kwh`` straight from coordinator.data."""
    sensor = TodayGenerationLiveSensor(
        _coord_with_local_tz({"today_generation_live_kwh": 12.3456}),
        make_entry(),
    )
    assert sensor.native_value == 12.346


def test_today_generation_live_none_when_unavailable():
    """A missing live key (entity unavailable) returns ``None``."""
    sensor = TodayGenerationLiveSensor(
        _coord_with_local_tz({"today_generation_live_kwh": None}),
        make_entry(),
    )
    assert sensor.native_value is None


def test_today_imported_and_exported_live_use_separate_keys():
    """Each live sensor reads its own key independently."""
    data = {
        "today_imported_live_kwh": 4.5,
        "today_exported_live_kwh": 7.0,
    }
    imp = TodayImportedLiveSensor(_coord_with_local_tz(data), make_entry())
    exp = TodayExportedLiveSensor(_coord_with_local_tz(data), make_entry())
    assert imp.native_value == 4.5
    assert exp.native_value == 7.0


# --- slot-sum (diagnostic) sensors ---


def test_today_generation_slot_sum_reads_observed_total():
    """Slot-sum sensor reflects ``ObservedGenerationSeries.total_today_so_far_kwh``."""
    observed = ObservedGenerationSeries(
        slots=(), computed_at=BASE,
        total_yesterday_kwh=8.0, total_today_so_far_kwh=3.789,
    )
    sensor = TodayGenerationSlotSumSensor(
        _coord_with_local_tz({"observed_generation": observed}), make_entry(),
    )
    assert sensor.native_value == 3.789


def test_today_imported_and_exported_slot_sum_read_grid_totals():
    """Grid slot-sum sensors read import/export totals independently."""
    observed = ObservedGridSeries(
        slots=(), computed_at=BASE,
        total_today_imported_kwh=2.222, total_today_exported_kwh=1.111,
    )
    imp = TodayImportedSlotSumSensor(
        _coord_with_local_tz({"observed_grid": observed}), make_entry(),
    )
    exp = TodayExportedSlotSumSensor(
        _coord_with_local_tz({"observed_grid": observed}), make_entry(),
    )
    assert imp.native_value == 2.222
    assert exp.native_value == 1.111


def test_slot_sum_sensor_none_when_observed_missing():
    """Slot-sum sensors return ``None`` when their observed series is absent."""
    sensor = TodayGenerationSlotSumSensor(
        _coord_with_local_tz({}), make_entry(),
    )
    assert sensor.native_value is None


# --- yesterday baked sensors ---


def _baked_record(side_id: str, date_str: str, baked_sum: float, source_kind: str) -> BakedDayRecord:
    """Construct a BakedDayRecord with minimal slot content."""
    return BakedDayRecord(
        date_str=date_str,
        side_id=side_id,
        counter_total_used=baked_sum,
        source_kind=source_kind,
        baked_slots=(SlotKwh(BASE, BASE + timedelta(hours=1), baked_sum),),
        baked_sum=baked_sum,
        baked_at=BASE,
    )


def test_yesterday_generation_baked_returns_baked_sum():
    """Sensor exposes baked_sum for yesterday's local date."""
    yesterday_str = (
        datetime.now(timezone.utc).astimezone(timezone.utc).date()
        - timedelta(days=1)
    ).isoformat()
    history = BakedObservedHistory(records=(
        _baked_record("generation", yesterday_str, 9.876, SOURCE_KIND_DEDICATED_SENSOR),
    ))
    sensor = YesterdayGenerationBakedSensor(
        _coord_with_local_tz({"baked_observed_history": history}), make_entry(),
    )
    assert sensor.native_value == 9.876


def test_yesterday_baked_exposes_source_kind_attribute():
    """The ``source_kind`` provenance is surfaced as a sensor attribute."""
    yesterday_str = (
        datetime.now(timezone.utc).astimezone(timezone.utc).date()
        - timedelta(days=1)
    ).isoformat()
    history = BakedObservedHistory(records=(
        _baked_record("generation", yesterday_str, 5.0, SOURCE_KIND_SNAPSHOT),
    ))
    sensor = YesterdayGenerationBakedSensor(
        _coord_with_local_tz({"baked_observed_history": history}), make_entry(),
    )
    attrs = sensor.extra_state_attributes
    assert attrs["source_kind"] == SOURCE_KIND_SNAPSHOT
    assert attrs["counter_total_used"] == 5.0
    assert attrs["date"] == yesterday_str


def test_yesterday_baked_none_when_no_record_for_yesterday():
    """No matching record → ``None`` value and empty attributes."""
    history = BakedObservedHistory(records=(
        _baked_record("generation", "1999-12-31", 5.0, SOURCE_KIND_DEDICATED_SENSOR),
    ))
    sensor = YesterdayGenerationBakedSensor(
        _coord_with_local_tz({"baked_observed_history": history}), make_entry(),
    )
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_yesterday_baked_picks_correct_side():
    """A sensor for grid_import never picks up generation's record on the same date."""
    yesterday_str = (
        datetime.now(timezone.utc).astimezone(timezone.utc).date()
        - timedelta(days=1)
    ).isoformat()
    history = BakedObservedHistory(records=(
        _baked_record("generation",  yesterday_str, 10.0, SOURCE_KIND_DEDICATED_SENSOR),
        _baked_record("grid_import", yesterday_str, 2.0,  SOURCE_KIND_SNAPSHOT),
        _baked_record("grid_export", yesterday_str, 3.0,  SOURCE_KIND_SNAPSHOT),
    ))
    coord = _coord_with_local_tz({"baked_observed_history": history})
    assert YesterdayGenerationBakedSensor(coord, make_entry()).native_value == 10.0
    assert YesterdayImportedBakedSensor(coord, make_entry()).native_value == 2.0
    assert YesterdayExportedBakedSensor(coord, make_entry()).native_value == 3.0


def test_yesterday_baked_none_when_no_history():
    """Missing ``baked_observed_history`` key → ``None``."""
    sensor = YesterdayGenerationBakedSensor(
        _coord_with_local_tz({}), make_entry(),
    )
    assert sensor.native_value is None


