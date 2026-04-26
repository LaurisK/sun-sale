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
# _read_solar_forecast
# ---------------------------------------------------------------------------

def test_read_solar_forecast_empty_when_no_entity_configured():
    coord, hass = make_coordinator(solar_entity="")
    coord._entry.data[CONF_SOLAR_FORECAST_ENTITY] = ""
    assert coord._read_solar_forecast() == []


def test_read_solar_forecast_empty_when_entity_missing():
    coord, hass = make_coordinator()
    hass.states.get.return_value = None
    assert coord._read_solar_forecast() == []


def test_read_solar_forecast_parses_pv_estimate():
    coord, hass = make_coordinator()
    hass.states.get.return_value = _state({
        "forecast": [
            {"time": "2024-01-15T10:00:00", "pv_estimate": 1.5},
            {"time": "2024-01-15T11:00:00", "pv_estimate": 2.0},
        ]
    })
    forecasts = coord._read_solar_forecast()
    assert len(forecasts) == 2
    assert abs(forecasts[0].generation_kwh - 1.5) < 1e-9
    assert abs(forecasts[1].generation_kwh - 2.0) < 1e-9


def test_read_solar_forecast_parses_energy_fallback():
    coord, hass = make_coordinator()
    hass.states.get.return_value = _state({
        "forecast": [{"time": "2024-01-15T10:00:00", "energy": 1.2}]
    })
    forecasts = coord._read_solar_forecast()
    assert len(forecasts) == 1
    assert abs(forecasts[0].generation_kwh - 1.2) < 1e-9


def test_read_solar_forecast_skips_bad_entries():
    coord, hass = make_coordinator()
    hass.states.get.return_value = _state({
        "forecast": [
            {"time": "bad-date", "pv_estimate": 1.0},
            {"time": "2024-01-15T10:00:00", "pv_estimate": 2.0},
        ]
    })
    forecasts = coord._read_solar_forecast()
    assert len(forecasts) == 1


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
