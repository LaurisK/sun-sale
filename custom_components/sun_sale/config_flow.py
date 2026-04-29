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

from .const import (
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_NOMINAL_CAPACITY,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_PURCHASE_PRICE,
    CONF_BATTERY_RATED_CYCLE_LIFE,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY,
    CONF_EV_BATTERY_CAPACITY,
    CONF_EV_ENABLED,
    CONF_EV_ENTITY_CHARGER_SWITCH,
    CONF_EV_ENTITY_DEPARTURE_TIME,
    CONF_EV_ENTITY_PLUG_STATE,
    CONF_EV_ENTITY_SOC,
    CONF_EV_ENTITY_TARGET_SOC,
    CONF_EV_MAX_CHARGE_POWER,
    CONF_EV_MIN_CHARGE_POWER,
    CONF_EV_PLATFORM,
    CONF_INVERTER_ENTITY_BATTERY_POWER,
    CONF_INVERTER_ENTITY_BATTERY_SOC,
    CONF_INVERTER_ENTITY_CHARGE_CONTROL,
    CONF_INVERTER_ENTITY_GRID_POWER,
    CONF_INVERTER_PLATFORM,
    CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH,
    CONF_INVERTER_SOLIS_CHARGE_CURRENT,
    CONF_INVERTER_SOLIS_CHARGE_END_TIME_1,
    CONF_INVERTER_SOLIS_CHARGE_START_TIME_1,
    CONF_INVERTER_SOLIS_DISCHARGE_CURRENT,
    CONF_INVERTER_SOLIS_DISCHARGE_END_TIME_1,
    CONF_INVERTER_SOLIS_DISCHARGE_START_TIME_1,
    CONF_INVERTER_SOLIS_SELF_USE_MODE_SWITCH,
    CONF_INVERTER_SOLIS_TOU_MODE_SWITCH,
    CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD,
    CONF_NORDPOOL_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY_2,
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
    DEFAULT_EV_MIN_CHARGE_POWER_KW,
    DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH,
    DEFAULT_SOLIS_CHARGE_CURRENT,
    DEFAULT_SOLIS_CHARGE_END_TIME_1,
    DEFAULT_SOLIS_CHARGE_START_TIME_1,
    DEFAULT_SOLIS_DISCHARGE_CURRENT,
    DEFAULT_SOLIS_DISCHARGE_END_TIME_1,
    DEFAULT_SOLIS_DISCHARGE_START_TIME_1,
    DEFAULT_SOLIS_SELF_USE_MODE_SWITCH,
    DEFAULT_SOLIS_TOU_MODE_SWITCH,
    DOMAIN,
)
from .ev_charger import EVChargerPlatform
from .inverter import InverterPlatform

INVERTER_PLATFORMS = [
    {"value": InverterPlatform.SOLIS.value, "label": "Solis (solis_modbus)"},
    {"value": InverterPlatform.HUAWEI_SOLAR.value, "label": "Huawei Solar (not tested)"},
    {"value": InverterPlatform.SOLAREDGE.value, "label": "SolarEdge (not tested)"},
    {"value": InverterPlatform.GOODWE.value, "label": "GoodWe (not tested)"},
    {"value": InverterPlatform.GENERIC.value, "label": "Generic (not tested)"},
]
EV_PLATFORMS = [p.value for p in EVChargerPlatform]

_SENSOR = EntitySelector(EntitySelectorConfig(domain="sensor"))
_SENSOR_POWER = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="power"))
_SENSOR_SOC = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="battery"))
_SENSOR_ENERGY = EntitySelector(EntitySelectorConfig(domain="sensor", device_class=["energy", "power"]))
_BINARY_SENSOR_PLUG = EntitySelector(EntitySelectorConfig(domain="binary_sensor", device_class="plug"))
_SWITCH = EntitySelector(EntitySelectorConfig(domain="switch"))
_NUMBER = EntitySelector(EntitySelectorConfig(domain="number"))
_TIME = EntitySelector(EntitySelectorConfig(domain="time"))
_ANY_ENTITY = EntitySelector(EntitySelectorConfig())


def _platform_selector(options: list[str]) -> SelectSelector:
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
    return vol.Schema({
        _req(CONF_TARIFF_DISTRIBUTION_FEE, d, 0.03): vol.Coerce(float),
        _req(CONF_TARIFF_TAX_RATE, d, 21.0): vol.Coerce(float),
        _req(CONF_TARIFF_MARKUP, d, 0.005): vol.Coerce(float),
        _req(CONF_TARIFF_SELL_DISTRIBUTION_FEE, d, 0.01): vol.Coerce(float),
        _req(CONF_TARIFF_SELL_TAX_RATE, d, 0.0): vol.Coerce(float),
        _req(CONF_TARIFF_SELL_MARKUP, d, 0.0): vol.Coerce(float),
    })


def _battery_schema(d: dict) -> vol.Schema:
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
    return vol.Schema({
        _req(CONF_INVERTER_PLATFORM, d, InverterPlatform.SOLIS.value): _platform_selector(INVERTER_PLATFORMS),
    })


def _inverter_entities_schema(d: dict) -> vol.Schema:
    return vol.Schema({
        _req(CONF_INVERTER_ENTITY_BATTERY_SOC, d): _SENSOR_SOC,
        _req(CONF_INVERTER_ENTITY_BATTERY_POWER, d): _SENSOR_POWER,
        _req(CONF_INVERTER_ENTITY_GRID_POWER, d): _SENSOR_POWER,
        _req(CONF_INVERTER_ENTITY_CHARGE_CONTROL, d): _ANY_ENTITY,
    })


def _inverter_solis_schema(d: dict) -> vol.Schema:
    return vol.Schema({
        _req(CONF_INVERTER_ENTITY_BATTERY_SOC, d, "sensor.solis_battery_soc"): _SENSOR_SOC,
        _req(CONF_INVERTER_ENTITY_BATTERY_POWER, d, "sensor.solis_battery_power"): _SENSOR_POWER,
        _req(CONF_INVERTER_ENTITY_GRID_POWER, d, "sensor.solis_ac_grid_port_power"): _SENSOR_POWER,
        _req(CONF_INVERTER_SOLIS_CHARGE_CURRENT, d, DEFAULT_SOLIS_CHARGE_CURRENT): _NUMBER,
        _req(CONF_INVERTER_SOLIS_DISCHARGE_CURRENT, d, DEFAULT_SOLIS_DISCHARGE_CURRENT): _NUMBER,
        _req(CONF_INVERTER_SOLIS_CHARGE_START_TIME_1, d, DEFAULT_SOLIS_CHARGE_START_TIME_1): _TIME,
        _req(CONF_INVERTER_SOLIS_CHARGE_END_TIME_1, d, DEFAULT_SOLIS_CHARGE_END_TIME_1): _TIME,
        _req(CONF_INVERTER_SOLIS_DISCHARGE_START_TIME_1, d, DEFAULT_SOLIS_DISCHARGE_START_TIME_1): _TIME,
        _req(CONF_INVERTER_SOLIS_DISCHARGE_END_TIME_1, d, DEFAULT_SOLIS_DISCHARGE_END_TIME_1): _TIME,
        _req(CONF_INVERTER_SOLIS_TOU_MODE_SWITCH, d, DEFAULT_SOLIS_TOU_MODE_SWITCH): _SWITCH,
        _req(CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH, d, DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH): _SWITCH,
        _req(CONF_INVERTER_SOLIS_SELF_USE_MODE_SWITCH, d, DEFAULT_SOLIS_SELF_USE_MODE_SWITCH): _SWITCH,
    })


def _ev_schema(d: dict) -> vol.Schema:
    return vol.Schema({
        _req(CONF_EV_ENABLED, d, False): bool,
        _opt(CONF_EV_PLATFORM, d, EVChargerPlatform.GENERIC.value): _platform_selector(EV_PLATFORMS),
        _opt(CONF_EV_BATTERY_CAPACITY, d): vol.Coerce(float),
        _opt(CONF_EV_MAX_CHARGE_POWER, d): vol.Coerce(float),
        _opt(CONF_EV_MIN_CHARGE_POWER, d, DEFAULT_EV_MIN_CHARGE_POWER_KW): vol.Coerce(float),
        _opt(CONF_EV_ENTITY_PLUG_STATE, d): _BINARY_SENSOR_PLUG,
        _opt(CONF_EV_ENTITY_SOC, d): _SENSOR_SOC,
        _opt(CONF_EV_ENTITY_TARGET_SOC, d): _ANY_ENTITY,
        _opt(CONF_EV_ENTITY_DEPARTURE_TIME, d): _ANY_ENTITY,
        _opt(CONF_EV_ENTITY_CHARGER_SWITCH, d): _SWITCH,
    })


def _sources_schema(d: dict) -> vol.Schema:
    return vol.Schema({
        _req(CONF_NORDPOOL_ENTITY, d): _SENSOR,
        _opt(CONF_SOLAR_FORECAST_ENTITY, d): _SENSOR_ENERGY,
        _opt(CONF_SOLAR_FORECAST_ENTITY_2, d): _SENSOR_ENERGY,
        _opt(CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD, d): _SENSOR_POWER,
    })


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------

class SunSaleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Multi-step config flow: tariff → battery → inverter platform → inverter entities → EV → sources."""

    VERSION = 1

    def __init__(self) -> None:
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
            return await self.async_step_ev()

        return self.async_show_form(
            step_id="inverter_entities",
            data_schema=_inverter_entities_schema({}),
        )

    async def async_step_inverter_solis(self, user_input: dict | None = None) -> FlowResult:
        """Step 3b (Solis): TOU-model entity mapping for solis_modbus."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_ev()

        return self.async_show_form(
            step_id="inverter_solis",
            data_schema=_inverter_solis_schema({}),
        )

    async def async_step_ev(self, user_input: dict | None = None) -> FlowResult:
        """Step 4: EV charger (optional)."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_sources()

        return self.async_show_form(
            step_id="ev",
            data_schema=_ev_schema({}),
        )

    async def async_step_sources(self, user_input: dict | None = None) -> FlowResult:
        """Step 5: Nordpool and solar forecast entity selection."""
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
        return SunSaleOptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options flow (full reconfiguration)
# ---------------------------------------------------------------------------

class SunSaleOptionsFlow(config_entries.OptionsFlow):
    """Allow reconfiguring all settings post-setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._data: dict[str, Any] = {}
        self._inverter_platform: str = InverterPlatform.GENERIC.value

    def _defaults(self) -> dict[str, Any]:
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
            return await self.async_step_ev()

        return self.async_show_form(
            step_id="inverter_entities",
            data_schema=_inverter_entities_schema(self._defaults()),
        )

    async def async_step_inverter_solis(self, user_input: dict | None = None) -> FlowResult:
        """Step 3b (Solis): TOU-model entity mapping."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_ev()

        return self.async_show_form(
            step_id="inverter_solis",
            data_schema=_inverter_solis_schema(self._defaults()),
        )

    async def async_step_ev(self, user_input: dict | None = None) -> FlowResult:
        """Step 4: EV charger (optional)."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_sources()

        return self.async_show_form(
            step_id="ev",
            data_schema=_ev_schema(self._defaults()),
        )

    async def async_step_sources(self, user_input: dict | None = None) -> FlowResult:
        """Step 5: Nordpool and solar forecast entities."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="", data=self._data)

        return self.async_show_form(
            step_id="sources",
            data_schema=_sources_schema(self._defaults()),
        )
