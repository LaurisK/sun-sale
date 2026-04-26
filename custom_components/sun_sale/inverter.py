"""Inverter control abstraction.

Translates generic battery commands (charge, discharge, idle) into
platform-specific Home Assistant service calls.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from homeassistant.core import HomeAssistant

from .models import BatteryConfig


class InverterPlatform(Enum):
    HUAWEI_SOLAR = "huawei_solar"
    SOLAREDGE = "solaredge"
    GOODWE = "goodwe"
    SOLIS = "solis_modbus"
    GENERIC = "generic"


class InverterController:
    """Translates generic battery commands into platform-specific HA service calls.

    entity_ids keys for non-Solis platforms:
      - "battery_soc":      sensor reporting SoC (0–100 % or 0.0–1.0)
      - "battery_power":    sensor reporting battery power (kW, positive=charging)
      - "grid_power":       sensor reporting grid power (kW, positive=importing)
      - "charge_control":   number/switch entity used to command the inverter

    entity_ids keys for Solis (solis_modbus):
      - "battery_soc", "battery_power", "grid_power" as above
      - "solis_charge_current"          number entity for charge amps
      - "solis_discharge_current"       number entity for discharge amps
      - "solis_charge_start_hour_1"     number entity, slot-1 charge start hour
      - "solis_charge_start_minute_1"   number entity, slot-1 charge start minute
      - "solis_charge_end_hour_1"       number entity, slot-1 charge end hour
      - "solis_charge_end_minute_1"     number entity, slot-1 charge end minute
      - "solis_discharge_start_hour_1"  number entity, slot-1 discharge start hour
      - "solis_discharge_start_minute_1"
      - "solis_discharge_end_hour_1"
      - "solis_discharge_end_minute_1"
      - "solis_tou_mode_switch"         switch entity for TOU mode
      - "solis_allow_grid_charge_switch" switch entity to allow grid charging
      - "solis_self_use_mode_switch"    switch entity for self-use / idle mode
    """

    def __init__(
        self,
        hass: HomeAssistant,
        platform: InverterPlatform,
        entity_ids: dict[str, str],
        battery_config: Optional[BatteryConfig] = None,
    ) -> None:
        self._hass = hass
        self._platform = platform
        self._entity_ids = entity_ids
        self._battery_config = battery_config

    # ------------------------------------------------------------------ #
    # Commands                                                             #
    # ------------------------------------------------------------------ #

    async def async_charge_from_grid(self, power_kw: float) -> None:
        """Command inverter to charge battery from the grid at given power."""
        await self._async_dispatch("charge", power_kw)

    async def async_discharge_to_grid(self, power_kw: float) -> None:
        """Command inverter to discharge battery to the grid."""
        await self._async_dispatch("discharge", power_kw)

    async def async_idle(self) -> None:
        """Stop charge/discharge — let solar self-consume."""
        await self._async_dispatch("idle", 0.0)

    # ------------------------------------------------------------------ #
    # State reads                                                          #
    # ------------------------------------------------------------------ #

    def get_battery_soc(self) -> float:
        """Return battery SoC as 0.0–1.0. Falls back to 0.5 when unavailable."""
        return self._read_float("battery_soc", fallback=0.5, normalize_pct=True)

    def get_battery_power(self) -> float:
        """Return battery power in kW (positive = charging). 0.0 when unavailable."""
        return self._read_float("battery_power", fallback=0.0)

    def get_grid_power(self) -> float:
        """Return grid power in kW (positive = importing). 0.0 when unavailable."""
        return self._read_float("grid_power", fallback=0.0)

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    def _read_float(
        self, key: str, fallback: float, normalize_pct: bool = False
    ) -> float:
        entity_id = self._entity_ids.get(key, "")
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return fallback
        try:
            value = float(state.state)
            if normalize_pct and value > 1.0:
                return value / 100.0
            return value
        except ValueError:
            return fallback

    async def _async_dispatch(self, mode: str, power_kw: float) -> None:
        if self._platform == InverterPlatform.HUAWEI_SOLAR:
            # Huawei Solar: positive W = charge, negative W = discharge, 0 = idle
            if mode == "charge":
                watt_value = power_kw * 1000.0
            elif mode == "discharge":
                watt_value = -(power_kw * 1000.0)
            else:
                watt_value = 0.0
            await self._hass.services.async_call(
                "number", "set_value",
                {"entity_id": self._entity_ids.get("charge_control", ""), "value": watt_value},
                blocking=True,
            )
        elif self._platform == InverterPlatform.SOLIS:
            await self._async_dispatch_solis(mode, power_kw)
        else:
            # Generic path: SOLAREDGE, GOODWE, GENERIC
            # positive kW = charge, negative = discharge, 0 = idle
            if mode == "charge":
                value = power_kw
            elif mode == "discharge":
                value = -power_kw
            else:
                value = 0.0
            await self._hass.services.async_call(
                "number", "set_value",
                {"entity_id": self._entity_ids.get("charge_control", ""), "value": value},
                blocking=True,
            )

    async def _async_dispatch_solis(self, mode: str, power_kw: float) -> None:
        """Dispatch to Solis inverter via TOU slot 1."""
        cfg = self._battery_config
        nominal_v = cfg.nominal_voltage_v if cfg is not None else 48.0

        now = datetime.now(timezone.utc)
        start_hour = now.hour
        start_minute = (now.minute // 5) * 5
        end_hour = (now.hour + 1) % 24
        end_minute = 0

        if mode == "charge":
            max_amps = ((cfg.max_charge_power_kw * 1000) / nominal_v) if cfg is not None else float("inf")
            amps = min(max(power_kw * 1000 / nominal_v, 0.0), max_amps)

            await self._hass.services.async_call(
                "number", "set_value",
                {"entity_id": self._entity_ids["solis_charge_current"], "value": amps},
                blocking=True,
            )
            await self._hass.services.async_call(
                "number", "set_value",
                {"entity_id": self._entity_ids["solis_charge_start_hour_1"], "value": start_hour},
                blocking=True,
            )
            await self._hass.services.async_call(
                "number", "set_value",
                {"entity_id": self._entity_ids["solis_charge_start_minute_1"], "value": start_minute},
                blocking=True,
            )
            await self._hass.services.async_call(
                "number", "set_value",
                {"entity_id": self._entity_ids["solis_charge_end_hour_1"], "value": end_hour},
                blocking=True,
            )
            await self._hass.services.async_call(
                "number", "set_value",
                {"entity_id": self._entity_ids["solis_charge_end_minute_1"], "value": end_minute},
                blocking=True,
            )
            await self._hass.services.async_call(
                "switch", "turn_on",
                {"entity_id": self._entity_ids["solis_allow_grid_charge_switch"]},
                blocking=True,
            )
            await self._hass.services.async_call(
                "switch", "turn_on",
                {"entity_id": self._entity_ids["solis_tou_mode_switch"]},
                blocking=True,
            )

        elif mode == "discharge":
            max_amps = ((cfg.max_discharge_power_kw * 1000) / nominal_v) if cfg is not None else float("inf")
            amps = min(max(power_kw * 1000 / nominal_v, 0.0), max_amps)

            await self._hass.services.async_call(
                "number", "set_value",
                {"entity_id": self._entity_ids["solis_discharge_current"], "value": amps},
                blocking=True,
            )
            await self._hass.services.async_call(
                "number", "set_value",
                {"entity_id": self._entity_ids["solis_discharge_start_hour_1"], "value": start_hour},
                blocking=True,
            )
            await self._hass.services.async_call(
                "number", "set_value",
                {"entity_id": self._entity_ids["solis_discharge_start_minute_1"], "value": start_minute},
                blocking=True,
            )
            await self._hass.services.async_call(
                "number", "set_value",
                {"entity_id": self._entity_ids["solis_discharge_end_hour_1"], "value": end_hour},
                blocking=True,
            )
            await self._hass.services.async_call(
                "number", "set_value",
                {"entity_id": self._entity_ids["solis_discharge_end_minute_1"], "value": end_minute},
                blocking=True,
            )
            await self._hass.services.async_call(
                "switch", "turn_on",
                {"entity_id": self._entity_ids["solis_tou_mode_switch"]},
                blocking=True,
            )

        else:  # idle
            await self._hass.services.async_call(
                "switch", "turn_off",
                {"entity_id": self._entity_ids["solis_tou_mode_switch"]},
                blocking=True,
            )
            await self._hass.services.async_call(
                "switch", "turn_on",
                {"entity_id": self._entity_ids["solis_self_use_mode_switch"]},
                blocking=True,
            )
            await self._hass.services.async_call(
                "number", "set_value",
                {"entity_id": self._entity_ids["solis_charge_current"], "value": 0},
                blocking=True,
            )
            await self._hass.services.async_call(
                "number", "set_value",
                {"entity_id": self._entity_ids["solis_discharge_current"], "value": 0},
                blocking=True,
            )
