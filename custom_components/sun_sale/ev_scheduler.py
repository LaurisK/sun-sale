"""EV charge scheduler.

Pure Python — no Home Assistant imports. Selects cheapest available hours
within a departure window to meet the EV's target SoC.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .models import EVChargeSlot, EVChargerConfig, EVChargerState, EVSchedule, TariffResult


def schedule_ev_charge(
    tariffs: list[TariffResult],
    ev_config: EVChargerConfig,
    ev_state: EVChargerState,
    now: datetime,
) -> EVSchedule:
    """Schedule EV charging into cheapest available hours.

    Algorithm:
    1. Compute energy needed: (target_soc - current_soc) * battery_capacity_kwh
    2. Determine hours needed: ceil(energy / max_power)
    3. Filter tariffs to future hours within departure window
    4. Select cheapest N hours, assign full power (partial power for last slot)
    """
    if not ev_state.is_plugged_in:
        return EVSchedule(slots=[], total_cost_eur=0.0, total_energy_kwh=0.0, computed_at=now)

    energy_needed = max(
        0.0,
        (ev_state.target_soc - ev_state.soc) * ev_config.battery_capacity_kwh,
    )
    if energy_needed < 0.01:
        return EVSchedule(slots=[], total_cost_eur=0.0, total_energy_kwh=0.0, computed_at=now)

    future = [t for t in tariffs if t.hour + timedelta(hours=1) > now]
    if ev_state.departure_time is not None:
        future = [t for t in future if t.hour < ev_state.departure_time]

    if not future:
        return EVSchedule(slots=[], total_cost_eur=0.0, total_energy_kwh=0.0, computed_at=now)

    full_hours = int(energy_needed // ev_config.max_charge_power_kw)
    remaining_energy = energy_needed - full_hours * ev_config.max_charge_power_kw
    hours_needed = full_hours + (1 if remaining_energy > 0.01 else 0)

    cheap = _cheapest_hours(future, hours_needed, now, ev_state.departure_time)
    cheap_sorted = sorted(cheap, key=lambda t: t.hour)
    cheap_hours_set = {t.hour for t in cheap}

    # Determine which cheap slot gets partial power (the last one chronologically)
    partial_hour = cheap_sorted[-1].hour if cheap_sorted and remaining_energy > 0.01 else None

    slots: list[EVChargeSlot] = []
    total_cost = 0.0
    total_energy = 0.0

    for tariff in future:
        if tariff.hour in cheap_hours_set:
            power = remaining_energy if tariff.hour == partial_hour else ev_config.max_charge_power_kw
            cost = power * tariff.buy_price
            slots.append(EVChargeSlot(
                start=tariff.hour,
                end=tariff.hour + timedelta(hours=1),
                charge_power_kw=power,
                cost_eur=cost,
            ))
            total_cost += cost
            total_energy += power
        else:
            slots.append(EVChargeSlot(
                start=tariff.hour,
                end=tariff.hour + timedelta(hours=1),
                charge_power_kw=0.0,
                cost_eur=0.0,
            ))

    return EVSchedule(
        slots=slots,
        total_cost_eur=total_cost,
        total_energy_kwh=total_energy,
        computed_at=now,
    )


def _cheapest_hours(
    tariffs: list[TariffResult],
    hours_needed: int,
    start: datetime,
    end: datetime | None,
) -> list[TariffResult]:
    """Select the N cheapest tariff hours within [start, end)."""
    window = [t for t in tariffs if t.hour >= start]
    if end is not None:
        window = [t for t in window if t.hour < end]
    return sorted(window, key=lambda t: t.buy_price)[:hours_needed]
