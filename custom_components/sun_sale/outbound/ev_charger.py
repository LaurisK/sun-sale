"""EV charger control abstraction.

Translates generic EV commands (start, stop) into platform-specific
Home Assistant service calls.
"""
from __future__ import annotations

from enum import Enum

from homeassistant.core import HomeAssistant


class EVChargerPlatform(Enum):
    OPENEVSE = "openevse"
    EASEE = "easee"
    WALLBOX = "wallbox"
    GENERIC = "generic"


class EVChargerController:
    """Translates generic EV charging commands into platform-specific HA service calls.

    entity_ids keys:
      - "plug_state":       binary_sensor (on = EV connected)
      - "soc":              sensor for EV battery % (optional)
      - "charger_switch":   switch or charger ID used to start/stop
      - "charge_current":   number entity for current setpoint (amps, optional)
    """

    def __init__(
        self,
        hass: HomeAssistant,
        platform: EVChargerPlatform,
        entity_ids: dict[str, str],
    ) -> None:
        self._hass = hass
        self._platform = platform
        self._entity_ids = entity_ids

    # ------------------------------------------------------------------ #
    # Commands                                                             #
    # ------------------------------------------------------------------ #

    async def async_start_charging(self, power_kw: float) -> None:
        """Start EV charging at the given power."""
        switch = self._entity_ids.get("charger_switch", "")
        current_entity = self._entity_ids.get("charge_current", "")

        if self._platform == EVChargerPlatform.OPENEVSE:
            amps = int((power_kw * 1000.0) / 230.0)  # 230 V single-phase
            if current_entity:
                await self._hass.services.async_call(
                    "number", "set_value",
                    {"entity_id": current_entity, "value": amps},
                    blocking=True,
                )
            await self._hass.services.async_call(
                "switch", "turn_on", {"entity_id": switch}, blocking=True,
            )
        elif self._platform == EVChargerPlatform.EASEE:
            await self._hass.services.async_call(
                "easee", "start_charging", {"charger_id": switch}, blocking=True,
            )
        elif self._platform == EVChargerPlatform.WALLBOX:
            await self._hass.services.async_call(
                "wallbox", "start_charging", {"entity_id": switch}, blocking=True,
            )
        else:  # GENERIC
            await self._hass.services.async_call(
                "switch", "turn_on", {"entity_id": switch}, blocking=True,
            )

    async def async_stop_charging(self) -> None:
        """Stop EV charging."""
        switch = self._entity_ids.get("charger_switch", "")

        if self._platform == EVChargerPlatform.EASEE:
            await self._hass.services.async_call(
                "easee", "stop_charging", {"charger_id": switch}, blocking=True,
            )
        elif self._platform == EVChargerPlatform.WALLBOX:
            await self._hass.services.async_call(
                "wallbox", "stop_charging", {"entity_id": switch}, blocking=True,
            )
        else:
            await self._hass.services.async_call(
                "switch", "turn_off", {"entity_id": switch}, blocking=True,
            )

    # ------------------------------------------------------------------ #
    # State reads                                                          #
    # ------------------------------------------------------------------ #

    def is_plugged_in(self) -> bool:
        """Return True if EV is connected to the charger."""
        entity_id = self._entity_ids.get("plug_state", "")
        state = self._hass.states.get(entity_id)
        if state is None:
            return False
        return state.state == "on"

    def get_ev_soc(self) -> float | None:
        """Return EV battery SoC as 0.0–1.0, or None if unavailable."""
        entity_id = self._entity_ids.get("soc", "")
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return None
        try:
            value = float(state.state)
            return value / 100.0 if value > 1.0 else value
        except ValueError:
            return None
