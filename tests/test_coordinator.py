"""Tests for coordinator.py — state reader and helper methods."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from custom_components.sun_sale.coordinator import SunSaleCoordinator
from custom_components.sun_sale.const import CONF_NORDPOOL_ENTITY, CONF_SOLAR_FORECAST_ENTITY

BASE = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)


def make_coordinator(nordpool_entity: str = "sensor.nordpool",
                     solar_entity: str = "sensor.solar"):
    hass = MagicMock()
    entry = MagicMock()
    entry.data = {
        CONF_NORDPOOL_ENTITY: nordpool_entity,
        CONF_SOLAR_FORECAST_ENTITY: solar_entity,
    }
    coord = SunSaleCoordinator(hass, entry)
    coord._config = dict(entry.data)
    return coord, hass


def _state(attrs: dict) -> MagicMock:
    s = MagicMock()
    s.attributes = attrs
    return s


# ---------------------------------------------------------------------------
# _read_nordpool_prices
# ---------------------------------------------------------------------------

def test_read_nordpool_prices_empty_when_entity_missing():
    coord, hass = make_coordinator()
    hass.states.get.return_value = None
    assert coord._read_nordpool_prices() == []


def test_read_nordpool_prices_parses_today():
    coord, hass = make_coordinator()
    hass.states.get.return_value = _state({"today": [0.10, 0.12, 0.08], "tomorrow": []})
    prices = coord._read_nordpool_prices()
    assert len(prices) == 3
    assert abs(prices[0].price_eur_kwh - 0.10) < 1e-9
    assert abs(prices[1].price_eur_kwh - 0.12) < 1e-9
    assert abs(prices[2].price_eur_kwh - 0.08) < 1e-9


def test_read_nordpool_prices_skips_none_entries():
    coord, hass = make_coordinator()
    hass.states.get.return_value = _state({"today": [0.10, None, 0.08], "tomorrow": []})
    prices = coord._read_nordpool_prices()
    assert len(prices) == 2


def test_read_nordpool_prices_includes_tomorrow():
    coord, hass = make_coordinator()
    hass.states.get.return_value = _state({
        "today": [0.10],
        "tomorrow": [0.09],
    })
    prices = coord._read_nordpool_prices()
    assert len(prices) == 2


def test_read_nordpool_prices_slot_spans_one_hour():
    coord, hass = make_coordinator()
    hass.states.get.return_value = _state({"today": [0.10], "tomorrow": []})
    prices = coord._read_nordpool_prices()
    from datetime import timedelta
    assert prices[0].end == prices[0].start + timedelta(hours=1)


# ---------------------------------------------------------------------------
# _build_capacity_observation
# ---------------------------------------------------------------------------

def test_build_capacity_observation_none_on_first_call():
    coord, _ = make_coordinator()
    assert coord._build_capacity_observation(0.5, 1.0, BASE) is None


def test_build_capacity_observation_none_when_small_soc_delta():
    coord, _ = make_coordinator()
    coord._last_battery_soc = 0.50
    coord._last_battery_power = 2.0
    assert coord._build_capacity_observation(0.52, 2.0, BASE) is None


def test_build_capacity_observation_at_threshold_boundary():
    coord, _ = make_coordinator()
    coord._last_battery_soc = 0.50
    coord._last_battery_power = 2.0
    assert coord._build_capacity_observation(0.549, 2.0, BASE) is None


def test_build_capacity_observation_charge_direction():
    coord, _ = make_coordinator()
    coord._last_battery_soc = 0.20
    coord._last_battery_power = 3.0
    obs = coord._build_capacity_observation(0.80, 3.0, BASE)
    assert obs is not None
    assert obs.direction == "charge"
    assert abs(obs.soc_start - 0.20) < 1e-9
    assert abs(obs.soc_end - 0.80) < 1e-9


def test_build_capacity_observation_discharge_direction():
    coord, _ = make_coordinator()
    coord._last_battery_soc = 0.80
    coord._last_battery_power = 3.0
    obs = coord._build_capacity_observation(0.20, 3.0, BASE)
    assert obs is not None
    assert obs.direction == "discharge"


def test_build_capacity_observation_energy_computed():
    coord, _ = make_coordinator()
    coord._last_battery_soc = 0.20
    coord._last_battery_power = 4.0
    obs = coord._build_capacity_observation(0.80, 4.0, BASE)
    assert obs is not None
    from custom_components.sun_sale.const import UPDATE_INTERVAL_MINUTES
    expected_energy = 4.0 * (UPDATE_INTERVAL_MINUTES / 60.0)
    assert abs(obs.energy_kwh - expected_energy) < 1e-6


# ---------------------------------------------------------------------------
# Pipeline stage keys in coordinator.data
# ---------------------------------------------------------------------------

def _make_pipeline_data() -> dict:
    """Build a minimal coordinator.data dict with all four pipeline stage keys."""
    from custom_components.sun_sale.models import (
        CalculationResult, GenerationSeries, PriceSeries, Schedule,
    )
    from custom_components.sun_sale.pricing import build_price_series
    from custom_components.sun_sale.calculator import calculate
    from tests.conftest import default_battery_state, default_tariff_config, make_price
    now = BASE
    prices = [make_price(h, 0.10) for h in range(4)]
    tc = default_tariff_config()
    ps = build_price_series(prices, tc, now=now)
    gen = GenerationSeries(slots=(), primary="none", overlays=(), computed_at=now)
    bs = default_battery_state()
    calc = calculate(ps, gen, bs, None, now)
    from custom_components.sun_sale.battery import degradation_cost_per_kwh
    from custom_components.sun_sale.optimizer import optimize_schedule
    from tests.conftest import default_battery_config
    bc = default_battery_config()
    deg = degradation_cost_per_kwh(bc, bs)
    schedule = optimize_schedule(ps, calc, bc, bs, deg, now)
    return {
        "pricing": ps,
        "forecast": gen,
        "calculation": calc,
        "schedule": schedule,
    }


def test_pipeline_keys_present_in_coordinator_data():
    data = _make_pipeline_data()
    for key in ("pricing", "forecast", "calculation", "schedule"):
        assert key in data, f"missing pipeline key: {key}"


def test_pipeline_pricing_is_price_series():
    from custom_components.sun_sale.models import PriceSeries
    data = _make_pipeline_data()
    assert isinstance(data["pricing"], PriceSeries)


def test_pipeline_forecast_is_generation_series():
    from custom_components.sun_sale.models import GenerationSeries
    data = _make_pipeline_data()
    assert isinstance(data["forecast"], GenerationSeries)


def test_pipeline_calculation_is_calculation_result():
    from custom_components.sun_sale.models import CalculationResult
    data = _make_pipeline_data()
    assert isinstance(data["calculation"], CalculationResult)


def test_pipeline_schedule_is_schedule():
    from custom_components.sun_sale.models import Schedule
    data = _make_pipeline_data()
    assert isinstance(data["schedule"], Schedule)
