"""Multi-step config flow for sunSale."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .contract.const import (
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_NOMINAL_CAPACITY,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_PURCHASE_PRICE,
    CONF_BATTERY_RATED_CYCLE_LIFE,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY,
    CONF_INVERTER_ENTITY_BATTERY_POWER,
    CONF_INVERTER_ENTITY_BATTERY_SOC,
    CONF_INVERTER_ENTITY_CHARGE_CONTROL,
    CONF_INVERTER_ENTITY_GRID_POWER,
    CONF_INVERTER_PLATFORM,
    CONF_INVERTER_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH,
    CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH,
    CONF_INVERTER_SOLIS_BACKFLOW_POWER,
    CONF_INVERTER_SOLIS_BATTERY_MAX_CHARGE_CURRENT,
    CONF_INVERTER_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT,
    CONF_INVERTER_SOLIS_FEED_IN_PRIORITY_SWITCH,
    CONF_INVERTER_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH,
    CONF_INVERTER_SOLIS_PEAK_MAX_USABLE_GRID_POWER,
    CONF_INVERTER_SOLIS_RC_SETPOINT,
    CONF_INVERTER_SOLIS_SELF_USE_SWITCH,
    CONF_INVERTER_SOLIS_STORAGE_CONTROL_READBACK,
    CONF_INVERTER_SOLIS_TOU_MODE_SWITCH,
    CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY,
    CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY,
    CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY,
    CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD,
    CONF_INVERTER_ENTITY_SOLAR_ENERGY,
    CONF_INVERTER_ENTITY_PV_POWER,
    CONF_NORDPOOL_ENTITY,
    CONF_NORDPOOL_RESOLUTION,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY_2,
    CONF_SOLIS_CONFIG_ENTRY_ID,
    CONF_TARIFF_DISTRIBUTION_FEE,
    CONF_TARIFF_MARKUP,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE,
    CONF_TARIFF_SELL_MARKUP,
    CONF_TARIFF_SELL_TAX_RATE,
    CONF_TARIFF_TAX_RATE,
    DEFAULT_BATTERY_MAX_SOC,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_BATTERY_NOMINAL_VOLTAGE,
    DEFAULT_BATTERY_RATED_CYCLE_LIFE,
    DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY,
    DEFAULT_NORDPOOL_RESOLUTION,
    DEFAULT_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH,
    DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH,
    DEFAULT_SOLIS_BACKFLOW_POWER,
    DEFAULT_SOLIS_BATTERY_MAX_CHARGE_CURRENT,
    DEFAULT_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT,
    DEFAULT_SOLIS_FEED_IN_PRIORITY_SWITCH,
    DEFAULT_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH,
    DEFAULT_SOLIS_PEAK_MAX_USABLE_GRID_POWER,
    DEFAULT_SOLIS_RC_SETPOINT,
    DEFAULT_SOLIS_SELF_USE_SWITCH,
    DEFAULT_SOLIS_STORAGE_CONTROL_READBACK,
    DEFAULT_SOLIS_TOU_MODE_SWITCH,
    DOMAIN,
)
from .outbound.inverter import InverterPlatform

INVERTER_PLATFORMS = [
    {"value": InverterPlatform.SOLIS.value, "label": "Solis (solis_modbus)"},
    {"value": InverterPlatform.HUAWEI_SOLAR.value, "label": "Huawei Solar (not tested)"},
    {"value": InverterPlatform.SOLAREDGE.value, "label": "SolarEdge (not tested)"},
    {"value": InverterPlatform.GOODWE.value, "label": "GoodWe (not tested)"},
    {"value": InverterPlatform.GENERIC.value, "label": "Generic (not tested)"},
]
_SENSOR = EntitySelector(EntitySelectorConfig(domain="sensor"))
_SENSOR_POWER = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="power"))
_SENSOR_SOC = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="battery"))
_SENSOR_ENERGY = EntitySelector(EntitySelectorConfig(domain="sensor", device_class=["energy", "power"]))
_SWITCH = EntitySelector(EntitySelectorConfig(domain="switch"))
_NUMBER = EntitySelector(EntitySelectorConfig(domain="number"))
_TIME = EntitySelector(EntitySelectorConfig(domain="time"))
_ANY_ENTITY = EntitySelector(EntitySelectorConfig())


def _platform_selector(options: list[str]) -> SelectSelector:
    """Build a list-mode SelectSelector from a list of option dicts.

    Args:
        options: List of ``{"value": ..., "label": ...}`` dicts.

    Returns:
        Configured SelectSelector widget.
    """
    return SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST))


def _req(key: str, d: dict, fallback: Any = vol.UNDEFINED) -> vol.Required:
    """Return vol.Required with a default only when a value is available."""
    v = d.get(key, fallback)
    return vol.Required(key, default=v) if v is not vol.UNDEFINED else vol.Required(key)


def _opt(key: str, d: dict, fallback: Any = vol.UNDEFINED) -> vol.Optional:
    """Return vol.Optional with a default only when a value is available."""
    v = d.get(key, fallback)
    return vol.Optional(key, default=v) if v is not vol.UNDEFINED else vol.Optional(key)


# ---------------------------------------------------------------------------
# Schema builders (shared between config flow and options flow)
# ---------------------------------------------------------------------------

def _tariff_schema(d: dict) -> vol.Schema:
    """Build the voluptuous tariff parameters schema, pre-filled from d.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema covering distribution fee, tax rate, markup, and sell-side equivalents.
    """
    return vol.Schema({
        _req(CONF_TARIFF_DISTRIBUTION_FEE, d, 0.03): vol.Coerce(float),
        _req(CONF_TARIFF_TAX_RATE, d, 21.0): vol.Coerce(float),
        _req(CONF_TARIFF_MARKUP, d, 0.005): vol.Coerce(float),
        _req(CONF_TARIFF_SELL_DISTRIBUTION_FEE, d, 0.01): vol.Coerce(float),
        _req(CONF_TARIFF_SELL_TAX_RATE, d, 0.0): vol.Coerce(float),
        _req(CONF_TARIFF_SELL_MARKUP, d, 0.0): vol.Coerce(float),
    })


def _battery_schema(d: dict) -> vol.Schema:
    """Build the voluptuous battery parameters schema, pre-filled from d.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema covering capacity, price, cycle life, charge/discharge power, SoC limits,
        round-trip efficiency, and nominal voltage.
    """
    return vol.Schema({
        _req(CONF_BATTERY_NOMINAL_CAPACITY, d): vol.Coerce(float),
        _req(CONF_BATTERY_PURCHASE_PRICE, d): vol.Coerce(float),
        _req(CONF_BATTERY_RATED_CYCLE_LIFE, d, DEFAULT_BATTERY_RATED_CYCLE_LIFE): vol.Coerce(int),
        _req(CONF_BATTERY_MAX_CHARGE_POWER, d, 5.0): vol.Coerce(float),
        _req(CONF_BATTERY_MAX_DISCHARGE_POWER, d, 5.0): vol.Coerce(float),
        _req(CONF_BATTERY_MIN_SOC, d, DEFAULT_BATTERY_MIN_SOC): vol.Coerce(float),
        _req(CONF_BATTERY_MAX_SOC, d, DEFAULT_BATTERY_MAX_SOC): vol.Coerce(float),
        _req(CONF_BATTERY_ROUND_TRIP_EFFICIENCY, d, DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY): vol.Coerce(float),
        _req(CONF_BATTERY_NOMINAL_VOLTAGE, d, DEFAULT_BATTERY_NOMINAL_VOLTAGE): vol.Coerce(float),
    })


def _inverter_platform_schema(d: dict) -> vol.Schema:
    """Build the inverter platform selection schema, pre-filled from d.

    Args:
        d: Existing config/options dict used for default value.

    Returns:
        Single-field schema for selecting the inverter integration platform.
    """
    return vol.Schema({
        _req(CONF_INVERTER_PLATFORM, d, InverterPlatform.SOLIS.value): _platform_selector(INVERTER_PLATFORMS),
    })


def _inverter_entities_schema(d: dict) -> vol.Schema:
    """Build the generic inverter entity mapping schema, pre-filled from d.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema covering SoC, battery power, grid power, and charge control entity selectors.
    """
    return vol.Schema({
        _req(CONF_INVERTER_ENTITY_BATTERY_SOC, d): _SENSOR_SOC,
        _req(CONF_INVERTER_ENTITY_BATTERY_POWER, d): _SENSOR_POWER,
        _req(CONF_INVERTER_ENTITY_GRID_POWER, d): _SENSOR_POWER,
        _req(CONF_INVERTER_ENTITY_CHARGE_CONTROL, d): _ANY_ENTITY,
    })


def _inverter_solis_schema(d: dict) -> vol.Schema:
    """Build the Solis register-level entity mapping schema, pre-filled from d.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema covering all entities required by the StorageMode state
        machine: telemetry sensors, the register-43110 readback, battery
        current numbers, RC setpoint, export-limit numbers, and the bit
        switches that compose register 43110 + the export-master switches.
    """
    return vol.Schema({
        _req(CONF_INVERTER_ENTITY_BATTERY_SOC, d, "sensor.solis_battery_soc"): _SENSOR_SOC,
        _req(CONF_INVERTER_ENTITY_BATTERY_POWER, d, "sensor.solis_battery_power"): _SENSOR_POWER,
        _req(CONF_INVERTER_ENTITY_GRID_POWER, d, "sensor.solis_grid_power_net"): _SENSOR_POWER,
        _req(CONF_INVERTER_SOLIS_STORAGE_CONTROL_READBACK, d, DEFAULT_SOLIS_STORAGE_CONTROL_READBACK): _SENSOR,
        _req(CONF_INVERTER_SOLIS_BATTERY_MAX_CHARGE_CURRENT, d, DEFAULT_SOLIS_BATTERY_MAX_CHARGE_CURRENT): _NUMBER,
        _req(CONF_INVERTER_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT, d, DEFAULT_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT): _NUMBER,
        _req(CONF_INVERTER_SOLIS_RC_SETPOINT, d, DEFAULT_SOLIS_RC_SETPOINT): _NUMBER,
        _req(CONF_INVERTER_SOLIS_BACKFLOW_POWER, d, DEFAULT_SOLIS_BACKFLOW_POWER): _NUMBER,
        _req(CONF_INVERTER_SOLIS_PEAK_MAX_USABLE_GRID_POWER, d, DEFAULT_SOLIS_PEAK_MAX_USABLE_GRID_POWER): _NUMBER,
        _req(CONF_INVERTER_SOLIS_SELF_USE_SWITCH, d, DEFAULT_SOLIS_SELF_USE_SWITCH): _SWITCH,
        _req(CONF_INVERTER_SOLIS_TOU_MODE_SWITCH, d, DEFAULT_SOLIS_TOU_MODE_SWITCH): _SWITCH,
        _req(CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH, d, DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH): _SWITCH,
        _req(CONF_INVERTER_SOLIS_FEED_IN_PRIORITY_SWITCH, d, DEFAULT_SOLIS_FEED_IN_PRIORITY_SWITCH): _SWITCH,
        _req(CONF_INVERTER_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH, d, DEFAULT_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH): _SWITCH,
        _req(CONF_INVERTER_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH, d, DEFAULT_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH): _SWITCH,
    })


def _inverter_solis_pick_schema(options: list[dict], d: dict) -> vol.Schema:
    """Build a single-field schema for selecting which solis_modbus config entry to use.

    Args:
        options: List of ``{"value": entry_id, "label": title}`` dicts from discovered entries.
        d: Existing config/options dict used for the default value.

    Returns:
        Schema with one SelectSelector field for the solis_modbus config entry ID.
    """
    return vol.Schema({
        _req(CONF_SOLIS_CONFIG_ENTRY_ID, d): SelectSelector(
            SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
        )
    })


_NORDPOOL_RESOLUTION_SELECTOR = SelectSelector(SelectSelectorConfig(
    options=[
        {"value": "15min", "label": "15-minute (recommended)"},
        {"value": "hourly", "label": "Hourly"},
    ],
    mode=SelectSelectorMode.LIST,
))


def _sources_schema(d: dict) -> vol.Schema:
    """Build the data-sources entity selection schema, pre-filled from d.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema covering Nordpool entity, resolution, solar forecast entities, solar energy,
        household load, and household consumption energy selectors.
    """
    return vol.Schema({
        _req(CONF_NORDPOOL_ENTITY, d): _SENSOR,
        _req(CONF_NORDPOOL_RESOLUTION, d, DEFAULT_NORDPOOL_RESOLUTION): _NORDPOOL_RESOLUTION_SELECTOR,
        _opt(CONF_SOLAR_FORECAST_ENTITY, d): _SENSOR_ENERGY,
        _opt(CONF_SOLAR_FORECAST_ENTITY_2, d): _SENSOR_ENERGY,
        _opt(CONF_INVERTER_ENTITY_SOLAR_ENERGY, d): _SENSOR_ENERGY,
        _opt(CONF_INVERTER_ENTITY_PV_POWER, d): _SENSOR_POWER,
        _opt(CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD, d): _SENSOR_POWER,
        _opt(CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY, d): _SENSOR_ENERGY,
        _opt(CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY, d): _SENSOR_ENERGY,
        _opt(CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY, d): _SENSOR_ENERGY,
    })


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------

class SunSaleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Multi-step config flow: tariff → battery → inverter platform → inverter entities → sources."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise config flow with empty data accumulator and generic inverter default."""
        self._data: dict[str, Any] = {}
        self._inverter_platform: str = InverterPlatform.GENERIC.value

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Step 1: Tariff parameters."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_TARIFF_DISTRIBUTION_FEE] < 0:
                errors[CONF_TARIFF_DISTRIBUTION_FEE] = "negative_fee"
            if not 0.0 <= user_input[CONF_TARIFF_TAX_RATE] <= 100.0:
                errors[CONF_TARIFF_TAX_RATE] = "invalid_tax_rate"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_battery()

        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=_tariff_schema({}),
        )

    async def async_step_battery(self, user_input: dict | None = None) -> FlowResult:
        """Step 2: Battery parameters."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_BATTERY_NOMINAL_CAPACITY] <= 0:
                errors[CONF_BATTERY_NOMINAL_CAPACITY] = "invalid_capacity"
            if user_input[CONF_BATTERY_PURCHASE_PRICE] <= 0:
                errors[CONF_BATTERY_PURCHASE_PRICE] = "invalid_price"
            if not 0.0 < user_input[CONF_BATTERY_ROUND_TRIP_EFFICIENCY] <= 100.0:
                errors[CONF_BATTERY_ROUND_TRIP_EFFICIENCY] = "invalid_efficiency"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_inverter()

        return self.async_show_form(
            step_id="battery",
            errors=errors,
            data_schema=_battery_schema({}),
        )

    async def async_step_inverter(self, user_input: dict | None = None) -> FlowResult:
        """Step 3a: Inverter platform selection."""
        if user_input is not None:
            self._inverter_platform = user_input[CONF_INVERTER_PLATFORM]
            self._data[CONF_INVERTER_PLATFORM] = self._inverter_platform
            if self._inverter_platform == InverterPlatform.SOLIS.value:
                return await self.async_step_inverter_solis()
            return await self.async_step_inverter_entities()

        return self.async_show_form(
            step_id="inverter",
            data_schema=_inverter_platform_schema({}),
        )

    async def async_step_inverter_entities(self, user_input: dict | None = None) -> FlowResult:
        """Step 3b: Standard inverter entity mapping (non-Solis platforms)."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_sources()

        return self.async_show_form(
            step_id="inverter_entities",
            data_schema=_inverter_entities_schema({}),
        )

    async def async_step_inverter_solis(self, user_input: dict | None = None) -> FlowResult:
        """Step 3b (Solis): Auto-detect solis_modbus or show entity mapping.

        When exactly one solis_modbus config entry exists the step auto-proceeds
        without showing a form. When multiple entries exist a compact picker is
        shown. When none exist the full manual entity mapping form is shown as a
        fallback.
        """
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_sources()

        solis_entries = self.hass.config_entries.async_entries("solis_modbus")

        if len(solis_entries) == 1:
            self._data[CONF_SOLIS_CONFIG_ENTRY_ID] = solis_entries[0].entry_id
            return await self.async_step_sources()

        if len(solis_entries) > 1:
            options = [{"value": e.entry_id, "label": e.title} for e in solis_entries]
            return self.async_show_form(
                step_id="inverter_solis",
                data_schema=_inverter_solis_pick_schema(options, {}),
            )

        return self.async_show_form(
            step_id="inverter_solis",
            data_schema=_inverter_solis_schema({}),
        )

    async def async_step_sources(self, user_input: dict | None = None) -> FlowResult:
        """Step 4: Nordpool and solar forecast entity selection."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="sunSale", data=self._data)

        return self.async_show_form(
            step_id="sources",
            data_schema=_sources_schema({}),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "SunSaleOptionsFlow":
        """Return the options flow handler for this config entry.

        Args:
            config_entry: The existing config entry to reconfigure.

        Returns:
            SunSaleOptionsFlow instance pre-loaded with the current entry.
        """
        return SunSaleOptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options flow (full reconfiguration)
# ---------------------------------------------------------------------------

class SunSaleOptionsFlow(config_entries.OptionsFlow):
    """Allow reconfiguring all settings post-setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialise options flow, storing the existing entry for default values.

        Args:
            config_entry: The config entry being reconfigured.
        """
        self._entry = config_entry
        self._data: dict[str, Any] = {}
        self._inverter_platform: str = InverterPlatform.GENERIC.value

    def _defaults(self) -> dict[str, Any]:
        """Return merged entry data + options as a single defaults dict.

        Returns:
            Combined dict of entry.data and entry.options (options take precedence).
        """
        return {**self._entry.data, **self._entry.options}

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        """Step 1: Tariff parameters."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_TARIFF_DISTRIBUTION_FEE] < 0:
                errors[CONF_TARIFF_DISTRIBUTION_FEE] = "negative_fee"
            if not 0.0 <= user_input[CONF_TARIFF_TAX_RATE] <= 100.0:
                errors[CONF_TARIFF_TAX_RATE] = "invalid_tax_rate"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_battery()

        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=_tariff_schema(self._defaults()),
        )

    async def async_step_battery(self, user_input: dict | None = None) -> FlowResult:
        """Step 2: Battery parameters."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_BATTERY_NOMINAL_CAPACITY] <= 0:
                errors[CONF_BATTERY_NOMINAL_CAPACITY] = "invalid_capacity"
            if user_input[CONF_BATTERY_PURCHASE_PRICE] <= 0:
                errors[CONF_BATTERY_PURCHASE_PRICE] = "invalid_price"
            if not 0.0 < user_input[CONF_BATTERY_ROUND_TRIP_EFFICIENCY] <= 100.0:
                errors[CONF_BATTERY_ROUND_TRIP_EFFICIENCY] = "invalid_efficiency"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_inverter()

        return self.async_show_form(
            step_id="battery",
            errors=errors,
            data_schema=_battery_schema(self._defaults()),
        )

    async def async_step_inverter(self, user_input: dict | None = None) -> FlowResult:
        """Step 3a: Inverter platform selection."""
        if user_input is not None:
            self._inverter_platform = user_input[CONF_INVERTER_PLATFORM]
            self._data[CONF_INVERTER_PLATFORM] = self._inverter_platform
            if self._inverter_platform == InverterPlatform.SOLIS.value:
                return await self.async_step_inverter_solis()
            return await self.async_step_inverter_entities()

        return self.async_show_form(
            step_id="inverter",
            data_schema=_inverter_platform_schema(self._defaults()),
        )

    async def async_step_inverter_entities(self, user_input: dict | None = None) -> FlowResult:
        """Step 3b: Standard inverter entity mapping."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_sources()

        return self.async_show_form(
            step_id="inverter_entities",
            data_schema=_inverter_entities_schema(self._defaults()),
        )

    async def async_step_inverter_solis(self, user_input: dict | None = None) -> FlowResult:
        """Step 3b (Solis): Auto-detect solis_modbus or show entity mapping.

        Mirrors the config-flow Solis step. Single-entry detection auto-proceeds;
        multiple entries show a picker; no entries fall back to manual mapping.
        """
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_sources()

        solis_entries = self.hass.config_entries.async_entries("solis_modbus")
        d = self._defaults()

        if len(solis_entries) == 1:
            self._data[CONF_SOLIS_CONFIG_ENTRY_ID] = solis_entries[0].entry_id
            return await self.async_step_sources()

        if len(solis_entries) > 1:
            options = [{"value": e.entry_id, "label": e.title} for e in solis_entries]
            return self.async_show_form(
                step_id="inverter_solis",
                data_schema=_inverter_solis_pick_schema(options, d),
            )

        return self.async_show_form(
            step_id="inverter_solis",
            data_schema=_inverter_solis_schema(d),
        )

    async def async_step_sources(self, user_input: dict | None = None) -> FlowResult:
        """Step 4: Nordpool and solar forecast entities."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="", data=self._data)

        return self.async_show_form(
            step_id="sources",
            data_schema=_sources_schema(self._defaults()),
        )
