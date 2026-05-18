"""Battery stage: BatteryReading HA-edge reader + BatteryStatus assembly.

The `BatteryTranslator` reads inverter telemetry (SoC, battery power, grid
power) via `InverterController` plus the household-load sensor, producing a
`BatteryReading`. The `build_battery_status` function then combines that
live reading with the configured nominal capacity to produce a `BatteryStatus`
for downstream DAG nodes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..contract.models import (
    BatteryConfig,
    BatteryReading,
    BatteryStatus,
    SunSaleConfig,
)
from ..outbound.inverter import InverterController

_DEFAULT_HOUSEHOLD_LOAD_KW = 0.2


def build_battery_status(
    reading: BatteryReading,
    config: BatteryConfig,
) -> BatteryStatus:
    """Combine live inverter telemetry with configured limits into a BatteryStatus snapshot.

    Args:
        reading: Current per-cycle inverter reading (SoC, power).
        config: Battery configuration (nominal capacity, power limits).

    Returns:
        BatteryStatus with remaining_capacity_kwh derived from SoC × nominal capacity.
    """
    return BatteryStatus(
        total_capacity_kwh=config.nominal_capacity_kwh,
        max_charge_power_kw=config.max_charge_power_kw,
        max_discharge_power_kw=config.max_discharge_power_kw,
        soc=reading.soc,
        remaining_capacity_kwh=reading.soc * config.nominal_capacity_kwh,
    )


# ---------------------------------------------------------------------------
# Battery translator (HA-edge reader)
# ---------------------------------------------------------------------------

def _read_household_load(hass: Any, entity_id: str) -> float:
    """Read household load from a HA power sensor in watts; returns kW.

    Falls back to _DEFAULT_HOUSEHOLD_LOAD_KW when the entity is absent,
    unavailable, or unparseable, so downstream consumers always receive a number.

    Args:
        hass: Home Assistant instance.
        entity_id: Entity ID of the household load sensor (watts); empty → fallback.

    Returns:
        Current household load in kW.
    """
    if not entity_id:
        return _DEFAULT_HOUSEHOLD_LOAD_KW
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown", ""):
        return _DEFAULT_HOUSEHOLD_LOAD_KW
    try:
        return max(0.0, float(state.state)) / 1000.0  # W → kW
    except ValueError:
        return _DEFAULT_HOUSEHOLD_LOAD_KW


class BatteryTranslator:
    """Reads inverter telemetry and household load; produces BatteryReading."""

    output_type = BatteryReading

    def __init__(
        self,
        inverter: InverterController,
        household_load_entity: str,
    ) -> None:
        """Initialise translator with the inverter controller and load sensor entity.

        Args:
            inverter: Platform-agnostic inverter abstraction.
            household_load_entity: HA entity ID of the household power sensor (watts).
        """
        self._inverter = inverter
        self._load_entity = household_load_entity

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> BatteryReading:
        """Read inverter telemetry and household load; produce a BatteryReading.

        Args:
            hass: Home Assistant instance.
            config: Structured SunSale config (unused here).
            raw_config: Raw config-entry dict (unused here).
            now: Cycle timestamp (unused here).

        Returns:
            BatteryReading with current SoC, power flows, and household load.
        """
        soc = self._inverter.get_battery_soc()
        power_kw = self._inverter.get_battery_power()
        grid_kw = self._inverter.get_grid_power()
        load_kw = _read_household_load(hass, self._load_entity)
        return BatteryReading(
            soc=soc,
            power_kw=power_kw,
            grid_power_kw=grid_kw,
            household_load_kw=load_kw,
        )
