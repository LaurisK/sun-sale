"""Grid stage: per-cycle grid-power reader + per-slot ObservedGridSeries builder.

Three translators feed the pipeline:

* ``GridObserver`` snapshots the inverter's net AC grid power each cycle
  (positive = import, negative = export). Persisted into ``GridPowerHistory``.
* ``GridImportTotalTranslator`` and ``GridExportTotalTranslator`` snapshot the
  daily-resetting cumulative import/export kWh counters. These update every
  ~10 min and reset at local midnight, mirroring the today-total generation
  counter; they anchor the end-of-day correction in ``ObservedGridSeries``.

``build_observed_grid_series`` averages the signed grid-power samples within
each price-grid slot, but does the **gross flow split per sample**:

    import_slot_kwh = (sum(max(0, kw)  for s in slot_samples) / n) * duration_h
    export_slot_kwh = (sum(max(0,-kw)) for s in slot_samples) / n) * duration_h

This preserves gross import + export even within a slot whose net averages to
zero (a slot with 50% import and 50% export was previously billed as
zero on both sides — that bug is fixed by splitting per sample first).

Window: the series spans **two days back through now** in local time (day
before yesterday 00:00 LOCAL → now). That extra day matters for the monthly
bill module's day-rollover bake-in, which always reaches one local day
further back than "yesterday" relative to the current cycle's `now`.

End-of-day correction: when the today-total import/export counters are present,
today's slot import/export are scaled independently so each side's sum matches
the authoritative counter. The factor is clamped to ``[0.5, 2.0]``; outside
that window the correction is skipped as a sensor-fault guard.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from datetime import tzinfo as TzInfo
from typing import Any

from ..contract.models import (
    GridExportTodayHistory,
    GridExportTodayReading,
    GridImportTodayHistory,
    GridImportTodayReading,
    GridPowerHistory,
    GridPowerReading,
    ObservedGridSeries,
    ObservedGridSlot,
    SunSaleConfig,
)
from ..outbound.inverter import normalize_power_to_kw


class GridObserver:
    """Reads the grid-power HA entity; produces GridPowerReading."""

    output_type = GridPowerReading

    def __init__(
        self,
        entity_id: str,
        invert_sign: bool = False,
        fallback_entity_id: str = "",
    ) -> None:
        """Initialise with the HA entity ID of the grid power sensor.

        Args:
            entity_id: Entity ID of the primary grid power sensor (W or kW).
                       The sunSale-internal contract is positive = import
                       from grid; raw values are negated when ``invert_sign``
                       is set so the rest of the pipeline can treat them as
                       already in sunSale convention.
            invert_sign: When True, the value read from the entity is
                       multiplied by -1 before being returned. Used to
                       adapt the solis_modbus convention (positive =
                       inverter→grid) to sunSale's positive=import contract.
            fallback_entity_id: Optional secondary entity consulted when the
                       primary is missing or its state is unavailable (Solis
                       CT-only installs where the Modbus-meter register is
                       empty fall back to the AC grid port power, which
                       shares the same sign convention as the meter).
        """
        self._entity_id = entity_id
        self._invert_sign = invert_sign
        self._fallback_entity_id = fallback_entity_id

    def parse(self, hass: Any, now: datetime) -> GridPowerReading | None:
        """Read the grid power entity and return a timestamped reading in kW.

        Returns None (not a zero stub) when neither the primary nor fallback
        entity yields a numeric state, so the persisted history is not
        polluted with false readings.

        Args:
            hass: Home Assistant instance.
            now: Snapshot timestamp.

        Returns:
            GridPowerReading in kW, or None when unavailable.
        """
        power_kw = self._read_kw(hass, self._entity_id)
        if power_kw is None and self._fallback_entity_id:
            power_kw = self._read_kw(hass, self._fallback_entity_id)
        if power_kw is None:
            return None
        if self._invert_sign:
            power_kw = -power_kw
        return GridPowerReading(
            power_kw=power_kw,
            timestamp=now,
        )

    @staticmethod
    def _read_kw(hass: Any, entity_id: str) -> float | None:
        """Read ``entity_id`` and normalise to kW; return None on missing state."""
        if not entity_id:
            return None
        state = hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        unit = str((state.attributes or {}).get("unit_of_measurement") or "").strip()
        return normalize_power_to_kw(value, unit)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> GridPowerReading | None:
        """DAG translator entry-point; delegates to parse().

        Args:
            hass: Home Assistant instance.
            config: Structured SunSale config (unused here).
            raw_config: Raw config-entry dict (unused here).
            now: Cycle timestamp.

        Returns:
            GridPowerReading or None when unavailable.
        """
        return self.parse(hass, now)


class _DailyTotalKwhTranslator:
    """Shared parsing for daily-resetting cumulative kWh sensors.

    Reads a single HA sensor whose value is "kWh since local midnight" and
    returns the latest snapshot. Subclasses bind it to a concrete reading type.
    """

    reading_cls: type
    output_type: type

    def __init__(self, entity_id: str) -> None:
        """Initialise with the HA entity ID of the counter sensor.

        Args:
            entity_id: Entity ID of the cumulative today-total kWh sensor.
        """
        self._entity_id = entity_id

    def _parse(self, hass: Any, now: datetime | None):
        """Read the sensor and return a typed snapshot, or None when unavailable.

        Args:
            hass: Home Assistant instance.
            now: Snapshot timestamp; defaults to UTC now.

        Returns:
            ``reading_cls`` instance, or None on missing / non-numeric state.
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
        return self.reading_cls(today_total_kwh=value, timestamp=now)


class GridImportTotalTranslator(_DailyTotalKwhTranslator):
    """Reads the today-total imported-kWh counter; produces GridImportTodayReading."""

    reading_cls = GridImportTodayReading
    output_type = GridImportTodayReading

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> GridImportTodayReading | None:
        """DAG translator entry-point; delegates to _parse()."""
        return self._parse(hass, now)


class GridExportTotalTranslator(_DailyTotalKwhTranslator):
    """Reads the today-total exported-kWh counter; produces GridExportTodayReading."""

    reading_cls = GridExportTodayReading
    output_type = GridExportTodayReading

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> GridExportTodayReading | None:
        """DAG translator entry-point; delegates to _parse()."""
        return self._parse(hass, now)


# ---------------------------------------------------------------------------
# ObservedGridSeries assembly
# ---------------------------------------------------------------------------

def build_observed_grid_series(
    grid_power_history: GridPowerHistory,
    import_total_history: GridImportTodayHistory,
    export_total_history: GridExportTodayHistory,
    price_slots: tuple,
    now: datetime | None = None,
    local_tz: TzInfo = timezone.utc,
) -> ObservedGridSeries:
    """Derive per-slot observed grid import/export from power samples + counters.

    For each price slot in the [yesterday 00:00, now) window, splits the
    signed grid-power samples into import and export buckets, averages each
    bucket separately, and multiplies by the slot duration. End-of-day
    correction then scales today's slots so each side's sum matches the
    corresponding today-total counter (when present and within the
    factor-bound guard).

    Args:
        grid_power_history: Rolling samples of net grid power in kW.
        import_total_history: Rolling samples of the today-total imported-kWh counter.
        export_total_history: Rolling samples of the today-total exported-kWh counter.
        price_slots: Price-grid slots defining the output resolution.
        now: Cycle timestamp; defaults to UTC now.
        local_tz: Local timezone for day-boundary calculations.

    Returns:
        ObservedGridSeries covering yesterday 00:00 local → now, grid-aligned.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if not price_slots or not grid_power_history.samples:
        return ObservedGridSeries(slots=(), computed_at=now)

    # Extend two local days back so the monthly_bill day-rollover bake-in
    # window (always the LOCAL day before the current cycle's "yesterday")
    # is still inside the series. Same data is already retained on the
    # GridPowerHistory side (GRID_POWER_HISTORY_RETENTION_DAYS=2).
    window_start = _day_start(now, local_tz) - timedelta(days=2)
    slots = _build_slots_from_power(
        grid_power_history.samples, price_slots, now, window_start
    )

    today_import_total = _latest_today_total(import_total_history.samples, now, local_tz)
    today_export_total = _latest_today_total(export_total_history.samples, now, local_tz)
    slots = _apply_end_of_day_correction(
        slots, today_import_total, today_export_total, now, local_tz,
    )

    totals = _compute_totals(slots, now, local_tz)
    return ObservedGridSeries(
        slots=tuple(slots),
        computed_at=now,
        total_yesterday_imported_kwh=totals["yesterday_imported"],
        total_yesterday_exported_kwh=totals["yesterday_exported"],
        total_today_imported_kwh=totals["today_imported"],
        total_today_exported_kwh=totals["today_exported"],
    )


def _build_slots_from_power(
    samples: tuple[GridPowerReading, ...],
    price_slots: tuple,
    now: datetime,
    window_start: datetime,
) -> list[ObservedGridSlot]:
    """Average signed grid-power samples per slot into gross import + export kWh.

    Args:
        samples: All grid-power readings from the rolling history.
        price_slots: Price-grid slots defining the output resolution.
        now: Cycle timestamp used to clamp partial slots.
        window_start: Earliest slot start to include.

    Returns:
        List of ObservedGridSlots with averaged imported / exported kWh values.
    """
    slots: list[ObservedGridSlot] = []
    for ps in price_slots:
        if ps.start < window_start or ps.start >= now:
            continue
        end_t = ps.end if ps.end < now else now
        if end_t <= ps.start:
            continue
        imported_kwh, exported_kwh = _split_average_power_to_kwh(
            samples, ps.start, end_t,
        )
        slots.append(ObservedGridSlot(
            start=ps.start,
            end=ps.end,
            imported_kwh=imported_kwh,
            exported_kwh=exported_kwh,
            source="inverter",
        ))
    return slots


def _split_average_power_to_kwh(
    samples: tuple[GridPowerReading, ...],
    slot_start: datetime,
    slot_end: datetime,
) -> tuple[float, float]:
    """Average positive / negative samples in [slot_start, slot_end) into kWh.

    A sample's positive part contributes to import; its negative part (as a
    positive magnitude) contributes to export. Averaging the parts separately
    over **all** samples in the window preserves gross flows even when the
    signed mean is near zero — a slot equally split between 2 kW import and
    2 kW export reports non-zero values on both sides rather than collapsing
    to zero (which is what averaging signed values then splitting by sign of
    the mean would have produced).

    Args:
        samples: All grid-power readings.
        slot_start: Inclusive slot boundary.
        slot_end: Exclusive slot boundary (already clamped to now).

    Returns:
        ``(imported_kwh, exported_kwh)`` — both non-negative, rounded to 6 dp.
        Returns ``(0.0, 0.0)`` when no samples fall within the window.
    """
    relevant = [s for s in samples if slot_start <= s.timestamp < slot_end]
    if not relevant:
        return 0.0, 0.0
    n = len(relevant)
    avg_import_kw = sum(max(0.0, s.power_kw) for s in relevant) / n
    avg_export_kw = sum(max(0.0, -s.power_kw) for s in relevant) / n
    duration_h = (slot_end - slot_start).total_seconds() / 3600
    return (
        round(avg_import_kw * duration_h, 6),
        round(avg_export_kw * duration_h, 6),
    )


def _latest_today_total(
    samples: tuple,
    now: datetime,
    local_tz: TzInfo,
) -> float | None:
    """Return the most recent today-total counter reading from today's samples.

    Args:
        samples: Rolling counter readings (either Import or Export variant).
        now: Reference time defining "today" in local timezone.
        local_tz: Timezone for date classification.

    Returns:
        Most recent today_total_kwh for today, or None if no today samples exist.
    """
    if not samples:
        return None
    local_today = now.astimezone(local_tz).date()
    today_samples = [
        s for s in samples
        if s.timestamp.astimezone(local_tz).date() == local_today
    ]
    if not today_samples:
        return None
    return max(today_samples, key=lambda s: s.timestamp).today_total_kwh


def _apply_end_of_day_correction(
    slots: list[ObservedGridSlot],
    today_import_total_kwh: float | None,
    today_export_total_kwh: float | None,
    now: datetime,
    local_tz: TzInfo,
) -> list[ObservedGridSlot]:
    """Scale today's import + export sums independently to match counter totals.

    The counters update every ~10 min and are authoritative over the full day.
    Import and export are scaled with independent factors so a deployment with
    only one of the two counters (or with one of them stuck at zero before
    sunrise) still benefits from the correction on the other side.

    A side is skipped when:
      - its counter is None or ≤ 0 (no reading / no flow yet)
      - its slot sum is ≤ 0 (no averaged values to scale)
      - its correction factor falls outside [0.5, 2.0] (sensor-fault guard)

    Args:
        slots: Per-slot import/export list (today's entries are modified in place).
        today_import_total_kwh: Most recent today-total imported reading.
        today_export_total_kwh: Most recent today-total exported reading.
        now: Reference time defining "today" in local timezone.
        local_tz: Timezone for date classification.

    Returns:
        New slot list with today's entries scaled per side; yesterday unchanged.
    """
    local_today = now.astimezone(local_tz).date()
    today_indices = [
        i for i, s in enumerate(slots)
        if s.start.astimezone(local_tz).date() == local_today
    ]
    if not today_indices:
        return slots

    import_factor = _scale_factor(
        today_import_total_kwh,
        sum(slots[i].imported_kwh for i in today_indices),
    )
    export_factor = _scale_factor(
        today_export_total_kwh,
        sum(slots[i].exported_kwh for i in today_indices),
    )
    if import_factor is None and export_factor is None:
        return slots

    result = list(slots)
    for i in today_indices:
        s = result[i]
        new_imp = (
            round(s.imported_kwh * import_factor, 6)
            if import_factor is not None else s.imported_kwh
        )
        new_exp = (
            round(s.exported_kwh * export_factor, 6)
            if export_factor is not None else s.exported_kwh
        )
        result[i] = ObservedGridSlot(
            start=s.start,
            end=s.end,
            imported_kwh=new_imp,
            exported_kwh=new_exp,
            source=s.source,
        )
    return result


def _scale_factor(total_kwh: float | None, slot_sum_kwh: float) -> float | None:
    """Return the correction multiplier, or None when the side must be skipped.

    Args:
        total_kwh: Counter's today-total reading (None when no counter).
        slot_sum_kwh: Sum of today's slot values for this side.

    Returns:
        Multiplier in [0.5, 2.0], or None when the scaling should be skipped.
    """
    if total_kwh is None or total_kwh <= 0:
        return None
    if slot_sum_kwh <= 0:
        return None
    factor = total_kwh / slot_sum_kwh
    if not 0.5 <= factor <= 2.0:
        return None
    return factor


def _compute_totals(
    slots: list[ObservedGridSlot], now: datetime, local_tz: TzInfo,
) -> dict[str, float]:
    """Sum observed grid slots into yesterday/today import + export totals.

    Args:
        slots: ObservedGridSlots in any order.
        now: Reference time used to determine today's and yesterday's local dates.
        local_tz: Timezone for local-date classification.

    Returns:
        Dict with keys yesterday_imported / yesterday_exported /
        today_imported / today_exported in kWh.
    """
    local_now = now.astimezone(local_tz)
    today = local_now.date()
    yesterday = today - timedelta(days=1)
    yest_imp = yest_exp = today_imp = today_exp = 0.0
    for s in slots:
        d = s.start.astimezone(local_tz).date()
        if d == yesterday:
            yest_imp += s.imported_kwh
            yest_exp += s.exported_kwh
        elif d == today:
            today_imp += s.imported_kwh
            today_exp += s.exported_kwh
    return {
        "yesterday_imported": round(yest_imp, 4),
        "yesterday_exported": round(yest_exp, 4),
        "today_imported":     round(today_imp, 4),
        "today_exported":     round(today_exp, 4),
    }


def _day_start(t: datetime, local_tz: TzInfo) -> datetime:
    """Return local midnight for t's local day, expressed as a UTC-aware datetime.

    Args:
        t: Any tz-aware datetime.
        local_tz: Timezone defining "local midnight".

    Returns:
        UTC-aware datetime of local midnight on t's local date.
    """
    local_t = t.astimezone(local_tz)
    local_midnight = local_t.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)
