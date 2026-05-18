"""Forecast stage: solar HA-state reader + GenerationSeries assembly.

The `SolarTranslator` reads forecast entities (Open Meteo watts, with a
Forecast.Solar / Solcast fallback) and produces SolarData. The
`build_generation_series` function then resamples it onto the price grid to
produce a continuous 72h GenerationSeries (yesterday 00:00 → tomorrow 23:59).
Every price slot gets exactly one generation slot, zero-filled where solar
coverage is absent. Yesterday is stitched in upstream from the persistent
store, invisible to consumers here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..contract.models import (
    GenerationSeries,
    GenerationSlot,
    SolarData,
    SolarEntry,
    SunSaleConfig,
)


def build_generation_series(
    solar: SolarData,
    price_slots: tuple,
    now: datetime | None = None,
) -> GenerationSeries:
    """Convert SolarData into a GenerationSeries aligned to the price grid."""
    if now is None:
        now = datetime.now(timezone.utc)

    ext = _compute_extended_day_totals(solar.entries, now)

    if not solar.entries or not price_slots:
        return GenerationSeries(
            slots=(),
            total_d2_kwh=ext[2],
            total_d3_kwh=ext[3],
            total_d4_kwh=ext[4],
            total_d5_kwh=ext[5],
            total_d6_kwh=ext[6],
        )

    resampled = _resample_to_grid(solar.entries, price_slots, solar.primary_source)
    totals = _compute_totals(resampled, now)

    return GenerationSeries(
        slots=resampled,
        total_yesterday_kwh=totals["yesterday"],
        total_today_kwh=totals["today"],
        total_tomorrow_kwh=totals["tomorrow"],
        today_remaining_kwh=totals["today_remaining"],
        total_d2_kwh=ext[2],
        total_d3_kwh=ext[3],
        total_d4_kwh=ext[4],
        total_d5_kwh=ext[5],
        total_d6_kwh=ext[6],
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


def _compute_extended_day_totals(entries: list[SolarEntry], now: datetime) -> dict[int, float]:
    """Sum solar entries into daily kWh totals for d2..d6."""
    today = now.date()
    return {
        n: round(sum(e.expected_kwh for e in entries if e.start.date() == today + timedelta(days=n)), 4)
        for n in range(2, 7)
    }


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


# ---------------------------------------------------------------------------
# Solar translator (HA-edge reader)
# ---------------------------------------------------------------------------

def _tomorrow_entity(entity_id: str) -> str:
    """Derive the tomorrow forecast entity from today's entity ID."""
    for pattern in ("_today_", "_today"):
        if pattern in entity_id:
            return entity_id.replace(pattern, pattern.replace("today", "tomorrow"), 1)
    return ""


def _day_entity(entity_id: str, n: int) -> str:
    """Derive the day-n (d2..d6) forecast entity from today's entity ID."""
    for pattern in ("_today_", "_today"):
        if pattern in entity_id:
            return entity_id.replace(pattern, pattern.replace("today", f"d{n}"), 1)
    return ""


def _watts_to_solar_entries(watts: dict[datetime, float]) -> list[SolarEntry]:
    """Convert {slot_utc: W} dict to SolarEntry list, detecting slot duration."""
    sorted_ts = sorted(watts.keys())
    if len(sorted_ts) >= 2:
        slot_dur = sorted_ts[1] - sorted_ts[0]
    else:
        slot_dur = timedelta(hours=1)
    slot_h = slot_dur.total_seconds() / 3600.0
    return [
        SolarEntry(
            start=ts,
            end=ts + slot_dur,
            expected_kwh=round(w * slot_h / 1000.0, 6),
            source="open_meteo",
        )
        for ts in sorted_ts
        for w in (watts[ts],)
    ]


def _make_solar_data(entries: list[SolarEntry], source: str, now: datetime) -> SolarData:
    today = now.date()
    total_today = sum(e.expected_kwh for e in entries if e.start.date() == today)
    remaining = sum(e.expected_kwh for e in entries if e.start.date() == today and e.start >= now)
    return SolarData(
        entries=entries,
        total_today_kwh=round(total_today, 4),
        today_remaining_kwh=round(remaining, 4),
        primary_source=source,
    )


class SolarTranslator:
    """Reads solar forecast from HA entities; produces SolarData.

    Tries Open Meteo (watts attribute) first across all entity IDs (today + tomorrow).
    Falls back to Forecast.Solar / Solcast (forecast attribute).
    Multiple panels (entity_1, entity_2) are combined by summing at each timestamp.
    Coordinator prepends yesterday entries from persistent store.
    """

    output_type = SolarData

    def __init__(self, entity_1: str, entity_2: str) -> None:
        self._entity_1 = entity_1
        self._entity_2 = entity_2

    def parse(self, hass: Any, now: datetime | None = None) -> SolarData:
        """Parse solar state into SolarData. Sync; callable from tests."""
        if now is None:
            now = datetime.now(timezone.utc)

        combined_watts: dict[datetime, float] = {}
        for base_eid in (self._entity_1, self._entity_2):
            if not base_eid:
                continue
            eids = [base_eid, _tomorrow_entity(base_eid)] + [_day_entity(base_eid, n) for n in range(2, 7)]
            for eid in eids:
                if not eid:
                    continue
                state = hass.states.get(eid)
                if state is None:
                    continue
                raw_watts = state.attributes.get("watts")
                if not isinstance(raw_watts, dict):
                    continue
                for ts_str, w in raw_watts.items():
                    try:
                        dt = datetime.fromisoformat(str(ts_str))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        slot_utc = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
                        combined_watts[slot_utc] = combined_watts.get(slot_utc, 0.0) + float(w)
                    except (ValueError, TypeError):
                        continue

        if combined_watts:
            entries = _watts_to_solar_entries(combined_watts)
            return _make_solar_data(entries, "open_meteo", now)

        # --- Forecast.Solar / Solcast fallback: collect from all entities ---
        combined_kwh: dict[datetime, float] = {}
        for base_eid in (self._entity_1, self._entity_2):
            if not base_eid:
                continue
            state = hass.states.get(base_eid)
            if state is None:
                continue
            for slot in state.attributes.get("forecast", []):
                try:
                    dt = datetime.fromisoformat(str(slot["time"]))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    slot_utc = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
                    kwh = float(slot.get("pv_estimate", slot.get("energy", 0.0)))
                    combined_kwh[slot_utc] = combined_kwh.get(slot_utc, 0.0) + kwh
                except (KeyError, ValueError, TypeError):
                    continue

        if combined_kwh:
            entries = [
                SolarEntry(start=s, end=s + timedelta(hours=1), expected_kwh=kwh, source="forecast_solar")
                for s, kwh in sorted(combined_kwh.items())
            ]
            return _make_solar_data(entries, "forecast_solar", now)

        return SolarData(entries=[], total_today_kwh=0.0, today_remaining_kwh=0.0, primary_source="none")

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> SolarData:
        return self.parse(hass, now)
