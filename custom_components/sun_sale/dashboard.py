"""Dashboard data builder for sunSale.

Assembles two outputs consumed by DashboardSensor:
  - future_slots: 15-min slots from now → end of tomorrow (prices, solar forecast, projected SoC, mode)
  - solar_frozen_forecast: today's frozen forecast series for the mismatch overlay

All heavy HA reads happen here so sensor.py stays trivial.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD,
    CONF_NORDPOOL_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY_2,
)
from .models import Action, BatteryConfig, BatteryState, Schedule, ScheduleSlot, TariffConfig
from . import tariff as tariff_module

_LOGGER = logging.getLogger(__name__)

_SLOT_MIN = 15
_SLOT_H = _SLOT_MIN / 60.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _floor_15min(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def _read_nordpool_15min(hass: HomeAssistant, entity_id: str) -> dict[datetime, float]:
    """Return {slot_start_utc: spot_price_eur_kwh} for all raw 15-min Nordpool slots."""
    if not entity_id:
        return {}
    state = hass.states.get(entity_id)
    if state is None:
        return {}

    result: dict[datetime, float] = {}
    for attr in ("raw_today", "raw_tomorrow"):
        raw = state.attributes.get(attr)
        if not isinstance(raw, list):
            continue
        for entry in raw:
            try:
                sv = entry["start"]
                dt = sv if isinstance(sv, datetime) else datetime.fromisoformat(str(sv))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt_utc = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
                result[dt_utc] = float(entry["value"])
            except (KeyError, ValueError, TypeError):
                continue
    return result


def _read_solar_watts_attr(hass: HomeAssistant, entity_id: str) -> dict[datetime, float]:
    """Read Open Meteo Solar Forecast 'watts' attribute → {slot_start_utc: W}."""
    if not entity_id:
        return {}
    state = hass.states.get(entity_id)
    if state is None:
        return {}
    watts = state.attributes.get("watts")
    if not isinstance(watts, dict):
        return {}

    result: dict[datetime, float] = {}
    for ts_str, w in watts.items():
        try:
            dt = datetime.fromisoformat(str(ts_str))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            result[dt.astimezone(timezone.utc).replace(second=0, microsecond=0)] = float(w)
        except (ValueError, TypeError):
            continue
    return result


def _tomorrow_entity(entity_id: str) -> str:
    """Derive tomorrow's forecast entity from today's by substituting 'today' → 'tomorrow'."""
    if not entity_id:
        return ""
    # handles both sensor.energy_production_today and sensor.energy_production_today_2
    for pattern in ("_today_", "_today"):
        if pattern in entity_id:
            return entity_id.replace(pattern, pattern.replace("today", "tomorrow"), 1)
    return ""


def _build_solar_15min(
    hass: HomeAssistant,
    entity_1: str,
    entity_2: str,
) -> dict[datetime, float]:
    """Combine up to two PV array forecasts (today + tomorrow) → {slot_utc: W}."""
    combined: dict[datetime, float] = {}

    for eid in (entity_1, entity_2, _tomorrow_entity(entity_1), _tomorrow_entity(entity_2)):
        for ts, w in _read_solar_watts_attr(hass, eid).items():
            combined[ts] = combined.get(ts, 0.0) + w

    return combined


def _estimate_load_w(hass: HomeAssistant, entity_id: str) -> float:
    """Return current household load in watts, or 200 W default."""
    if not entity_id:
        return 200.0
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown", ""):
        return 200.0
    try:
        return max(0.0, float(state.state))
    except ValueError:
        return 200.0


def _derive_mode(
    slot: ScheduleSlot | None,
    solar_w: float,
    load_w: float,
) -> tuple[str, str | None, float]:
    """Return (mode_label, grid_operation, net_battery_kw_per_15min).

    net_battery_kw > 0 = charging, < 0 = discharging.
    """
    if slot is None:
        if solar_w > load_w:
            return "self_use_sell", "sell", (solar_w - load_w) / 1000.0 * _SLOT_H
        return "self_use", None, 0.0

    # schedule is hourly; distribute across 4 × 15-min slots
    power_kw = slot.power_kw / 4.0

    if slot.action == Action.CHARGE_FROM_GRID:
        return "charge_from_grid", "buy", power_kw
    if slot.action == Action.DISCHARGE_TO_GRID:
        return "sell_discharge", "sell", -power_kw
    if slot.action == Action.CHARGE_FROM_SOLAR:
        net = max(0.0, (solar_w - load_w) / 1000.0) * _SLOT_H
        return "charge_solar", None, net
    # IDLE
    if solar_w > load_w:
        return "self_use_sell", "sell", 0.0
    return "self_use", None, 0.0


def _project_soc(
    soc_pct: float,
    net_batt_kwh: float,
    battery_config: BatteryConfig,
) -> float:
    """Advance SoC by net_batt_kwh (positive = charge), apply efficiency, clamp to limits."""
    eff = battery_config.round_trip_efficiency
    delta = net_batt_kwh * eff if net_batt_kwh >= 0 else net_batt_kwh / eff
    new_soc = soc_pct + (delta / battery_config.nominal_capacity_kwh) * 100.0
    return max(
        battery_config.min_soc * 100.0,
        min(battery_config.max_soc * 100.0, new_soc),
    )


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------

def build_future_slots(
    hass: HomeAssistant,
    config: dict,
    coordinator_data: dict,
    battery_config: BatteryConfig,
    tariff_config: TariffConfig,
) -> list[dict[str, Any]]:
    """Build 15-min future slots from now to end of tomorrow.

    Each slot:  {t, buy_price, sell_price, solar_forecast_w, battery_soc_pct,
                 inverter_mode, grid_operation}
    """
    now = datetime.now(timezone.utc)

    schedule: Schedule | None = coordinator_data.get("schedule")
    battery_state: BatteryState | None = coordinator_data.get("battery_state")

    # 15-min Nordpool spot prices
    prices = _read_nordpool_15min(hass, config.get(CONF_NORDPOOL_ENTITY, ""))

    # Solar forecast summed across both PV arrays at 15-min resolution
    solar = _build_solar_15min(
        hass,
        config.get(CONF_SOLAR_FORECAST_ENTITY, ""),
        config.get(CONF_SOLAR_FORECAST_ENTITY_2, ""),
    )

    # Fall back to coordinator's hourly solar forecast if no Open Meteo entities found
    if not solar:
        for sf in coordinator_data.get("solar_forecast", []):
            base = sf.start.replace(second=0, microsecond=0)
            w = sf.generation_kwh * 1000.0
            for i in range(4):
                solar.setdefault(base + timedelta(minutes=i * 15), w)

    load_w = _estimate_load_w(hass, config.get(CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD, ""))

    # Schedule lookup keyed by hour (UTC, minute=0)
    sched_by_hour: dict[datetime, ScheduleSlot] = {}
    if schedule:
        for s in schedule.slots:
            sched_by_hour[s.start.replace(minute=0, second=0, microsecond=0)] = s

    current_soc = (battery_state.soc * 100.0) if battery_state else 50.0
    soc = current_soc

    tomorrow = now.date() + timedelta(days=1)
    end_dt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 23, 45, tzinfo=timezone.utc)

    slots: list[dict[str, Any]] = []
    t = _floor_15min(now)

    while t <= end_dt:
        spot = prices.get(t)
        if spot is None:
            t += timedelta(minutes=15)
            continue

        buy_p = tariff_module.buy_price(spot, tariff_config)
        sell_p = tariff_module.sell_price(spot, tariff_config)

        solar_w = solar.get(t, 0.0)
        slot = sched_by_hour.get(t.replace(minute=0, second=0, microsecond=0))

        mode, grid_op, net_kwh = _derive_mode(slot, solar_w, load_w)
        soc = _project_soc(soc, net_kwh, battery_config)

        slots.append({
            "t": int(t.timestamp() * 1000),
            "buy_price": round(buy_p, 4),
            "sell_price": round(sell_p, 4),
            "solar_forecast_w": round(solar_w),
            "battery_soc_pct": round(soc, 1),
            "inverter_mode": mode,
            "grid_operation": grid_op,
        })
        t += timedelta(minutes=15)

    return slots


def build_solar_frozen_forecast(
    hass: HomeAssistant,
    config: dict,
) -> list[dict[str, Any]]:
    """Return today's frozen solar forecast as [{t_ms, forecast_w}] for the mismatch overlay.

    Reads from the configured Open Meteo entities' current 'watts' attributes.
    The panel uses this to colour actual-vs-forecast fills.
    """
    solar = _build_solar_15min(
        hass,
        config.get(CONF_SOLAR_FORECAST_ENTITY, ""),
        config.get(CONF_SOLAR_FORECAST_ENTITY_2, ""),
    )
    if not solar:
        return []

    today = datetime.now(timezone.utc).date()
    return [
        {"t": int(ts.timestamp() * 1000), "forecast_w": round(w)}
        for ts, w in sorted(solar.items())
        if ts.date() == today
    ]
