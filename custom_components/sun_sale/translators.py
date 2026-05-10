"""Translation layer (Layer 3a) — reads HA state and produces typed domain values.

Each translator is independent; they run in parallel before the DAG starts.
All HA reads are isolated here; no other module reads hass.states directly
(except output adapters in inverter.py / ev_charger.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .const import (
    CONF_EV_ENTITY_DEPARTURE_TIME,
    CONF_EV_ENTITY_TARGET_SOC,
    CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD,
    CONF_NORDPOOL_ENTITY,
    CONF_NORDPOOL_RESOLUTION,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY_2,
    DEFAULT_EV_TARGET_SOC,
    DEFAULT_NORDPOOL_RESOLUTION,
)
from .ev_charger import EVChargerController
from .inverter import InverterController
from .models import (
    BatteryReading,
    EVChargerState,
    HourlyPrice,
    NordpoolPrices,
    RawSolarData,
    SunSaleConfig,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_HOUSEHOLD_LOAD_KW = 0.2


# ---------------------------------------------------------------------------
# Nordpool translator
# ---------------------------------------------------------------------------

class NordpoolTranslator:
    """Reads Nordpool sensor and produces NordpoolPrices.

    Produces:
      - slots: list[HourlyPrice] at configured resolution (15min or hourly)
      - raw_15min: dict[datetime, float] always at 15-min granularity for dashboard use
    """

    output_type = NordpoolPrices

    def __init__(self, entity_id: str, resolution: str) -> None:
        self._entity_id = entity_id
        self._resolution = resolution

    def parse(self, hass: Any) -> NordpoolPrices:
        """Parse Nordpool state into NordpoolPrices. Sync; callable from tests."""
        state = hass.states.get(self._entity_id)
        if state is None:
            _LOGGER.warning("Nordpool entity '%s' not found", self._entity_id)
            return NordpoolPrices(slots=[], raw_15min={})

        raw_entries: list[dict] = []
        for attr_key in ("raw_today", "raw_tomorrow"):
            raw = state.attributes.get(attr_key)
            if isinstance(raw, list):
                raw_entries.extend(raw)

        if raw_entries:
            return self._parse_raw_entries(raw_entries)

        # Legacy: flat list of hourly prices
        return self._parse_legacy(state)

    def _parse_raw_entries(self, raw_entries: list[dict]) -> NordpoolPrices:
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
            return NordpoolPrices(slots=[], raw_15min={})

        slot_dur = (unique[1][0] - unique[0][0]) if len(unique) >= 2 else timedelta(hours=1)
        raw_15min = {s: p for s, p in unique}

        if self._resolution == "hourly" and slot_dur < timedelta(hours=1):
            hourly: dict[datetime, list[float]] = {}
            for start_utc, price in unique:
                hour = start_utc.replace(minute=0)
                hourly.setdefault(hour, []).append(price)
            slots = [
                HourlyPrice(start=h, end=h + timedelta(hours=1),
                            price_eur_kwh=sum(vals) / len(vals))
                for h, vals in sorted(hourly.items())
            ]
        else:
            slots = [
                HourlyPrice(start=s, end=s + slot_dur, price_eur_kwh=p)
                for s, p in unique
            ]

        return NordpoolPrices(slots=slots, raw_15min=raw_15min)

    def _parse_legacy(self, state: Any) -> NordpoolPrices:
        """Legacy format: flat list of up to 24 hourly prices per day."""
        now = datetime.now(timezone.utc)
        slots: list[HourlyPrice] = []
        raw_15min: dict[datetime, float] = {}
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
                hp = HourlyPrice(start=start, end=start + timedelta(hours=1),
                                 price_eur_kwh=float(price))
                slots.append(hp)
                raw_15min[start] = float(price)
        return NordpoolPrices(slots=slots, raw_15min=raw_15min)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> NordpoolPrices:
        return self.parse(hass)


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
    """Reads solar forecast from HA entities and produces RawSolarData.

    Tries Open Meteo (watts attribute) first across both entity IDs (today + tomorrow).
    Falls back to Forecast.Solar / Solcast (forecast attribute) for entity_1 only.
    """

    output_type = RawSolarData

    def __init__(self, entity_1: str, entity_2: str) -> None:
        self._entity_1 = entity_1
        self._entity_2 = entity_2

    def parse(self, hass: Any) -> RawSolarData:
        """Parse solar state into RawSolarData. Sync; callable from tests."""
        watts: dict[datetime, float] = {}
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
                        watts[slot_utc] = watts.get(slot_utc, 0.0) + float(w)
                    except (ValueError, TypeError):
                        continue

        if watts:
            return RawSolarData(watts=watts, forecast_slots=[])

        # Fallback: Forecast.Solar / Solcast
        forecast_slots: list[dict] = []
        if self._entity_1:
            state = hass.states.get(self._entity_1)
            if state is not None:
                forecast_slots = state.attributes.get("forecast", [])

        return RawSolarData(watts={}, forecast_slots=list(forecast_slots))

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> RawSolarData:
        return self.parse(hass)


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
