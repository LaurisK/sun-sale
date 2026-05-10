"""Forecast stage: normalise RawSolarData into GenerationSeries.

Pure Python — no Home Assistant imports.
Called by GenerationNode (Tier 2 DAG node).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import GenerationSlot, GenerationSeries, PriceSeries, RawSolarData


def build_generation_series(
    raw: RawSolarData,
    price_series: PriceSeries,
    now: datetime | None = None,
) -> GenerationSeries:
    """Convert RawSolarData + PriceSeries resolution into a GenerationSeries."""
    if now is None:
        now = datetime.now(timezone.utc)

    if raw.watts:
        return _build_from_watts(raw.watts, price_series, now)

    if raw.forecast_slots:
        return _build_from_forecast(raw.forecast_slots, now)

    return GenerationSeries(slots=(), primary="none", overlays=(), computed_at=now)


def _build_from_watts(
    watts: dict[datetime, float],
    price_series: PriceSeries,
    now: datetime,
) -> GenerationSeries:
    slot_dur = price_series.resolution
    slot_h = slot_dur.total_seconds() / 3600

    if slot_h >= 1.0:
        bucketed: dict[datetime, float] = {}
        for slot_utc, w in watts.items():
            hour = slot_utc.replace(minute=0)
            bucketed[hour] = bucketed.get(hour, 0.0) + w * 0.25 / 1000
        slots = tuple(
            GenerationSlot(
                start=h,
                end=h + slot_dur,
                expected_kwh=round(kwh, 4),
                source="open_meteo",
                confidence=None,
            )
            for h, kwh in sorted(bucketed.items())
        )
    else:
        slots = tuple(
            GenerationSlot(
                start=s,
                end=s + slot_dur,
                expected_kwh=round(w * slot_h / 1000, 4),
                source="open_meteo",
                confidence=None,
            )
            for s, w in sorted(watts.items())
        )

    return GenerationSeries(slots=slots, primary="open_meteo", overlays=(), computed_at=now)


def _build_from_forecast(forecast_slots: list[dict], now: datetime) -> GenerationSeries:
    slots_list: list[GenerationSlot] = []
    for entry in forecast_slots:
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
    if not slots_list:
        return GenerationSeries(slots=(), primary="none", overlays=(), computed_at=now)
    return GenerationSeries(
        slots=tuple(slots_list),
        primary="forecast_solar",
        overlays=(),
        computed_at=now,
    )
