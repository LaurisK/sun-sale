"""Tests for config_flow.py — SunSaleConfigFlow validation and step routing."""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.sun_sale.config_flow import SunSaleConfigFlow
from custom_components.sun_sale.contract.const import (
    CONF_BATTERY_NOMINAL_CAPACITY,
    CONF_BATTERY_PURCHASE_PRICE,
    CONF_BATTERY_RATED_CYCLE_LIFE,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_INVERTER_PLATFORM,
    CONF_NORDPOOL_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLIS_CONFIG_ENTRY_ID,
    CONF_TARIFF_DISTRIBUTION_FEE,
    CONF_TARIFF_MARKUP,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE,
    CONF_TARIFF_SELL_MARKUP,
    CONF_TARIFF_SELL_TAX_RATE,
    CONF_TARIFF_TAX_RATE,
)
from custom_components.sun_sale.outbound.inverter import InverterPlatform

VALID_TARIFF_INPUT = {
    CONF_TARIFF_DISTRIBUTION_FEE: 0.03,
    CONF_TARIFF_TAX_RATE: 21.0,
    CONF_TARIFF_MARKUP: 0.005,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE: 0.01,
    CONF_TARIFF_SELL_TAX_RATE: 0.0,
    CONF_TARIFF_SELL_MARKUP: 0.0,
}

VALID_BATTERY_INPUT = {
    CONF_BATTERY_NOMINAL_CAPACITY: 10.0,
    CONF_BATTERY_PURCHASE_PRICE: 5000.0,
    CONF_BATTERY_RATED_CYCLE_LIFE: 6000,
    CONF_BATTERY_MAX_CHARGE_POWER: 5.0,
    CONF_BATTERY_MAX_DISCHARGE_POWER: 5.0,
    CONF_BATTERY_MIN_SOC: 10,
    CONF_BATTERY_MAX_SOC: 95,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY: 90,
    CONF_BATTERY_NOMINAL_VOLTAGE: 48.0,
}


# ---------------------------------------------------------------------------
# async_step_user — form and validation
# ---------------------------------------------------------------------------

async def test_step_user_no_input_shows_form():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_user(None)
    assert result["step_id"] == "user"
    assert result.get("errors", {}) == {}


async def test_step_user_valid_input_proceeds_to_battery():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_user(VALID_TARIFF_INPUT)
    assert result["step_id"] == "battery"


async def test_step_user_negative_fee_returns_error():
    flow = SunSaleConfigFlow()
    bad = {**VALID_TARIFF_INPUT, CONF_TARIFF_DISTRIBUTION_FEE: -0.01}
    result = await flow.async_step_user(bad)
    assert result["step_id"] == "user"
    assert CONF_TARIFF_DISTRIBUTION_FEE in result["errors"]


async def test_step_user_invalid_tax_rate_returns_error():
    flow = SunSaleConfigFlow()
    bad = {**VALID_TARIFF_INPUT, CONF_TARIFF_TAX_RATE: 150.0}
    result = await flow.async_step_user(bad)
    assert result["step_id"] == "user"
    assert CONF_TARIFF_TAX_RATE in result["errors"]


async def test_step_user_negative_tax_returns_error():
    flow = SunSaleConfigFlow()
    bad = {**VALID_TARIFF_INPUT, CONF_TARIFF_TAX_RATE: -1.0}
    result = await flow.async_step_user(bad)
    assert result["step_id"] == "user"
    assert CONF_TARIFF_TAX_RATE in result["errors"]


async def test_step_user_stores_data():
    flow = SunSaleConfigFlow()
    await flow.async_step_user(VALID_TARIFF_INPUT)
    assert flow._data[CONF_TARIFF_DISTRIBUTION_FEE] == 0.03
    assert flow._data[CONF_TARIFF_TAX_RATE] == 21.0


# ---------------------------------------------------------------------------
# async_step_battery — form and validation
# ---------------------------------------------------------------------------

async def test_step_battery_no_input_shows_form():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_battery(None)
    assert result["step_id"] == "battery"


async def test_step_battery_valid_input_proceeds_to_inverter():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_battery(VALID_BATTERY_INPUT)
    assert result["step_id"] == "inverter"


async def test_step_battery_zero_capacity_returns_error():
    flow = SunSaleConfigFlow()
    bad = {**VALID_BATTERY_INPUT, CONF_BATTERY_NOMINAL_CAPACITY: 0.0}
    result = await flow.async_step_battery(bad)
    assert result["step_id"] == "battery"
    assert CONF_BATTERY_NOMINAL_CAPACITY in result["errors"]


async def test_step_battery_zero_price_returns_error():
    flow = SunSaleConfigFlow()
    bad = {**VALID_BATTERY_INPUT, CONF_BATTERY_PURCHASE_PRICE: 0.0}
    result = await flow.async_step_battery(bad)
    assert result["step_id"] == "battery"
    assert CONF_BATTERY_PURCHASE_PRICE in result["errors"]


async def test_step_battery_invalid_efficiency_returns_error():
    flow = SunSaleConfigFlow()
    bad = {**VALID_BATTERY_INPUT, CONF_BATTERY_ROUND_TRIP_EFFICIENCY: 110.0}
    result = await flow.async_step_battery(bad)
    assert result["step_id"] == "battery"
    assert CONF_BATTERY_ROUND_TRIP_EFFICIENCY in result["errors"]


async def test_step_battery_zero_efficiency_returns_error():
    flow = SunSaleConfigFlow()
    bad = {**VALID_BATTERY_INPUT, CONF_BATTERY_ROUND_TRIP_EFFICIENCY: 0.0}
    result = await flow.async_step_battery(bad)
    assert result["step_id"] == "battery"
    assert CONF_BATTERY_ROUND_TRIP_EFFICIENCY in result["errors"]


# ---------------------------------------------------------------------------
# async_step_inverter — platform routing
# ---------------------------------------------------------------------------

async def test_step_inverter_no_input_shows_form():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_inverter(None)
    assert result["step_id"] == "inverter"


def _flow_with_hass(solis_entries: list) -> SunSaleConfigFlow:
    """Return a SunSaleConfigFlow with a mock hass pre-configured with solis entries."""
    flow = SunSaleConfigFlow()
    mock_hass = MagicMock()
    mock_hass.config_entries.async_entries.return_value = solis_entries
    flow.hass = mock_hass
    return flow


async def test_step_inverter_solis_routes_to_solis_step_no_entries():
    flow = _flow_with_hass([])
    result = await flow.async_step_inverter({CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value})
    assert result["step_id"] == "inverter_solis"


async def test_step_inverter_solis_autodetects_single_entry():
    entry = MagicMock()
    entry.entry_id = "solis_entry_abc"
    flow = _flow_with_hass([entry])
    result = await flow.async_step_inverter_solis(None)
    assert result["step_id"] == "sources"
    assert flow._data[CONF_SOLIS_CONFIG_ENTRY_ID] == "solis_entry_abc"


async def test_step_inverter_solis_shows_picker_for_multiple_entries():
    entries = [
        MagicMock(entry_id="e1", title="Solis 1"),
        MagicMock(entry_id="e2", title="Solis 2"),
    ]
    flow = _flow_with_hass(entries)
    result = await flow.async_step_inverter_solis(None)
    assert result["step_id"] == "inverter_solis"


async def test_step_inverter_solis_picker_submission_proceeds():
    entries = [
        MagicMock(entry_id="e1", title="Solis 1"),
        MagicMock(entry_id="e2", title="Solis 2"),
    ]
    flow = _flow_with_hass(entries)
    result = await flow.async_step_inverter_solis({CONF_SOLIS_CONFIG_ENTRY_ID: "e2"})
    assert result["step_id"] == "sources"
    assert flow._data[CONF_SOLIS_CONFIG_ENTRY_ID] == "e2"


async def test_step_inverter_generic_routes_to_entities_step():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_inverter({CONF_INVERTER_PLATFORM: InverterPlatform.GENERIC.value})
    assert result["step_id"] == "inverter_entities"


async def test_step_inverter_stores_platform():
    flow = _flow_with_hass([])
    await flow.async_step_inverter({CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value})
    assert flow._data[CONF_INVERTER_PLATFORM] == InverterPlatform.SOLIS.value


# ---------------------------------------------------------------------------
# async_step_sources — entry creation
# ---------------------------------------------------------------------------

async def test_step_sources_no_input_shows_form():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_sources(None)
    assert result["step_id"] == "sources"


async def test_step_sources_creates_entry():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_sources({
        CONF_NORDPOOL_ENTITY: "sensor.nordpool",
        CONF_SOLAR_FORECAST_ENTITY: "",
    })
    assert result["title"] == "sunSale"
    assert result["data"][CONF_NORDPOOL_ENTITY] == "sensor.nordpool"
