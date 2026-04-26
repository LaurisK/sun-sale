"""Multi-step config flow for sunSale."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

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
    CONF_INVERTER_SOLIS_CHARGE_END_HOUR_1,
    CONF_INVERTER_SOLIS_CHARGE_END_MINUTE_1,
    CONF_INVERTER_SOLIS_CHARGE_START_HOUR_1,
    CONF_INVERTER_SOLIS_CHARGE_START_MINUTE_1,
    CONF_INVERTER_SOLIS_DISCHARGE_CURRENT,
    CONF_INVERTER_SOLIS_DISCHARGE_END_HOUR_1,
    CONF_INVERTER_SOLIS_DISCHARGE_END_MINUTE_1,
    CONF_INVERTER_SOLIS_DISCHARGE_START_HOUR_1,
    CONF_INVERTER_SOLIS_DISCHARGE_START_MINUTE_1,
    CONF_INVERTER_SOLIS_SELF_USE_MODE_SWITCH,
    CONF_INVERTER_SOLIS_TOU_MODE_SWITCH,
    CONF_NORDPOOL_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY,
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
    DEFAULT_SOLIS_CHARGE_END_HOUR_1,
    DEFAULT_SOLIS_CHARGE_END_MINUTE_1,
    DEFAULT_SOLIS_CHARGE_START_HOUR_1,
    DEFAULT_SOLIS_CHARGE_START_MINUTE_1,
    DEFAULT_SOLIS_DISCHARGE_CURRENT,
    DEFAULT_SOLIS_DISCHARGE_END_HOUR_1,
    DEFAULT_SOLIS_DISCHARGE_END_MINUTE_1,
    DEFAULT_SOLIS_DISCHARGE_START_HOUR_1,
    DEFAULT_SOLIS_DISCHARGE_START_MINUTE_1,
    DEFAULT_SOLIS_SELF_USE_MODE_SWITCH,
    DEFAULT_SOLIS_TOU_MODE_SWITCH,
    DOMAIN,
)
from .ev_charger import EVChargerPlatform
from .inverter import InverterPlatform

INVERTER_PLATFORMS = [p.value for p in InverterPlatform]
EV_PLATFORMS = [p.value for p in EVChargerPlatform]


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
            if not 0.0 <= user_input[CONF_TARIFF_TAX_RATE] <= 1.0:
                errors[CONF_TARIFF_TAX_RATE] = "invalid_tax_rate"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_battery()

        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=vol.Schema({
                vol.Required(CONF_TARIFF_DISTRIBUTION_FEE, default=0.03): vol.Coerce(float),
                vol.Required(CONF_TARIFF_TAX_RATE, default=0.21): vol.Coerce(float),
                vol.Required(CONF_TARIFF_MARKUP, default=0.005): vol.Coerce(float),
                vol.Required(CONF_TARIFF_SELL_DISTRIBUTION_FEE, default=0.01): vol.Coerce(float),
                vol.Required(CONF_TARIFF_SELL_TAX_RATE, default=0.0): vol.Coerce(float),
                vol.Required(CONF_TARIFF_SELL_MARKUP, default=0.0): vol.Coerce(float),
            }),
        )

    async def async_step_battery(self, user_input: dict | None = None) -> FlowResult:
        """Step 2: Battery parameters."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_BATTERY_NOMINAL_CAPACITY] <= 0:
                errors[CONF_BATTERY_NOMINAL_CAPACITY] = "invalid_capacity"
            if user_input[CONF_BATTERY_PURCHASE_PRICE] <= 0:
                errors[CONF_BATTERY_PURCHASE_PRICE] = "invalid_price"
            if not 0.0 < user_input[CONF_BATTERY_ROUND_TRIP_EFFICIENCY] <= 1.0:
                errors[CONF_BATTERY_ROUND_TRIP_EFFICIENCY] = "invalid_efficiency"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_inverter()

        return self.async_show_form(
            step_id="battery",
            errors=errors,
            data_schema=vol.Schema({
                vol.Required(CONF_BATTERY_NOMINAL_CAPACITY): vol.Coerce(float),
                vol.Required(CONF_BATTERY_PURCHASE_PRICE): vol.Coerce(float),
                vol.Required(CONF_BATTERY_RATED_CYCLE_LIFE, default=DEFAULT_BATTERY_RATED_CYCLE_LIFE): vol.Coerce(int),
                vol.Required(CONF_BATTERY_MAX_CHARGE_POWER, default=5.0): vol.Coerce(float),
                vol.Required(CONF_BATTERY_MAX_DISCHARGE_POWER, default=5.0): vol.Coerce(float),
                vol.Required(CONF_BATTERY_MIN_SOC, default=DEFAULT_BATTERY_MIN_SOC): vol.Coerce(float),
                vol.Required(CONF_BATTERY_MAX_SOC, default=DEFAULT_BATTERY_MAX_SOC): vol.Coerce(float),
                vol.Required(CONF_BATTERY_ROUND_TRIP_EFFICIENCY, default=DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY): vol.Coerce(float),
                vol.Required(CONF_BATTERY_NOMINAL_VOLTAGE, default=DEFAULT_BATTERY_NOMINAL_VOLTAGE): vol.Coerce(float),
            }),
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
            data_schema=vol.Schema({
                vol.Required(CONF_INVERTER_PLATFORM, default=InverterPlatform.GENERIC.value): vol.In(INVERTER_PLATFORMS),
            }),
        )

    async def async_step_inverter_entities(self, user_input: dict | None = None) -> FlowResult:
        """Step 3b: Standard inverter entity mapping (non-Solis platforms)."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_ev()

        return self.async_show_form(
            step_id="inverter_entities",
            data_schema=vol.Schema({
                vol.Required(CONF_INVERTER_ENTITY_BATTERY_SOC): str,
                vol.Required(CONF_INVERTER_ENTITY_BATTERY_POWER): str,
                vol.Required(CONF_INVERTER_ENTITY_GRID_POWER): str,
                vol.Required(CONF_INVERTER_ENTITY_CHARGE_CONTROL): str,
            }),
        )

    async def async_step_inverter_solis(self, user_input: dict | None = None) -> FlowResult:
        """Step 3b (Solis): TOU-model entity mapping for solis_modbus."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_ev()

        return self.async_show_form(
            step_id="inverter_solis",
            data_schema=vol.Schema({
                vol.Required(CONF_INVERTER_ENTITY_BATTERY_SOC, default="sensor.solis_battery_soc"): str,
                vol.Required(CONF_INVERTER_ENTITY_BATTERY_POWER, default="sensor.solis_battery_power"): str,
                vol.Required(CONF_INVERTER_ENTITY_GRID_POWER, default="sensor.solis_ac_grid_port_power"): str,
                vol.Required(CONF_INVERTER_SOLIS_CHARGE_CURRENT, default=DEFAULT_SOLIS_CHARGE_CURRENT): str,
                vol.Required(CONF_INVERTER_SOLIS_DISCHARGE_CURRENT, default=DEFAULT_SOLIS_DISCHARGE_CURRENT): str,
                vol.Required(CONF_INVERTER_SOLIS_CHARGE_START_HOUR_1, default=DEFAULT_SOLIS_CHARGE_START_HOUR_1): str,
                vol.Required(CONF_INVERTER_SOLIS_CHARGE_START_MINUTE_1, default=DEFAULT_SOLIS_CHARGE_START_MINUTE_1): str,
                vol.Required(CONF_INVERTER_SOLIS_CHARGE_END_HOUR_1, default=DEFAULT_SOLIS_CHARGE_END_HOUR_1): str,
                vol.Required(CONF_INVERTER_SOLIS_CHARGE_END_MINUTE_1, default=DEFAULT_SOLIS_CHARGE_END_MINUTE_1): str,
                vol.Required(CONF_INVERTER_SOLIS_DISCHARGE_START_HOUR_1, default=DEFAULT_SOLIS_DISCHARGE_START_HOUR_1): str,
                vol.Required(CONF_INVERTER_SOLIS_DISCHARGE_START_MINUTE_1, default=DEFAULT_SOLIS_DISCHARGE_START_MINUTE_1): str,
                vol.Required(CONF_INVERTER_SOLIS_DISCHARGE_END_HOUR_1, default=DEFAULT_SOLIS_DISCHARGE_END_HOUR_1): str,
                vol.Required(CONF_INVERTER_SOLIS_DISCHARGE_END_MINUTE_1, default=DEFAULT_SOLIS_DISCHARGE_END_MINUTE_1): str,
                vol.Required(CONF_INVERTER_SOLIS_TOU_MODE_SWITCH, default=DEFAULT_SOLIS_TOU_MODE_SWITCH): str,
                vol.Required(CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH, default=DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH): str,
                vol.Required(CONF_INVERTER_SOLIS_SELF_USE_MODE_SWITCH, default=DEFAULT_SOLIS_SELF_USE_MODE_SWITCH): str,
            }),
        )

    async def async_step_ev(self, user_input: dict | None = None) -> FlowResult:
        """Step 4: EV charger (optional)."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_sources()

        return self.async_show_form(
            step_id="ev",
            data_schema=vol.Schema({
                vol.Required(CONF_EV_ENABLED, default=False): bool,
                vol.Optional(CONF_EV_PLATFORM, default=EVChargerPlatform.GENERIC.value): vol.In(EV_PLATFORMS),
                vol.Optional(CONF_EV_BATTERY_CAPACITY): vol.Coerce(float),
                vol.Optional(CONF_EV_MAX_CHARGE_POWER): vol.Coerce(float),
                vol.Optional(CONF_EV_MIN_CHARGE_POWER, default=DEFAULT_EV_MIN_CHARGE_POWER_KW): vol.Coerce(float),
                vol.Optional(CONF_EV_ENTITY_PLUG_STATE, default=""): str,
                vol.Optional(CONF_EV_ENTITY_SOC, default=""): str,
                vol.Optional(CONF_EV_ENTITY_TARGET_SOC, default=""): str,
                vol.Optional(CONF_EV_ENTITY_DEPARTURE_TIME, default=""): str,
                vol.Optional(CONF_EV_ENTITY_CHARGER_SWITCH, default=""): str,
            }),
        )

    async def async_step_sources(self, user_input: dict | None = None) -> FlowResult:
        """Step 5: Nordpool and solar forecast entity selection."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="sunSale", data=self._data)

        return self.async_show_form(
            step_id="sources",
            data_schema=vol.Schema({
                vol.Required(CONF_NORDPOOL_ENTITY): str,
                vol.Optional(CONF_SOLAR_FORECAST_ENTITY, default=""): str,
            }),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "SunSaleOptionsFlow":
        return SunSaleOptionsFlow(config_entry)


class SunSaleOptionsFlow(config_entries.OptionsFlow):
    """Handle post-setup option changes (tariff parameters)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        data = {**self._entry.data, **self._entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_TARIFF_DISTRIBUTION_FEE, default=data.get(CONF_TARIFF_DISTRIBUTION_FEE, 0.03)): vol.Coerce(float),
                vol.Required(CONF_TARIFF_TAX_RATE, default=data.get(CONF_TARIFF_TAX_RATE, 0.21)): vol.Coerce(float),
                vol.Required(CONF_TARIFF_MARKUP, default=data.get(CONF_TARIFF_MARKUP, 0.005)): vol.Coerce(float),
                vol.Required(CONF_TARIFF_SELL_DISTRIBUTION_FEE, default=data.get(CONF_TARIFF_SELL_DISTRIBUTION_FEE, 0.01)): vol.Coerce(float),
                vol.Required(CONF_TARIFF_SELL_TAX_RATE, default=data.get(CONF_TARIFF_SELL_TAX_RATE, 0.0)): vol.Coerce(float),
                vol.Required(CONF_TARIFF_SELL_MARKUP, default=data.get(CONF_TARIFF_SELL_MARKUP, 0.0)): vol.Coerce(float),
            }),
        )
