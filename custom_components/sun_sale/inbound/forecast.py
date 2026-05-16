"""Forecast stage: normalise SolarData into GenerationSeries.

Pure Python — no Home Assistant imports.
Called by GenerationNode (Tier 2 DAG node).

Produces a continuous 72h GenerationSeries (yesterday 00:00 → tomorrow 23:59)
at PriceSeries granularity. How yesterday is obtained (persistent store, same
pattern as pricing) is invisible to consumers: every price slot gets exactly
one generation slot, zero-filled where solar coverage is absent. Exposes per-
day totals (yesterday / today / tomorrow) and today_remaining — nothing else.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..contract.models import (
    GenerationSeries,
    GenerationSlot,
    PriceSeries,
    SolarData,
    SolarEntry,
)


def build_generation_series(
    solar: SolarData,
    price_series: PriceSeries,
    now: datetime | None = None,
) -> GenerationSeries:
    """Convert SolarData into a GenerationSeries aligned to the price grid."""
    if now is None:
        now = datetime.now(timezone.utc)

    if not solar.entries or not price_series.slots:
        return GenerationSeries(
            slots=(),
            primary=solar.primary_source if solar.entries else "none",
            overlays=(),
            computed_at=now,
        )

    resampled = _resample_to_grid(solar.entries, price_series.slots, solar.primary_source)
    totals = _compute_totals(resampled, now)

    return GenerationSeries(
        slots=resampled,
        primary=solar.primary_source,
        overlays=(),
        computed_at=now,
        total_yesterday_kwh=totals["yesterday"],
        total_today_kwh=totals["today"],
        total_tomorrow_kwh=totals["tomorrow"],
        today_remaining_kwh=totals["today_remaining"],
    )


def _resample_to_grid(
    entries: list[SolarEntry],
    target_slots: tuple,
    source: str,
) -> tuple[GenerationSlot, ...]:
    """Redistribute entry kWh onto target slots by overlap-weighted area.

    Works for both downsampling (e.g. 15-min → 1h) and upsampling (1h → 15-min):
    each entry contributes ``expected_kwh * overlap / entry_duration`` to every
    target slot it intersects. Every target slot is emitted, even when no solar
    entries overlap it, so the output covers the full price grid continuously.
    """
    # Pre-extract entry spans once; entries may not be sorted strictly, but
    # we iterate over all of them per target slot anyway.
    spans: list[tuple[datetime, datetime, float, float]] = []
    for e in entries:
        dur = (e.end - e.start).total_seconds()
        if dur <= 0:
            continue
        spans.append((e.start, e.end, e.expected_kwh, dur))

    if not spans:
        return ()

    out: list[GenerationSlot] = []
    for t in target_slots:
        total = 0.0
        for e_start, e_end, e_kwh, e_dur in spans:
            ov_start = e_start if e_start > t.start else t.start
            ov_end = e_end if e_end < t.end else t.end
            ov_secs = (ov_end - ov_start).total_seconds()
            if ov_secs <= 0:
                continue
            total += e_kwh * (ov_secs / e_dur)
        out.append(GenerationSlot(
            start=t.start,
            end=t.end,
            expected_kwh=round(total, 6),
            source=source,
            confidence=None,
        ))
    return tuple(out)


def _compute_totals(slots: tuple[GenerationSlot, ...], now: datetime) -> dict[str, float]:
    """Bucket resampled slots into yesterday/today/tomorrow by start.date()."""
    today = now.date()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    yest_sum = 0.0
    today_sum = 0.0
    tomo_sum = 0.0
    today_remaining = 0.0
    for s in slots:
        d = s.start.date()
        if d == yesterday:
            yest_sum += s.expected_kwh
        elif d == today:
            today_sum += s.expected_kwh
            if s.start >= now:
                today_remaining += s.expected_kwh
        elif d == tomorrow:
            tomo_sum += s.expected_kwh

    return {
        "yesterday": round(yest_sum, 4),
        "today": round(today_sum, 4),
        "tomorrow": round(tomo_sum, 4),
        "today_remaining": round(today_remaining, 4),
    }
