"""Tests for forecast.py — reads from HA state machine via mocked hass."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from custom_components.sun_sale.forecast import build_generation_series, _tomorrow_entity
from custom_components.sun_sale.models import PriceSeries
from custom_components.sun_sale.pricing import build_price_series
from tests.conftest import BASE_DT, default_tariff_config, make_price

NOW = BASE_DT
SOLAR_ENTITY = "sensor.solar_today"
SOLAR_ENTITY_2 = "sensor.solar2_today"


def _make_config(entity_1=SOLAR_ENTITY, entity_2=""):
    return {
        "solar_forecast_entity": entity_1,
        "solar_forecast_entity_2": entity_2,
    }


def _empty_price_series() -> PriceSeries:
    prices = [make_price(h, 0.10) for h in range(24)]
    return build_price_series(prices, default_tariff_config(), now=NOW)


def _hass_with_state(entity_id: str, attrs: dict) -> MagicMock:
    hass = MagicMock()
    state = MagicMock()
    state.attributes = attrs
    hass.states.get.side_effect = lambda eid: state if eid == entity_id else None
    return hass


# ---------------------------------------------------------------------------
# Empty / missing cases
# ---------------------------------------------------------------------------

def test_empty_when_no_entity_configured():
    hass = MagicMock()
    gen = build_generation_series(hass, _make_config(entity_1=""), _empty_price_series(), now=NOW)
    assert gen.slots == ()
    assert gen.primary == "none"


def test_empty_when_entity_missing():
    hass = MagicMock()
    hass.states.get.return_value = None
    gen = build_generation_series(hass, _make_config(), _empty_price_series(), now=NOW)
    assert gen.slots == ()


# ---------------------------------------------------------------------------
# Open Meteo (watts attribute)
# ---------------------------------------------------------------------------

def _make_watts_state(watts_dict: dict) -> MagicMock:
    s = MagicMock()
    s.attributes = {"watts": watts_dict}
    return s


def test_open_meteo_watts_parsed():
    ts_1 = "2024-01-15T10:00:00+00:00"
    ts_2 = "2024-01-15T11:00:00+00:00"
    hass = MagicMock()
    hass.states.get.side_effect = lambda eid: (
        _make_watts_state({ts_1: 2000.0, ts_2: 3000.0}) if eid == SOLAR_ENTITY else None
    )
    gen = build_generation_series(hass, _make_config(), _empty_price_series(), now=NOW)
    assert gen.primary == "open_meteo"
    starts = {s.start.hour for s in gen.slots}
    assert 10 in starts
    assert 11 in starts


def test_open_meteo_15min_aggregated_to_hourly():
    # Four 15-min slots at 1000 W each → 1.0 kWh per hour
    base = "2024-01-15T10:"
    watts = {f"{base}{m:02d}:00+00:00": 1000.0 for m in (0, 15, 30, 45)}
    hass = MagicMock()
    hass.states.get.side_effect = lambda eid: (
        _make_watts_state(watts) if eid == SOLAR_ENTITY else None
    )
    gen = build_generation_series(hass, _make_config(), _empty_price_series(), now=NOW)
    slot_10 = next(s for s in gen.slots if s.start.hour == 10)
    assert abs(slot_10.expected_kwh - 1.0) < 1e-6


def test_open_meteo_two_arrays_summed():
    ts = "2024-01-15T10:00:00+00:00"
    hass = MagicMock()
    hass.states.get.side_effect = lambda eid: (
        _make_watts_state({ts: 1000.0}) if eid in (SOLAR_ENTITY, SOLAR_ENTITY_2) else None
    )
    gen = build_generation_series(
        hass, _make_config(entity_1=SOLAR_ENTITY, entity_2=SOLAR_ENTITY_2),
        _empty_price_series(), now=NOW,
    )
    slot_10 = next(s for s in gen.slots if s.start.hour == 10)
    # Each array contributes 1000 W × 0.25 h / 1000 = 0.25 kWh → total 0.5 kWh
    assert abs(slot_10.expected_kwh - 0.5) < 1e-6


# ---------------------------------------------------------------------------
# Forecast.Solar / Solcast fallback
# ---------------------------------------------------------------------------

def test_forecast_solar_pv_estimate_parsed():
    hass = _hass_with_state(SOLAR_ENTITY, {
        "forecast": [
            {"time": "2024-01-15T10:00:00", "pv_estimate": 1.5},
            {"time": "2024-01-15T11:00:00", "pv_estimate": 2.0},
        ]
    })
    gen = build_generation_series(hass, _make_config(), _empty_price_series(), now=NOW)
    assert gen.primary == "forecast_solar"
    assert len(gen.slots) == 2
    assert abs(gen.slots[0].expected_kwh - 1.5) < 1e-9
    assert abs(gen.slots[1].expected_kwh - 2.0) < 1e-9


def test_forecast_solar_energy_fallback():
    hass = _hass_with_state(SOLAR_ENTITY, {
        "forecast": [{"time": "2024-01-15T10:00:00", "energy": 1.2}]
    })
    gen = build_generation_series(hass, _make_config(), _empty_price_series(), now=NOW)
    assert abs(gen.slots[0].expected_kwh - 1.2) < 1e-9


def test_forecast_solar_skips_bad_entries():
    hass = _hass_with_state(SOLAR_ENTITY, {
        "forecast": [
            {"time": "bad-date", "pv_estimate": 1.0},
            {"time": "2024-01-15T10:00:00", "pv_estimate": 2.0},
        ]
    })
    gen = build_generation_series(hass, _make_config(), _empty_price_series(), now=NOW)
    assert len(gen.slots) == 1


# ---------------------------------------------------------------------------
# multi-source: primary selection and overlays
# ---------------------------------------------------------------------------

def test_primary_is_set():
    hass = _hass_with_state(SOLAR_ENTITY, {
        "forecast": [{"time": "2024-01-15T10:00:00", "pv_estimate": 1.0}]
    })
    gen = build_generation_series(hass, _make_config(), _empty_price_series(), now=NOW)
    assert gen.primary in ("open_meteo", "forecast_solar")


# ---------------------------------------------------------------------------
# energy_between helper
# ---------------------------------------------------------------------------

def test_energy_between_full_slot():
    hass = _hass_with_state(SOLAR_ENTITY, {
        "forecast": [{"time": "2024-01-15T10:00:00", "pv_estimate": 3.0}]
    })
    gen = build_generation_series(hass, _make_config(), _empty_price_series(), now=NOW)
    t1 = NOW.replace(hour=10)
    t2 = NOW.replace(hour=11)
    assert abs(gen.energy_between(t1, t2) - 3.0) < 1e-9


def test_energy_between_partial_slot():
    hass = _hass_with_state(SOLAR_ENTITY, {
        "forecast": [{"time": "2024-01-15T10:00:00", "pv_estimate": 4.0}]
    })
    gen = build_generation_series(hass, _make_config(), _empty_price_series(), now=NOW)
    t1 = NOW.replace(hour=10, minute=30)
    t2 = NOW.replace(hour=11)
    # 30 min of a 60-min slot → 50%
    assert abs(gen.energy_between(t1, t2) - 2.0) < 1e-6


def test_energy_between_no_overlap():
    hass = _hass_with_state(SOLAR_ENTITY, {
        "forecast": [{"time": "2024-01-15T10:00:00", "pv_estimate": 3.0}]
    })
    gen = build_generation_series(hass, _make_config(), _empty_price_series(), now=NOW)
    t1 = NOW.replace(hour=12)
    t2 = NOW.replace(hour=13)
    assert gen.energy_between(t1, t2) == 0.0


# ---------------------------------------------------------------------------
# _tomorrow_entity helper
# ---------------------------------------------------------------------------

def test_tomorrow_entity_today_suffix():
    assert _tomorrow_entity("sensor.energy_production_today") == "sensor.energy_production_tomorrow"


def test_tomorrow_entity_today_infix():
    assert _tomorrow_entity("sensor.energy_production_today_2") == "sensor.energy_production_tomorrow_2"


def test_tomorrow_entity_no_today():
    assert _tomorrow_entity("sensor.energy_production") == ""
