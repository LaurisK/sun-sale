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
    """Combine live telemetry with configured limits into a BatteryStatus."""
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
        self._inverter = inverter
        self._load_entity = household_load_entity

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> BatteryReading:
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
