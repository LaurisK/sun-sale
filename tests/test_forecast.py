"""Tests for forecast.py — pure Python, no HA mocking needed."""
from datetime import datetime, timedelta, timezone

from custom_components.sun_sale.inbound.forecast import build_generation_series
from custom_components.sun_sale.contract.models import PriceSeries, SolarData, SolarEntry
from custom_components.sun_sale.inbound.pricing import build_price_series
from custom_components.sun_sale.inbound.translators import _tomorrow_entity
from tests.conftest import BASE_DT, default_tariff_config, make_price

NOW = BASE_DT


def _empty_price_series() -> PriceSeries:
    prices = [make_price(h, 0.10) for h in range(24)]
    return build_price_series(prices, default_tariff_config(), now=NOW)


def _make_solar_data_from_watts(watts_by_iso: dict[str, float], now=NOW) -> SolarData:
    """Build SolarData from {iso_str: watts} dict."""
    from custom_components.sun_sale.inbound.translators import _watts_to_solar_entries, _make_solar_data
    parsed: dict[datetime, float] = {}
    for ts_str, w in watts_by_iso.items():
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        parsed[dt.astimezone(timezone.utc).replace(second=0, microsecond=0)] = w
    entries = _watts_to_solar_entries(parsed)
    return _make_solar_data(entries, "open_meteo", now)


def _make_solar_data_from_forecast(forecast_slots: list[dict], now=NOW) -> SolarData:
    """Build SolarData from Forecast.Solar-style forecast_slots."""
    entries = []
    for slot in forecast_slots:
        try:
            dt = datetime.fromisoformat(slot["time"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            kwh = float(slot.get("pv_estimate", slot.get("energy", 0.0)))
            entries.append(SolarEntry(start=dt, end=dt + timedelta(hours=1), expected_kwh=kwh, source="forecast_solar"))
        except (KeyError, ValueError):
            continue
    from custom_components.sun_sale.inbound.translators import _make_solar_data
    return _make_solar_data(entries, "forecast_solar" if entries else "none", now)


# ---------------------------------------------------------------------------
# Empty / missing cases
# ---------------------------------------------------------------------------

def test_empty_when_no_data():
    solar = SolarData(entries=[], total_today_kwh=0.0, today_remaining_kwh=0.0, primary_source="none")
    gen = build_generation_series(solar, _empty_price_series(), now=NOW)
    assert gen.slots == ()
    assert gen.primary == "none"


def test_empty_when_empty_forecast_slots():
    solar = SolarData(entries=[], total_today_kwh=0.0, today_remaining_kwh=0.0, primary_source="none")
    gen = build_generation_series(solar, _empty_price_series(), now=NOW)
    assert gen.slots == ()


# ---------------------------------------------------------------------------
# Open Meteo (watts dict)
# ---------------------------------------------------------------------------

def test_open_meteo_watts_parsed():
    solar = _make_solar_data_from_watts({
        "2024-01-15T10:00:00+00:00": 2000.0,
        "2024-01-15T11:00:00+00:00": 3000.0,
    })
    gen = build_generation_series(solar, _empty_price_series(), now=NOW)
    assert gen.primary == "open_meteo"
    starts = {s.start.hour for s in gen.slots}
    assert 10 in starts
    assert 11 in starts


def test_open_meteo_15min_aggregated_to_hourly():
    # Four 15-min slots at 1000 W → each slot = 1000 W * 0.25 h / 1000 = 0.25 kWh → total 1.0 kWh
    solar = _make_solar_data_from_watts({
        "2024-01-15T10:00:00+00:00": 1000.0,
        "2024-01-15T10:15:00+00:00": 1000.0,
        "2024-01-15T10:30:00+00:00": 1000.0,
        "2024-01-15T10:45:00+00:00": 1000.0,
    })
    gen = build_generation_series(solar, _empty_price_series(), now=NOW)
    # Each slot is 0.25 kWh; there are 4 slots in hour 10
    slots_10 = [s for s in gen.slots if s.start.hour == 10]
    total_kwh = sum(s.expected_kwh for s in slots_10)
    assert abs(total_kwh - 1.0) < 1e-5


def test_open_meteo_two_arrays_summed():
    # Simulate two arrays combined: translator already sums → 2000 W total
    solar = _make_solar_data_from_watts({"2024-01-15T10:00:00+00:00": 2000.0})
    gen = build_generation_series(solar, _empty_price_series(), now=NOW)
    slot_10 = next(s for s in gen.slots if s.start.hour == 10)
    # 2000 W × 1 h / 1000 = 2.0 kWh (hourly slot)
    assert abs(slot_10.expected_kwh - 2.0) < 1e-5


# ---------------------------------------------------------------------------
# Forecast.Solar / Solcast fallback
# ---------------------------------------------------------------------------

def test_forecast_solar_pv_estimate_parsed():
    solar = _make_solar_data_from_forecast([
        {"time": "2024-01-15T10:00:00", "pv_estimate": 1.5},
        {"time": "2024-01-15T11:00:00", "pv_estimate": 2.0},
    ])
    gen = build_generation_series(solar, _empty_price_series(), now=NOW)
    assert gen.primary == "forecast_solar"
    assert len(gen.slots) == 2
    assert abs(gen.slots[0].expected_kwh - 1.5) < 1e-9
    assert abs(gen.slots[1].expected_kwh - 2.0) < 1e-9


def test_forecast_solar_energy_fallback():
    solar = _make_solar_data_from_forecast([
        {"time": "2024-01-15T10:00:00", "energy": 1.2},
    ])
    gen = build_generation_series(solar, _empty_price_series(), now=NOW)
    assert abs(gen.slots[0].expected_kwh - 1.2) < 1e-9


def test_forecast_solar_skips_bad_entries():
    solar = _make_solar_data_from_forecast([
        {"time": "bad-date", "pv_estimate": 1.0},
        {"time": "2024-01-15T10:00:00", "pv_estimate": 2.0},
    ])
    gen = build_generation_series(solar, _empty_price_series(), now=NOW)
    assert len(gen.slots) == 1


# ---------------------------------------------------------------------------
# primary selection
# ---------------------------------------------------------------------------

def test_primary_is_set_for_forecast_solar():
    solar = _make_solar_data_from_forecast([
        {"time": "2024-01-15T10:00:00", "pv_estimate": 1.0},
    ])
    gen = build_generation_series(solar, _empty_price_series(), now=NOW)
    assert gen.primary == "forecast_solar"


def test_primary_is_open_meteo_when_watts_present():
    solar = _make_solar_data_from_watts({"2024-01-15T10:00:00+00:00": 1000.0})
    gen = build_generation_series(solar, _empty_price_series(), now=NOW)
    assert gen.primary == "open_meteo"


# ---------------------------------------------------------------------------
# energy_between helper
# ---------------------------------------------------------------------------

def test_energy_between_full_slot():
    solar = _make_solar_data_from_forecast([
        {"time": "2024-01-15T10:00:00", "pv_estimate": 3.0},
    ])
    gen = build_generation_series(solar, _empty_price_series(), now=NOW)
    t1 = NOW.replace(hour=10)
    t2 = NOW.replace(hour=11)
    assert abs(gen.energy_between(t1, t2) - 3.0) < 1e-9


def test_energy_between_partial_slot():
    solar = _make_solar_data_from_forecast([
        {"time": "2024-01-15T10:00:00", "pv_estimate": 4.0},
    ])
    gen = build_generation_series(solar, _empty_price_series(), now=NOW)
    t1 = NOW.replace(hour=10, minute=30)
    t2 = NOW.replace(hour=11)
    assert abs(gen.energy_between(t1, t2) - 2.0) < 1e-6


def test_energy_between_no_overlap():
    solar = _make_solar_data_from_forecast([
        {"time": "2024-01-15T10:00:00", "pv_estimate": 3.0},
    ])
    gen = build_generation_series(solar, _empty_price_series(), now=NOW)
    t1 = NOW.replace(hour=12)
    t2 = NOW.replace(hour=13)
    assert gen.energy_between(t1, t2) == 0.0


# ---------------------------------------------------------------------------
# _tomorrow_entity helper (now in translators.py)
# ---------------------------------------------------------------------------

def test_tomorrow_entity_today_suffix():
    assert _tomorrow_entity("sensor.energy_production_today") == "sensor.energy_production_tomorrow"


def test_tomorrow_entity_today_infix():
    assert _tomorrow_entity("sensor.energy_production_today_2") == "sensor.energy_production_tomorrow_2"


def test_tomorrow_entity_no_today():
    assert _tomorrow_entity("sensor.energy_production") == ""
