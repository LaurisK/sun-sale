"""Translation layer (Layer 3a) — reads HA state and produces typed domain values.

Each translator is independent; they run in parallel before the DAG starts.
All HA reads are isolated here; no other module reads hass.states directly
(except output adapters in inverter.py / ev_charger.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..contract.const import (
    CONF_EV_ENTITY_DEPARTURE_TIME,
    CONF_EV_ENTITY_TARGET_SOC,
    CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD,
    CONF_NORDPOOL_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY_2,
    DEFAULT_EV_TARGET_SOC,
)
from ..outbound.ev_charger import EVChargerController
from ..outbound.inverter import InverterController
from ..contract.models import (
    BatteryReading,
    EVChargerState,
    NordpoolData,
    PriceEntry,
    SolarData,
    SolarEntry,
    SunSaleConfig,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_HOUSEHOLD_LOAD_KW = 0.2


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _zero_fill_tomorrow(entries: list[PriceEntry], resolution: timedelta, now: datetime) -> list[PriceEntry]:
    """Append zero-price entries for tomorrow if tomorrow has no entries yet."""
    tomorrow_date = (now + timedelta(days=1)).date()
    has_tomorrow = any(e.start.date() == tomorrow_date for e in entries)
    if has_tomorrow:
        return entries

    tomorrow_start = datetime(
        tomorrow_date.year, tomorrow_date.month, tomorrow_date.day,
        0, 0, 0, tzinfo=timezone.utc,
    )
    slots_per_day = 96 if resolution <= timedelta(minutes=15) else 24
    zero_entries = [
        PriceEntry(
            start=tomorrow_start + i * resolution,
            end=tomorrow_start + (i + 1) * resolution,
            price_eur_kwh=0.0,
        )
        for i in range(slots_per_day)
    ]
    return entries + zero_entries


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


# ---------------------------------------------------------------------------
# Nordpool translator
# ---------------------------------------------------------------------------

class NordpoolTranslator:
    """Reads Nordpool sensor; produces NordpoolData for today + tomorrow.

    Resolution is auto-detected from the sensor data (15min or 1h).
    Tomorrow entries are zero-filled when not yet published.
    Coordinator prepends yesterday entries from persistent store.
    """

    output_type = NordpoolData

    def __init__(self, entity_id: str) -> None:
        self._entity_id = entity_id

    def parse(self, hass: Any, now: datetime | None = None) -> NordpoolData:
        """Parse Nordpool state into NordpoolData (today + tomorrow). Sync; callable from tests."""
        if now is None:
            now = datetime.now(timezone.utc)

        state = hass.states.get(self._entity_id)
        if state is None:
            _LOGGER.warning("Nordpool entity '%s' not found", self._entity_id)
            return NordpoolData(entries=[], resolution=timedelta(hours=1))

        raw_entries: list[dict] = []
        for attr_key in ("raw_today", "raw_tomorrow"):
            raw = state.attributes.get(attr_key)
            if isinstance(raw, list):
                raw_entries.extend(raw)

        if raw_entries:
            return self._parse_raw_entries(raw_entries, now)

        return self._parse_legacy(state, now)

    def _parse_raw_entries(self, raw_entries: list[dict], now: datetime) -> NordpoolData:
        parsed: list[tuple[datetime, float]] = []
        for entry in raw_entries:
            try:
                sv = entry["start"]
                dt = sv if isinstance(sv, datetime) else datetime.fromisoformat(str(sv))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                start_utc = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
                parsed.append((start_utc, float(entry["value"])))
            except (KeyError, ValueError, TypeError):
                continue

        seen: set[datetime] = set()
        unique: list[tuple[datetime, float]] = []
        for item in sorted(parsed):
            if item[0] not in seen:
                seen.add(item[0])
                unique.append(item)

        if not unique:
            return NordpoolData(entries=[], resolution=timedelta(hours=1))

        resolution = (unique[1][0] - unique[0][0]) if len(unique) >= 2 else timedelta(hours=1)
        entries = [PriceEntry(start=s, end=s + resolution, price_eur_kwh=p) for s, p in unique]
        entries = _zero_fill_tomorrow(entries, resolution, now)
        return NordpoolData(entries=entries, resolution=resolution)

    def _parse_legacy(self, state: Any, now: datetime) -> NordpoolData:
        """Legacy format: flat list of up to 24 hourly prices per day."""
        resolution = timedelta(hours=1)
        entries: list[PriceEntry] = []
        for offset, attr_key in enumerate(("today", "tomorrow")):
            raw = state.attributes.get(attr_key)
            if not isinstance(raw, list):
                continue
            base_date = (now + timedelta(days=offset)).date()
            for hour_idx, price in enumerate(raw):
                if price is None or hour_idx >= 24:
                    continue
                start = datetime(
                    base_date.year, base_date.month, base_date.day,
                    hour_idx, 0, 0, tzinfo=timezone.utc,
                )
                entries.append(PriceEntry(start=start, end=start + resolution, price_eur_kwh=float(price)))

        if not entries:
            return NordpoolData(entries=[], resolution=resolution)
        entries = _zero_fill_tomorrow(entries, resolution, now)
        return NordpoolData(entries=entries, resolution=resolution)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> NordpoolData:
        return self.parse(hass, now)


# ---------------------------------------------------------------------------
# Solar translator
# ---------------------------------------------------------------------------

def _tomorrow_entity(entity_id: str) -> str:
    """Derive the tomorrow forecast entity from today's entity ID."""
    for pattern in ("_today_", "_today"):
        if pattern in entity_id:
            return entity_id.replace(pattern, pattern.replace("today", "tomorrow"), 1)
    return ""


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

        # --- Open Meteo: collect watts from all entities and their tomorrow counterparts ---
        combined_watts: dict[datetime, float] = {}
        for base_eid in (self._entity_1, self._entity_2):
            if not base_eid:
                continue
            for eid in (base_eid, _tomorrow_entity(base_eid)):
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


# ---------------------------------------------------------------------------
# Battery translator
# ---------------------------------------------------------------------------

class BatteryTranslator:
    """Reads inverter telemetry and household load; produces BatteryReading."""

    output_type = BatteryReading

    def __init__(
        self,
        inverter: InverterController,
        household_load_entity: str,
    ) -> None:
        self._inverter = inverter
        self._load_entity = household_load_entity

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> BatteryReading:
        soc = self._inverter.get_battery_soc()
        power_kw = self._inverter.get_battery_power()
        grid_kw = self._inverter.get_grid_power()
        load_kw = _read_household_load(hass, self._load_entity)
        return BatteryReading(
            soc=soc,
            power_kw=power_kw,
            grid_power_kw=grid_kw,
            household_load_kw=load_kw,
        )


def _read_household_load(hass: Any, entity_id: str) -> float:
    if not entity_id:
        return _DEFAULT_HOUSEHOLD_LOAD_KW
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown", ""):
        return _DEFAULT_HOUSEHOLD_LOAD_KW
    try:
        return max(0.0, float(state.state)) / 1000.0  # W → kW
    except ValueError:
        return _DEFAULT_HOUSEHOLD_LOAD_KW


# ---------------------------------------------------------------------------
# EV translator
# ---------------------------------------------------------------------------

class EVTranslator:
    """Reads EV charger state from HA; produces EVChargerState.

    Only registered when EV is enabled. Returns None (skipped) when EV unavailable.
    """

    output_type = EVChargerState

    def __init__(
        self,
        ev_charger: EVChargerController,
        target_soc_entity: str,
        departure_entity: str,
    ) -> None:
        self._ev_charger = ev_charger
        self._target_soc_entity = target_soc_entity
        self._departure_entity = departure_entity

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> EVChargerState:
        is_plugged = self._ev_charger.is_plugged_in()
        soc = self._ev_charger.get_ev_soc()

        target_soc = DEFAULT_EV_TARGET_SOC
        if self._target_soc_entity:
            ts = hass.states.get(self._target_soc_entity)
            if ts and ts.state not in ("unavailable", "unknown", ""):
                try:
                    val = float(ts.state)
                    target_soc = val / 100.0 if val > 1.0 else val
                except ValueError:
                    pass

        departure_time: datetime | None = None
        if self._departure_entity:
            ds = hass.states.get(self._departure_entity)
            if ds and ds.state not in ("unavailable", "unknown", ""):
                try:
                    departure_time = datetime.fromisoformat(ds.state).replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    pass

        return EVChargerState(
            is_plugged_in=is_plugged,
            soc=soc if soc is not None else 0.5,
            target_soc=target_soc,
            departure_time=departure_time,
        )
