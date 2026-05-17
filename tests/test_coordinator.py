"""Tests for coordinator helpers and NordpoolTranslator parsing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from custom_components.sun_sale.orchestration.coordinator import SunSaleCoordinator
from custom_components.sun_sale.inbound.pricing import NordpoolTranslator
from custom_components.sun_sale.contract.models import BatteryReading
from custom_components.sun_sale.contract.const import CONF_NORDPOOL_ENTITY, CONF_SOLAR_FORECAST_ENTITY

BASE = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)


def _make_coordinator():
    hass = MagicMock()
    entry = MagicMock()
    entry.data = {
        CONF_NORDPOOL_ENTITY: "sensor.nordpool",
        CONF_SOLAR_FORECAST_ENTITY: "sensor.solar",
    }
    coord = SunSaleCoordinator(hass, entry)
    coord._config = dict(entry.data)
    return coord, hass


def _make_translator(entity_id: str = "sensor.nordpool"):
    return NordpoolTranslator(entity_id=entity_id)


def _state(attrs: dict) -> MagicMock:
    s = MagicMock()
    s.attributes = attrs
    return s


def _hass_with(entity_id: str, attrs: dict) -> MagicMock:
    hass = MagicMock()
    hass.states.get.side_effect = lambda eid: _state(attrs) if eid == entity_id else None
    return hass


# ---------------------------------------------------------------------------
# NordpoolTranslator.parse — raw_today / raw_tomorrow format
# ---------------------------------------------------------------------------

def test_nordpool_empty_when_entity_missing():
    t = _make_translator()
    hass = MagicMock()
    hass.states.get.return_value = None
    result = t.parse(hass, now=BASE)
    assert result.entries == []


def test_nordpool_parses_raw_today():
    t = _make_translator()
    entry = {"start": "2024-01-15T10:00:00+00:00", "value": 0.10}
    hass = _hass_with("sensor.nordpool", {"raw_today": [entry], "raw_tomorrow": []})
    result = t.parse(hass, now=BASE)
    # At least one entry is parsed; zero-fill may add more for tomorrow
    assert len(result.entries) >= 1
    today_entries = [e for e in result.entries if e.start.date().isoformat() == "2024-01-15"]
    assert len(today_entries) == 1
    assert abs(today_entries[0].price_eur_kwh - 0.10) < 1e-9


def test_nordpool_resolution_detected():
    t = _make_translator()
    entries = [
        {"start": "2024-01-15T10:00:00+00:00", "value": 0.10},
        {"start": "2024-01-15T10:15:00+00:00", "value": 0.11},
    ]
    hass = _hass_with("sensor.nordpool", {"raw_today": entries, "raw_tomorrow": []})
    result = t.parse(hass, now=BASE)
    assert result.resolution == timedelta(minutes=15)


def test_nordpool_deduplicates_slots():
    t = _make_translator()
    dup = {"start": "2024-01-15T10:00:00+00:00", "value": 0.10}
    hass = _hass_with("sensor.nordpool", {"raw_today": [dup, dup], "raw_tomorrow": []})
    result = t.parse(hass, now=BASE)
    # Only one unique today entry; zero-fill may add tomorrow entries
    today_entries = [e for e in result.entries if e.start.date().isoformat() == "2024-01-15"]
    assert len(today_entries) == 1


def test_nordpool_includes_tomorrow():
    t = _make_translator()
    today_e = {"start": "2024-01-15T10:00:00+00:00", "value": 0.10}
    tomorrow_e = {"start": "2024-01-16T10:00:00+00:00", "value": 0.09}
    hass = _hass_with("sensor.nordpool", {"raw_today": [today_e], "raw_tomorrow": [tomorrow_e]})
    result = t.parse(hass, now=BASE)
    assert len(result.entries) == 2


def test_nordpool_slot_duration_15min():
    t = _make_translator()
    entries = [
        {"start": "2024-01-15T10:00:00+00:00", "value": 0.10},
        {"start": "2024-01-15T10:15:00+00:00", "value": 0.11},
    ]
    hass = _hass_with("sensor.nordpool", {"raw_today": entries, "raw_tomorrow": []})
    result = t.parse(hass, now=BASE)
    assert result.entries[0].end == result.entries[0].start + timedelta(minutes=15)


# ---------------------------------------------------------------------------
# NordpoolTranslator.parse — legacy flat list format
# ---------------------------------------------------------------------------

def test_nordpool_legacy_parses_today():
    t = _make_translator()
    hass = _hass_with("sensor.nordpool", {"today": [0.10, 0.12, 0.08], "tomorrow": []})
    result = t.parse(hass, now=BASE)
    today_entries = [e for e in result.entries if e.start.date().isoformat() == "2024-01-15"]
    assert len(today_entries) == 3
    assert abs(today_entries[0].price_eur_kwh - 0.10) < 1e-9


def test_nordpool_legacy_skips_none_entries():
    t = _make_translator()
    hass = _hass_with("sensor.nordpool", {"today": [0.10, None, 0.08], "tomorrow": []})
    result = t.parse(hass, now=BASE)
    today_entries = [e for e in result.entries if e.start.date().isoformat() == "2024-01-15"]
    assert len(today_entries) == 2


def test_nordpool_legacy_slot_spans_one_hour():
    t = _make_translator()
    hass = _hass_with("sensor.nordpool", {"today": [0.10], "tomorrow": []})
    result = t.parse(hass, now=BASE)
    today_entries = [e for e in result.entries if e.start.date().isoformat() == "2024-01-15"]
    assert today_entries[0].end == today_entries[0].start + timedelta(hours=1)


# ---------------------------------------------------------------------------
# _build_capacity_observation
# ---------------------------------------------------------------------------

def _reading(soc: float, power_kw: float = 0.0) -> BatteryReading:
    return BatteryReading(soc=soc, power_kw=power_kw, grid_power_kw=0.0, household_load_kw=0.2)


def test_build_capacity_observation_none_on_first_call():
    coord, _ = _make_coordinator()
    assert coord._build_capacity_observation(_reading(0.5, 1.0), BASE) is None


def test_build_capacity_observation_none_when_small_soc_delta():
    coord, _ = _make_coordinator()
    coord._last_battery_reading = _reading(0.50, 2.0)
    assert coord._build_capacity_observation(_reading(0.52, 2.0), BASE) is None


def test_build_capacity_observation_at_threshold_boundary():
    coord, _ = _make_coordinator()
    coord._last_battery_reading = _reading(0.50, 2.0)
    assert coord._build_capacity_observation(_reading(0.549, 2.0), BASE) is None


def test_build_capacity_observation_charge_direction():
    coord, _ = _make_coordinator()
    coord._last_battery_reading = _reading(0.20, 3.0)
    obs = coord._build_capacity_observation(_reading(0.80, 3.0), BASE)
    assert obs is not None
    assert obs.direction == "charge"
    assert abs(obs.soc_start - 0.20) < 1e-9
    assert abs(obs.soc_end - 0.80) < 1e-9


def test_build_capacity_observation_discharge_direction():
    coord, _ = _make_coordinator()
    coord._last_battery_reading = _reading(0.80, 3.0)
    obs = coord._build_capacity_observation(_reading(0.20, 3.0), BASE)
    assert obs is not None
    assert obs.direction == "discharge"


def test_build_capacity_observation_energy_computed():
    coord, _ = _make_coordinator()
    coord._last_battery_reading = _reading(0.20, 4.0)
    obs = coord._build_capacity_observation(_reading(0.80, 4.0), BASE)
    assert obs is not None
    from custom_components.sun_sale.contract.const import UPDATE_INTERVAL_MINUTES
    expected_energy = 4.0 * (UPDATE_INTERVAL_MINUTES / 60.0)
    assert abs(obs.energy_kwh - expected_energy) < 1e-6


# ---------------------------------------------------------------------------
# Pipeline stage keys in coordinator.data (built manually, no coordinator run)
# ---------------------------------------------------------------------------

def _make_pipeline_data() -> dict:
    from custom_components.sun_sale.contract.models import GenerationSeries
    from custom_components.sun_sale.inbound.pricing import build_price_series
    from custom_components.sun_sale.pipeline.calculator import calculate
    from custom_components.sun_sale.pipeline.battery import degradation_cost_per_kwh
    from custom_components.sun_sale.pipeline.optimizer import optimize_schedule
    from tests.conftest import default_battery_config, default_battery_state, default_tariff_config, make_price

    now = BASE
    prices = [make_price(h, 0.10) for h in range(4)]
    ps = build_price_series(prices, default_tariff_config(), now=now)
    gen = GenerationSeries(slots=(), primary="none", overlays=(), computed_at=now)
    bs = default_battery_state()
    calc = calculate(ps, gen, bs, now)
    bc = default_battery_config()
    deg = degradation_cost_per_kwh(bc, bs)
    schedule = optimize_schedule(ps, calc, bc, bs, deg, now)
    return {"pricing": ps, "forecast": gen, "calculation": calc, "schedule": schedule}


def test_pipeline_keys_present_in_coordinator_data():
    data = _make_pipeline_data()
    for key in ("pricing", "forecast", "calculation", "schedule"):
        assert key in data


def test_pipeline_pricing_is_price_series():
    from custom_components.sun_sale.contract.models import PriceSeries
    assert isinstance(_make_pipeline_data()["pricing"], PriceSeries)


def test_pipeline_forecast_is_generation_series():
    from custom_components.sun_sale.contract.models import GenerationSeries
    assert isinstance(_make_pipeline_data()["forecast"], GenerationSeries)


def test_pipeline_calculation_is_calculation_result():
    from custom_components.sun_sale.contract.models import CalculationResult
    assert isinstance(_make_pipeline_data()["calculation"], CalculationResult)


def test_pipeline_schedule_is_schedule():
    from custom_components.sun_sale.contract.models import Schedule
    assert isinstance(_make_pipeline_data()["schedule"], Schedule)
