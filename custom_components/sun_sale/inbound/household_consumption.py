"""Household-consumption stage: read the today-total kWh counter sensor.

Mirrors `inbound/generation.py` but for the household-load energy counter
(e.g. `sensor.namai_inv_household_load_today_energy_2`). The sensor is a
cumulative kWh counter that resets at local midnight; this translator only
takes a snapshot per cycle so downstream consumers can display
"consumption so far today" without re-deriving it from instantaneous
load samples.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..contract.models import HouseholdConsumptionReading, SunSaleConfig


class HouseholdConsumptionTranslator:
    """Reads the household-consumption today-total sensor; produces HouseholdConsumptionReading."""

    output_type = HouseholdConsumptionReading

    def __init__(self, entity_id: str) -> None:
        self._entity_id = entity_id

    def parse(
        self, hass: Any, now: datetime | None = None
    ) -> HouseholdConsumptionReading | None:
        if now is None:
            now = datetime.now(timezone.utc)
        if not self._entity_id:
            return None
        state = hass.states.get(self._entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        return HouseholdConsumptionReading(today_total_kwh=value, timestamp=now)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> HouseholdConsumptionReading | None:
        return self.parse(hass, now)
