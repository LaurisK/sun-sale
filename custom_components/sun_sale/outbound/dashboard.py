"""Dashboard data builder for sunSale.

Pure Python — no Home Assistant imports.
Called by DashboardNode (Tier 5 DAG sink node).

Builds two outputs consumed by DashboardSensor:
  - future_slots: 15-min slots from now → end of tomorrow
  - solar_frozen_forecast: today's frozen forecast for the mismatch overlay
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..contract.models import (
    Action,
    BatteryConfig,
    BatteryReading,
    NordpoolData,
    SolarData,
    Schedule,
    ScheduleSlot,
    TariffConfig,
)
from ..pipeline import tariff as tariff_module

_SLOT_MIN = 15
_SLOT_H = _SLOT_MIN / 60.0


def _floor_15min(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def _spot_at(entries, t: datetime) -> float | None:
    """Find the spot price covering time t in NordpoolData.entries."""
    for e in entries:
        if e.start <= t < e.end:
            return e.price_eur_kwh
    return None


def _solar_watts_at(entries, t: datetime) -> float:
    """Return solar generation at time t as approximate watts (from SolarData.entries)."""
    for e in entries:
        if e.start <= t < e.end:
            slot_h = (e.end - e.start).total_seconds() / 3600.0
            return e.expected_kwh / slot_h * 1000.0 if slot_h > 0 else 0.0
    return 0.0


def _derive_mode(
    slot: ScheduleSlot | None,
    solar_w: float,
    load_w: float,
) -> tuple[str, str | None, float]:
    """Return (mode_label, grid_operation, net_battery_kwh_per_15min)."""
    if slot is None:
        if solar_w > load_w:
            return "self_use_sell", "sell", (solar_w - load_w) / 1000.0 * _SLOT_H
        return "self_use", None, 0.0

    slot_h = (slot.end - slot.start).total_seconds() / 3600
    power_kwh = slot.power_kw * _SLOT_H / slot_h if slot_h > 0 else 0.0

    if slot.action == Action.CHARGE_FROM_GRID:
        return "charge_from_grid", "buy", power_kwh
    if slot.action == Action.DISCHARGE_TO_GRID:
        return "sell_discharge", "sell", -power_kwh
    if slot.action == Action.CHARGE_FROM_SOLAR:
        net = max(0.0, (solar_w - load_w) / 1000.0) * _SLOT_H
        return "charge_solar", None, net
    if solar_w > load_w:
        return "self_use_sell", "sell", 0.0
    return "self_use", None, 0.0


def _project_soc(
    soc_pct: float,
    net_batt_kwh: float,
    battery_config: BatteryConfig,
) -> float:
    eff = battery_config.round_trip_efficiency
    delta = net_batt_kwh * eff if net_batt_kwh >= 0 else net_batt_kwh / eff
    new_soc = soc_pct + (delta / battery_config.nominal_capacity_kwh) * 100.0
    return max(
        battery_config.min_soc * 100.0,
        min(battery_config.max_soc * 100.0, new_soc),
    )


def build_future_slots(
    nordpool: NordpoolData,
    solar: SolarData,
    reading: BatteryReading,
    schedule: Schedule | None,
    battery_config: BatteryConfig,
    tariff_config: TariffConfig,
    now: datetime,
) -> list[dict[str, Any]]:
    """Build 15-min future slots from now to end of local-tomorrow.

    Each slot: {t, buy_price, sell_price, solar_forecast_w, battery_soc_pct,
                inverter_mode, grid_operation}
    """
    sched_by_hour: dict[datetime, ScheduleSlot] = {}
    if schedule:
        for s in schedule.slots:
            sched_by_hour[s.start.replace(minute=0, second=0, microsecond=0)] = s

    current_soc = reading.soc * 100.0
    load_w = reading.household_load_kw * 1000.0
    soc = current_soc

    # End at "day-after-tomorrow UTC 23:45". The +2-day reach absorbs the
    # UTC↔local boundary skew: when the coordinator runs in the early-UTC-
    # morning hours of local-yesterday, `now.date() + 1 day` is only a few
    # hours into local tomorrow, leaving the chart's tomorrow column empty.
    # The chart filters its own 72 h window, so the extra slots are harmless.
    end_date = now.date() + timedelta(days=2)
    end_dt = datetime(end_date.year, end_date.month, end_date.day, 23, 45, tzinfo=timezone.utc)

    slots: list[dict[str, Any]] = []
    t = _floor_15min(now)

    while t <= end_dt:
        spot = _spot_at(nordpool.entries, t)
        if spot is None:
            t += timedelta(minutes=15)
            continue

        buy_p = tariff_module.buy_price(spot, tariff_config)
        sell_p = tariff_module.sell_price(spot, tariff_config)
        solar_w = _solar_watts_at(solar.entries, t)
        slot = sched_by_hour.get(t.replace(minute=0, second=0, microsecond=0))

        mode, grid_op, net_kwh = _derive_mode(slot, solar_w, load_w)
        soc = _project_soc(soc, net_kwh, battery_config)

        slots.append({
            "t": int(t.timestamp() * 1000),
            "buy_price": round(buy_p, 4),
            "sell_price": round(sell_p, 4),
            "solar_forecast_w": round(solar_w),
            "solar_forecast_kwh": round(solar_w * _SLOT_H / 1000, 4),
            "battery_soc_pct": round(soc, 1),
            "inverter_mode": mode,
            "grid_operation": grid_op,
        })
        t += timedelta(minutes=15)

    return slots


def build_solar_frozen_forecast(
    solar: SolarData,
    now: datetime,
) -> list[dict[str, Any]]:
    """Return the yesterday/today/tomorrow solar forecast as
    [{t_ms, forecast_w, forecast_kwh}]. Yesterday is included so the chart
    can render the full 72 h window — yesterday's entries come from the
    coordinator's persisted yesterday store (re-attached to `solar.entries`
    on each cycle); today and tomorrow come straight from the live HA
    forecast entities.
    """
    today = now.date()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    in_window = {yesterday, today, tomorrow}
    result = []
    for e in solar.entries:
        if e.start.date() not in in_window:
            continue
        slot_h = (e.end - e.start).total_seconds() / 3600.0
        w = e.expected_kwh / slot_h * 1000.0 if slot_h > 0 else 0.0
        result.append({
            "t": int(e.start.timestamp() * 1000),
            "forecast_w": round(w),
            "forecast_kwh": round(e.expected_kwh, 4),
        })
    return result
