"""Generation stage: HA-edge readers + ObservedGenerationSeries assembly.

Primary path — `PvPowerTranslator` snapshots the inverter's instantaneous PV
power (W) each coordinator cycle. `build_observed_generation_series` averages
those samples within each price-grid slot and converts to kWh:

    slot_kwh = mean(power_W in [slot.start, slot.end)) × slot_duration_h / 1000

Secondary (end-of-day correction) — `GenerationTranslator` snapshots the
inverter's daily-resetting cumulative kWh counter (updates every ~10 min).
After the power-averaged slots are built, the most recent counter value for
today is used to scale all today slots proportionally so their sum matches
the authoritative total:

    correction_factor = today_total_kwh / sum(today_slot_kwh)

The correction is skipped if the factor falls outside [0.5, 2.0] (sensor fault
or empty-generation guard).

Fallback — when no PV-power samples are available, per-slot kWh is derived
by differencing the today-total counter between slot boundaries, exactly as
in the original implementation.
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
    PvPowerHistory,
    PvPowerReading,
    SunSaleConfig,
)


def build_observed_generation_series(
    pv_power_history: PvPowerHistory,
    generation_history: GenerationHistory,
    price_slots: tuple,
    now: datetime | None = None,
    local_tz: TzInfo = timezone.utc,
) -> ObservedGenerationSeries:
    """Derive per-slot observed generation from PV power samples or counter history.

    Uses instantaneous PV power averaging as the primary method. Falls back to
    counter differencing when no power samples are available. Applies an
    end-of-day proportional correction whenever the today-total counter reading
    is present and the correction factor is within [0.5, 2.0].

    Args:
        pv_power_history: Rolling samples of instantaneous PV power (W).
        generation_history: Rolling samples of the today-total kWh counter.
        price_slots: Price-grid slots defining the output resolution.
        now: Cycle timestamp; defaults to UTC now.
        local_tz: Local timezone for day-boundary calculations.

    Returns:
        ObservedGenerationSeries covering yesterday 00:00 local → now, grid-aligned.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if not price_slots:
        return ObservedGenerationSeries(slots=(), computed_at=now)

    yesterday_start = _day_start(now, local_tz) - timedelta(days=1)

    if pv_power_history.samples:
        slots = _build_slots_from_power(
            pv_power_history.samples, price_slots, now, local_tz, yesterday_start
        )
    elif generation_history.samples:
        slots = _build_slots_from_counter(
            generation_history.samples, price_slots, now, local_tz, yesterday_start
        )
    else:
        return ObservedGenerationSeries(slots=(), computed_at=now)

    today_total = _latest_today_total(generation_history.samples, now, local_tz)
    slots = _apply_end_of_day_correction(slots, today_total, now, local_tz)

    totals = _compute_totals(slots, now, local_tz)
    return ObservedGenerationSeries(
        slots=tuple(slots),
        computed_at=now,
        total_yesterday_kwh=totals["yesterday"],
        total_today_so_far_kwh=totals["today"],
    )


def _build_slots_from_power(
    samples: tuple[PvPowerReading, ...],
    price_slots: tuple,
    now: datetime,
    local_tz: TzInfo,
    yesterday_start: datetime,
) -> list[ObservedGenerationSlot]:
    """Average PV power readings within each price slot to derive slot kWh.

    Args:
        samples: All PV power readings from the rolling history.
        price_slots: Price-grid slots defining the output resolution.
        now: Cycle timestamp used to clamp partial slots.
        local_tz: Timezone for day-boundary classification.
        yesterday_start: Earliest slot start to include.

    Returns:
        List of ObservedGenerationSlots with power-averaged kWh values.
    """
    slots: list[ObservedGenerationSlot] = []
    for ps in price_slots:
        if ps.start < yesterday_start or ps.start >= now:
            continue
        end_t = ps.end if ps.end < now else now
        if end_t <= ps.start:
            continue
        kwh = _average_power_to_kwh(samples, ps.start, end_t)
        slots.append(ObservedGenerationSlot(
            start=ps.start,
            end=ps.end,
            generated_kwh=kwh,
            source="inverter",
        ))
    return slots


def _build_slots_from_counter(
    samples: tuple[GenerationReading, ...],
    price_slots: tuple,
    now: datetime,
    local_tz: TzInfo,
    yesterday_start: datetime,
) -> list[ObservedGenerationSlot]:
    """Derive slot kWh by differencing the today-total counter at slot boundaries.

    Fallback path used when no PV power samples are available. Energy per slot
    is estimated by linear interpolation of the counter between adjacent
    samples, anchored at local midnight (counter reset point).

    Args:
        samples: Ordered today-total counter readings from the rolling history.
        price_slots: Price-grid slots defining the output resolution.
        now: Cycle timestamp used to clamp partial slots.
        local_tz: Timezone for day-boundary classification.
        yesterday_start: Earliest slot start to include.

    Returns:
        List of ObservedGenerationSlots with counter-differenced kWh values.
    """
    samples_by_day = _group_samples_by_day(samples, local_tz)
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
    return slots


def _average_power_to_kwh(
    samples: tuple[PvPowerReading, ...],
    slot_start: datetime,
    slot_end: datetime,
) -> float:
    """Convert power samples within [slot_start, slot_end) to kWh via averaging.

    Args:
        samples: All available PV power readings.
        slot_start: Inclusive slot boundary.
        slot_end: Exclusive slot boundary (already clamped to now).

    Returns:
        kWh for the slot; 0.0 when no samples fall within the window.
    """
    relevant = [s for s in samples if slot_start <= s.timestamp < slot_end]
    if not relevant:
        return 0.0
    avg_w = sum(s.power_w for s in relevant) / len(relevant)
    duration_h = (slot_end - slot_start).total_seconds() / 3600
    return max(0.0, round(avg_w * duration_h / 1000, 6))


def _latest_today_total(
    samples: tuple[GenerationReading, ...],
    now: datetime,
    local_tz: TzInfo,
) -> float | None:
    """Return the most recent today-total counter reading from today's samples.

    Args:
        samples: Rolling today-total counter readings.
        now: Reference time defining "today" in local timezone.
        local_tz: Timezone for date classification.

    Returns:
        Most recent today_total_kwh for today, or None if no today samples exist.
    """
    local_today = now.astimezone(local_tz).date()
    today_samples = [
        s for s in samples
        if s.timestamp.astimezone(local_tz).date() == local_today
    ]
    if not today_samples:
        return None
    return max(today_samples, key=lambda s: s.timestamp).today_total_kwh


def _apply_end_of_day_correction(
    slots: list[ObservedGenerationSlot],
    today_total_kwh: float | None,
    now: datetime,
    local_tz: TzInfo,
) -> list[ObservedGenerationSlot]:
    """Scale today's slots proportionally so they sum to the inverter's counter total.

    The cumulative counter updates every ~10 minutes and is authoritative over
    the full day. This correction anchors the power-averaged slot values to that
    total. Slots from yesterday are not modified (the counter has already reset).

    Skipped when:
    - today_total_kwh is None or ≤ 0 (no counter reading / no generation yet)
    - sum of today slots is ≤ 0 (no averaged values to scale)
    - correction factor outside [0.5, 2.0] (sensor misconfiguration guard)

    Args:
        slots: Power-averaged (or counter-differenced) slot list.
        today_total_kwh: Most recent today-total reading from the counter.
        now: Reference time defining "today" in local timezone.
        local_tz: Timezone for date classification.

    Returns:
        Slot list with today's entries scaled; yesterday's entries unchanged.
    """
    if today_total_kwh is None or today_total_kwh <= 0:
        return slots

    local_today = now.astimezone(local_tz).date()
    today_indices = [
        i for i, s in enumerate(slots)
        if s.start.astimezone(local_tz).date() == local_today
    ]
    if not today_indices:
        return slots

    today_sum = sum(slots[i].generated_kwh for i in today_indices)
    if today_sum <= 0:
        return slots

    factor = today_total_kwh / today_sum
    if not 0.5 <= factor <= 2.0:
        return slots

    result = list(slots)
    for i in today_indices:
        s = result[i]
        result[i] = ObservedGenerationSlot(
            start=s.start,
            end=s.end,
            generated_kwh=round(s.generated_kwh * factor, 6),
            source=s.source,
        )
    return result


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
# Translators (HA-edge readers)
# ---------------------------------------------------------------------------

class PvPowerTranslator:
    """Reads the inverter instantaneous PV power sensor; produces PvPowerReading.

    Accepts sensors in W or kW; reads unit_of_measurement from entity attributes
    and normalises to watts internally.
    """

    output_type = PvPowerReading

    def __init__(self, entity_id: str) -> None:
        """Initialise with the HA entity ID of the inverter PV power sensor.

        Args:
            entity_id: Entity ID of the instantaneous PV power sensor (W or kW).
        """
        self._entity_id = entity_id

    def parse(self, hass: Any, now: datetime | None = None) -> PvPowerReading | None:
        """Read the PV power sensor and return a timestamped snapshot in watts.

        Args:
            hass: Home Assistant instance.
            now: Snapshot timestamp; defaults to UTC now.

        Returns:
            PvPowerReading with power_w ≥ 0, or None when the entity is absent,
            unavailable, or not configured.
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
        unit = state.attributes.get("unit_of_measurement", "W")
        power_w = value if unit.upper() in ("W",) else value * 1000
        return PvPowerReading(power_w=max(0.0, power_w), timestamp=now)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> PvPowerReading | None:
        """DAG translator entry-point; delegates to parse().

        Args:
            hass: Home Assistant instance.
            config: Structured SunSale config (unused here).
            raw_config: Raw config-entry dict (unused here).
            now: Cycle timestamp.

        Returns:
            PvPowerReading or None when the sensor is unavailable.
        """
        return self.parse(hass, now)


class GenerationTranslator:
    """Reads the inverter today-total generation sensor; produces GenerationReading.

    The sensor is a cumulative kWh counter that resets at local midnight. Used
    as the authoritative daily total for end-of-day correction of power-averaged
    slots. Differencing across samples (fallback path) is done by
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
