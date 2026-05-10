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

from .models import (
    Action,
    BatteryConfig,
    BatteryReading,
    NordpoolPrices,
    RawSolarData,
    Schedule,
    ScheduleSlot,
    TariffConfig,
)
from . import tariff as tariff_module

_SLOT_MIN = 15
_SLOT_H = _SLOT_MIN / 60.0


def _floor_15min(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


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
    nordpool: NordpoolPrices,
    solar: RawSolarData,
    reading: BatteryReading,
    schedule: Schedule | None,
    battery_config: BatteryConfig,
    tariff_config: TariffConfig,
    now: datetime,
) -> list[dict[str, Any]]:
    """Build 15-min future slots from now to end of tomorrow.

    Each slot: {t, buy_price, sell_price, solar_forecast_w, battery_soc_pct,
                inverter_mode, grid_operation}
    """
    solar_watts = solar.watts

    sched_by_hour: dict[datetime, ScheduleSlot] = {}
    if schedule:
        for s in schedule.slots:
            sched_by_hour[s.start.replace(minute=0, second=0, microsecond=0)] = s

    current_soc = reading.soc * 100.0
    load_w = reading.household_load_kw * 1000.0
    soc = current_soc

    tomorrow = now.date() + timedelta(days=1)
    end_dt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 23, 45, tzinfo=timezone.utc)

    slots: list[dict[str, Any]] = []
    t = _floor_15min(now)

    while t <= end_dt:
        spot = nordpool.raw_15min.get(t)
        if spot is None:
            t += timedelta(minutes=15)
            continue

        buy_p = tariff_module.buy_price(spot, tariff_config)
        sell_p = tariff_module.sell_price(spot, tariff_config)
        solar_w = solar_watts.get(t, 0.0)
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
    solar: RawSolarData,
    now: datetime,
) -> list[dict[str, Any]]:
    """Return today's frozen solar forecast as [{t_ms, forecast_w}]."""
    if not solar.watts:
        return []
    today = now.date()
    return [
        {
            "t": int(ts.timestamp() * 1000),
            "forecast_w": round(w),
            "forecast_kwh": round(w * _SLOT_H / 1000, 4),
        }
        for ts, w in sorted(solar.watts.items())
        if ts.date() == today
    ]
