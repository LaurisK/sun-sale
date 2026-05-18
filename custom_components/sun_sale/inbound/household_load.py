"""Household-load stage: read the household-load sensor; produce HouseholdLoadReading.

The reading is None when the sensor is absent or unavailable, so the persisted
baseload history isn't polluted by a default value (see
docs/base_load_missing.md §8). The `BatteryTranslator` reads the same entity
but substitutes a 0.2 kW stub so the per-cycle BatteryReading always has a
load number for downstream consumers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..contract.models import HouseholdLoadReading, SunSaleConfig


class HouseholdLoadTranslator:
    """Reads the household-load sensor; produces HouseholdLoadReading."""

    output_type = HouseholdLoadReading

    def __init__(self, entity_id: str) -> None:
        """Initialise with the HA entity ID of the household power sensor.

        Args:
            entity_id: Entity ID of the household load sensor (watts).
        """
        self._entity_id = entity_id

    def parse(
        self, hass: Any, now: datetime | None = None
    ) -> HouseholdLoadReading | None:
        """Read the household load sensor and return a timestamped reading in kW.

        Returns None (not a fallback value) when the sensor is absent or
        unavailable, so the persisted load history is not polluted.

        Args:
            hass: Home Assistant instance.
            now: Snapshot timestamp; defaults to UTC now.

        Returns:
            HouseholdLoadReading in kW, or None when unavailable.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        if not self._entity_id:
            return None
        state = hass.states.get(self._entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return None
        try:
            value_w = float(state.state)
        except (ValueError, TypeError):
            return None
        return HouseholdLoadReading(
            timestamp=now, load_kw=max(0.0, value_w / 1000.0),
        )

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> HouseholdLoadReading | None:
        """DAG translator entry-point; delegates to parse().

        Args:
            hass: Home Assistant instance.
            config: Structured SunSale config (unused here).
            raw_config: Raw config-entry dict (unused here).
            now: Cycle timestamp.

        Returns:
            HouseholdLoadReading or None when unavailable.
        """
        return self.parse(hass, now)
