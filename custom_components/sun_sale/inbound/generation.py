"""Generation stage: today-total HA-edge reader + ObservedGenerationSeries assembly.

The `GenerationTranslator` snapshots the inverter's daily-resetting cumulative
kWh counter, producing a `GenerationReading` per coordinator cycle. The
`build_observed_generation_series` function then turns the persisted rolling
sample history into a per-slot kWh series spanning yesterday 00:00 → now at
the price grid (Nordpool resolution).

Per slot: `generated_kwh = today_total(slot.end) - today_total(slot.start)`,
where `today_total(t)` is estimated by linear interpolation across the
samples of t's LOCAL day (with the implicit anchor: today_total = 0 at LOCAL
midnight, matching the inverter counter reset). Slots whose end falls past
`now` are clamped to `now`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from datetime import tzinfo as TzInfo
from typing import Any

from ..contract.models import (
    GenerationHistory,
    GenerationReading,
    ObservedGenerationSeries,
    ObservedGenerationSlot,
    SunSaleConfig,
)


def build_observed_generation_series(
    history: GenerationHistory,
    price_slots: tuple,
    now: datetime | None = None,
    local_tz: TzInfo = timezone.utc,
) -> ObservedGenerationSeries:
    """Derive per-slot observed generation from persisted today-total counter samples.

    Emits one ObservedGenerationSlot per price-grid slot in [yesterday 00:00, now).
    Energy per slot is the difference of linearly-interpolated counter values at
    slot boundaries. Day boundaries are computed in local time so they align with
    the inverter counter reset (which happens at local midnight).

    Args:
        history: Persisted rolling sample history of the today-total counter.
        price_slots: Price-grid slots defining the output resolution.
        now: Cycle timestamp; defaults to UTC now.
        local_tz: Local timezone for day-boundary calculations; defaults to UTC.

    Returns:
        ObservedGenerationSeries covering yesterday 00:00 local → now, grid-aligned.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if not price_slots or not history.samples:
        return ObservedGenerationSeries(slots=(), computed_at=now)

    yesterday_start = _day_start(now, local_tz) - timedelta(days=1)
    samples_by_day = _group_samples_by_day(history.samples, local_tz)

    slots: list[ObservedGenerationSlot] = []
    for ps in price_slots:
        if ps.start < yesterday_start or ps.start >= now:
            continue
        end_t = ps.end if ps.end < now else now
        if end_t <= ps.start:
            continue
        start_val = _total_at(ps.start, samples_by_day, local_tz)
        end_val = _total_at(end_t, samples_by_day, local_tz)
        kwh = end_val - start_val
        if kwh < 0:
            kwh = 0.0
        slots.append(ObservedGenerationSlot(
            start=ps.start,
            end=ps.end,
            generated_kwh=round(kwh, 6),
            source="inverter",
        ))

    totals = _compute_totals(slots, now, local_tz)
    return ObservedGenerationSeries(
        slots=tuple(slots),
        computed_at=now,
        total_yesterday_kwh=totals["yesterday"],
        total_today_so_far_kwh=totals["today"],
    )


def _group_samples_by_day(
    samples: tuple[GenerationReading, ...],
    local_tz: TzInfo,
) -> dict[datetime, list[GenerationReading]]:
    """Group samples by local-midnight day, retaining only the post-last-reset segment.

    The inverter counter resets at local midnight, so grouping by local day matches
    the counter's natural reset boundary. Within a day, a reset
    (curr.today_total_kwh < prev.today_total_kwh) starts a new segment. We keep
    the segment that ends the day so interpolation uses the live counter rather
    than a stale pre-reset value.

    Args:
        samples: Tuple of GenerationReadings in any order.
        local_tz: Timezone used to determine local-day boundaries.

    Returns:
        Dict mapping each local-midnight (as UTC datetime) to its cleaned sample list.
    """
    by_day: dict[datetime, list[GenerationReading]] = {}
    for s in sorted(samples, key=lambda x: x.timestamp):
        day = _day_start(s.timestamp, local_tz)
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
    t: datetime,
    samples_by_day: dict[datetime, list[GenerationReading]],
    local_tz: TzInfo,
) -> float:
    """Estimate the cumulative today-total counter value at time t via linear interpolation.

    Anchored at (local midnight, 0.0) — matching the inverter's reset point;
    linearly interpolated to the first sample; linearly interpolated between
    adjacent samples; clamped to the last sample after it. Returns 0.0 when the
    day has no samples.

    Args:
        t: Time at which to estimate the counter (tz-aware UTC).
        samples_by_day: Output of _group_samples_by_day.
        local_tz: Timezone used to determine local-day boundaries.

    Returns:
        Estimated cumulative kWh at time t.
    """
    day = _day_start(t, local_tz)
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
    slots: list[ObservedGenerationSlot], now: datetime, local_tz: TzInfo
) -> dict[str, float]:
    """Sum observed generation slots into yesterday/today totals.

    Uses local-date classification so slots spanning local midnight are
    attributed to the correct local day.

    Args:
        slots: ObservedGenerationSlots in any order.
        now: Reference time used to determine today's and yesterday's local dates.
        local_tz: Timezone for local-date classification.

    Returns:
        Dict with keys "yesterday" and "today" in kWh.
    """
    local_now = now.astimezone(local_tz)
    today = local_now.date()
    yesterday = today - timedelta(days=1)
    yest_sum = 0.0
    today_sum = 0.0
    for s in slots:
        d = s.start.astimezone(local_tz).date()
        if d == yesterday:
            yest_sum += s.generated_kwh
        elif d == today:
            today_sum += s.generated_kwh
    return {"yesterday": round(yest_sum, 4), "today": round(today_sum, 4)}


def _day_start(t: datetime, local_tz: TzInfo) -> datetime:
    """Return local midnight for t's local day, expressed as a UTC-aware datetime.

    This is the zero-anchor for inverter counter interpolation: the counter
    resets to 0 at local midnight, so all per-slot differencing is relative
    to that instant.

    Args:
        t: Any tz-aware datetime.
        local_tz: Timezone defining "local midnight".

    Returns:
        UTC-aware datetime of local midnight on t's local date.
    """
    local_t = t.astimezone(local_tz)
    local_midnight = local_t.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)


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
        """Initialise with the HA entity ID of the inverter solar-energy today sensor.

        Args:
            entity_id: Entity ID of the cumulative today-total kWh sensor.
        """
        self._entity_id = entity_id

    def parse(self, hass: Any, now: datetime | None = None) -> GenerationReading | None:
        """Read the today-total sensor and return a timestamped snapshot.

        Args:
            hass: Home Assistant instance.
            now: Snapshot timestamp; defaults to UTC now.

        Returns:
            GenerationReading with the current counter value, or None when the
            entity is absent, unavailable, or not configured.
        """
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
        """DAG translator entry-point; delegates to parse().

        Args:
            hass: Home Assistant instance.
            config: Structured SunSale config (unused here).
            raw_config: Raw config-entry dict (unused here).
            now: Cycle timestamp.

        Returns:
            GenerationReading or None when the sensor is unavailable.
        """
        return self.parse(hass, now)
