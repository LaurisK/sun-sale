"""Generation stage: today-total HA-edge reader + ObservedGenerationSeries assembly.

The `GenerationTranslator` snapshots the inverter's daily-resetting cumulative
kWh counter, producing a `GenerationReading` per coordinator cycle. The
`build_observed_generation_series` function then turns the persisted rolling
sample history into a per-slot kWh series spanning yesterday 00:00 → now at
the price grid (Nordpool resolution).

Per slot: `generated_kwh = today_total(slot.end) - today_total(slot.start)`,
where `today_total(t)` is estimated by linear interpolation across the
samples of t's UTC day (with the implicit anchor: today_total = 0 at UTC
midnight). Slots whose end falls past `now` are clamped to `now`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..contract.models import (
    GenerationHistory,
    GenerationReading,
    ObservedGenerationSeries,
    ObservedGenerationSlot,
    PriceSeries,
    SunSaleConfig,
)


def build_observed_generation_series(
    history: GenerationHistory,
    price_series: PriceSeries,
    now: datetime | None = None,
) -> ObservedGenerationSeries:
    """Emit one slot per price-grid slot in [yesterday 00:00, now)."""
    if now is None:
        now = datetime.now(timezone.utc)

    if not price_series.slots or not history.samples:
        return ObservedGenerationSeries(slots=(), computed_at=now)

    yesterday_start = _utc_midnight(now) - timedelta(days=1)
    samples_by_day = _group_samples_by_day(history.samples)

    slots: list[ObservedGenerationSlot] = []
    for ps in price_series.slots:
        if ps.start < yesterday_start or ps.start >= now:
            continue
        end_t = ps.end if ps.end < now else now
        if end_t <= ps.start:
            continue
        start_val = _total_at(ps.start, samples_by_day)
        end_val = _total_at(end_t, samples_by_day)
        kwh = end_val - start_val
        if kwh < 0:
            kwh = 0.0
        slots.append(ObservedGenerationSlot(
            start=ps.start,
            end=ps.end,
            generated_kwh=round(kwh, 6),
            source="inverter",
        ))

    totals = _compute_totals(slots, now)
    return ObservedGenerationSeries(
        slots=tuple(slots),
        computed_at=now,
        total_yesterday_kwh=totals["yesterday"],
        total_today_so_far_kwh=totals["today"],
    )


def _group_samples_by_day(
    samples: tuple[GenerationReading, ...],
) -> dict[datetime, list[GenerationReading]]:
    """Group samples by UTC-midnight day; keep only the post-last-reset segment.

    Within a day, a reset (curr.today_total_kwh < prev.today_total_kwh) starts
    a new segment. We retain the segment ending the day so interpolation uses
    the live counter rather than a stale pre-reset value.
    """
    by_day: dict[datetime, list[GenerationReading]] = {}
    for s in sorted(samples, key=lambda x: x.timestamp):
        day = _utc_midnight(s.timestamp)
        by_day.setdefault(day, []).append(s)

    cleaned: dict[datetime, list[GenerationReading]] = {}
    for day, day_samples in by_day.items():
        current = [day_samples[0]]
        last_segment = current
        for s in day_samples[1:]:
            if s.today_total_kwh < current[-1].today_total_kwh:
                current = [s]
                last_segment = current
            else:
                current.append(s)
        cleaned[day] = last_segment
    return cleaned


def _total_at(
    t: datetime, samples_by_day: dict[datetime, list[GenerationReading]]
) -> float:
    """Estimate the cumulative today-counter at time `t`.

    Anchored at (UTC midnight, 0); linearly interpolated to the first sample
    of the day; linearly interpolated between adjacent samples; clamped to the
    last sample's value after it. Returns 0 when the day has no samples.
    """
    day = _utc_midnight(t)
    day_samples = samples_by_day.get(day)
    if not day_samples:
        return 0.0

    first = day_samples[0]
    if t <= first.timestamp:
        span = (first.timestamp - day).total_seconds()
        if span <= 0:
            return first.today_total_kwh
        ratio = (t - day).total_seconds() / span
        return ratio * first.today_total_kwh

    last = day_samples[-1]
    if t >= last.timestamp:
        return last.today_total_kwh

    for i in range(len(day_samples) - 1):
        a, b = day_samples[i], day_samples[i + 1]
        if a.timestamp <= t <= b.timestamp:
            span = (b.timestamp - a.timestamp).total_seconds()
            if span <= 0:
                return b.today_total_kwh
            ratio = (t - a.timestamp).total_seconds() / span
            return a.today_total_kwh + ratio * (b.today_total_kwh - a.today_total_kwh)

    return last.today_total_kwh


def _compute_totals(
    slots: list[ObservedGenerationSlot], now: datetime
) -> dict[str, float]:
    today = now.date()
    yesterday = today - timedelta(days=1)
    yest_sum = 0.0
    today_sum = 0.0
    for s in slots:
        d = s.start.date()
        if d == yesterday:
            yest_sum += s.generated_kwh
        elif d == today:
            today_sum += s.generated_kwh
    return {"yesterday": round(yest_sum, 4), "today": round(today_sum, 4)}


def _utc_midnight(t: datetime) -> datetime:
    return datetime(t.year, t.month, t.day, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Generation translator (HA-edge reader)
# ---------------------------------------------------------------------------

class GenerationTranslator:
    """Reads the inverter today-total generation sensor; produces GenerationReading.

    The sensor is a cumulative kWh counter that resets at local midnight; this
    translator only takes a snapshot. Differencing across samples is done by
    `build_observed_generation_series` after the coordinator stitches the
    persisted history.
    """

    output_type = GenerationReading

    def __init__(self, entity_id: str) -> None:
        self._entity_id = entity_id

    def parse(self, hass: Any, now: datetime | None = None) -> GenerationReading | None:
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
        return GenerationReading(today_total_kwh=value, timestamp=now)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> GenerationReading | None:
        return self.parse(hass, now)
