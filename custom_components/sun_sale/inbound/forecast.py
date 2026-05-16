"""Forecast stage: normalise SolarData into GenerationSeries.

Pure Python — no Home Assistant imports.
Called by GenerationNode (Tier 2 DAG node).
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..contract.models import GenerationSlot, GenerationSeries, PriceSeries, SolarData


def build_generation_series(
    solar: SolarData,
    price_series: PriceSeries,
    now: datetime | None = None,
) -> GenerationSeries:
    """Convert SolarData into a GenerationSeries aligned to the pipeline."""
    if now is None:
        now = datetime.now(timezone.utc)

    if not solar.entries:
        return GenerationSeries(slots=(), primary="none", overlays=(), computed_at=now)

    slots = tuple(
        GenerationSlot(
            start=e.start,
            end=e.end,
            expected_kwh=e.expected_kwh,
            source=e.source,
            confidence=None,
        )
        for e in solar.entries
    )
    return GenerationSeries(
        slots=slots,
        primary=solar.primary_source,
        overlays=(),
        computed_at=now,
    )
