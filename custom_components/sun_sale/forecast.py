"""Forecast stage: normalise solar generation data into GenerationSeries."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import GenerationSlot, GenerationSeries, PriceSeries

_LOGGER = logging.getLogger(__name__)


def build_generation_series(
    hass: Any,
    config: dict,
    price_series: PriceSeries,
    now: datetime | None = None,
) -> GenerationSeries:
    """Read solar forecast from HA state machine and normalise into GenerationSeries."""
    from .const import CONF_SOLAR_FORECAST_ENTITY, CONF_SOLAR_FORECAST_ENTITY_2

    if now is None:
        now = datetime.now(timezone.utc)

    entity_1: str = config.get(CONF_SOLAR_FORECAST_ENTITY, "")
    entity_2: str = config.get(CONF_SOLAR_FORECAST_ENTITY_2, "")

    # Try Open Meteo (watts attribute at 15-min resolution) first
    combined_watts: dict[datetime, float] = {}
    for base_eid in (entity_1, entity_2):
        if not base_eid:
            continue
        for eid in (base_eid, _tomorrow_entity(base_eid)):
            if not eid:
                continue
            state = hass.states.get(eid)
            if state is None:
                continue
            watts = state.attributes.get("watts")
            if not isinstance(watts, dict):
                continue
            for ts_str, w in watts.items():
                try:
                    dt = datetime.fromisoformat(str(ts_str))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    slot_utc = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
                    combined_watts[slot_utc] = combined_watts.get(slot_utc, 0.0) + float(w)
                except (ValueError, TypeError):
                    continue

    if combined_watts:
        # Aggregate 15-min watts → hourly kWh (W × 0.25 h / 1000)
        hourly: dict[datetime, float] = {}
        for slot_utc, w in combined_watts.items():
            hour = slot_utc.replace(minute=0)
            hourly[hour] = hourly.get(hour, 0.0) + w * 0.25 / 1000

        slots = tuple(
            GenerationSlot(
                start=h,
                end=h + timedelta(hours=1),
                expected_kwh=round(kwh, 4),
                source="open_meteo",
                confidence=None,
            )
            for h, kwh in sorted(hourly.items())
        )
        return GenerationSeries(slots=slots, primary="open_meteo", overlays=(), computed_at=now)

    # Fallback: Forecast.Solar / Solcast "forecast" attribute
    if entity_1:
        state = hass.states.get(entity_1)
        if state is not None:
            slots_list: list[GenerationSlot] = []
            for entry in state.attributes.get("forecast", []):
                try:
                    start = datetime.fromisoformat(entry["time"]).replace(tzinfo=timezone.utc)
                    kwh = float(entry.get("pv_estimate", entry.get("energy", 0.0)))
                    slots_list.append(GenerationSlot(
                        start=start,
                        end=start + timedelta(hours=1),
                        expected_kwh=kwh,
                        source="forecast_solar",
                        confidence=None,
                    ))
                except (KeyError, ValueError):
                    continue
            if slots_list:
                return GenerationSeries(
                    slots=tuple(slots_list),
                    primary="forecast_solar",
                    overlays=(),
                    computed_at=now,
                )

    return GenerationSeries(slots=(), primary="none", overlays=(), computed_at=now)


def _tomorrow_entity(entity_id: str) -> str:
    for pattern in ("_today_", "_today"):
        if pattern in entity_id:
            return entity_id.replace(pattern, pattern.replace("today", "tomorrow"), 1)
    return ""
