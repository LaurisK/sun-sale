"""EV charge scheduler.

Pure Python — no Home Assistant imports. Selects cheapest available hours
within a departure window to meet the EV's target SoC.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..contract.models import EVChargeSlot, EVChargerConfig, EVChargerState, EVSchedule, PriceSeries, PriceSlot


def schedule_ev_charge(
    price_series: PriceSeries,
    ev_config: EVChargerConfig,
    ev_state: EVChargerState,
    now: datetime,
) -> EVSchedule:
    """Schedule EV charging into cheapest available hours.

    Algorithm:
    1. Compute energy needed: (target_soc - current_soc) * battery_capacity_kwh
    2. Determine hours needed: ceil(energy / max_power)
    3. Filter price slots to future slots within departure window
    4. Select cheapest N slots, assign full power (partial power for last slot)
    """
    if not ev_state.is_plugged_in:
        return EVSchedule(slots=[], total_cost_eur=0.0, total_energy_kwh=0.0, computed_at=now)

    energy_needed = max(
        0.0,
        (ev_state.target_soc - ev_state.soc) * ev_config.battery_capacity_kwh,
    )
    if energy_needed < 0.01:
        return EVSchedule(slots=[], total_cost_eur=0.0, total_energy_kwh=0.0, computed_at=now)

    future = [s for s in price_series.slots if s.end > now]
    if ev_state.departure_time is not None:
        future = [s for s in future if s.start < ev_state.departure_time]

    if not future:
        return EVSchedule(slots=[], total_cost_eur=0.0, total_energy_kwh=0.0, computed_at=now)

    full_hours = int(energy_needed // ev_config.max_charge_power_kw)
    remaining_energy = energy_needed - full_hours * ev_config.max_charge_power_kw
    hours_needed = full_hours + (1 if remaining_energy > 0.01 else 0)

    cheap = _cheapest_slots(future, hours_needed, now, ev_state.departure_time)
    cheap_sorted = sorted(cheap, key=lambda s: s.start)
    cheap_starts = {s.start for s in cheap}

    partial_start = cheap_sorted[-1].start if cheap_sorted and remaining_energy > 0.01 else None

    slots: list[EVChargeSlot] = []
    total_cost = 0.0
    total_energy = 0.0

    for slot in future:
        if slot.start in cheap_starts:
            power = remaining_energy if slot.start == partial_start else ev_config.max_charge_power_kw
            cost = power * slot.buy_eur_kwh
            slots.append(EVChargeSlot(
                start=slot.start,
                end=slot.end,
                charge_power_kw=power,
                cost_eur=cost,
            ))
            total_cost += cost
            total_energy += power
        else:
            slots.append(EVChargeSlot(
                start=slot.start,
                end=slot.end,
                charge_power_kw=0.0,
                cost_eur=0.0,
            ))

    return EVSchedule(
        slots=slots,
        total_cost_eur=total_cost,
        total_energy_kwh=total_energy,
        computed_at=now,
    )


def _cheapest_slots(
    slots: list[PriceSlot],
    hours_needed: int,
    start: datetime,
    end: datetime | None,
) -> list[PriceSlot]:
    """Select the N cheapest price slots within [start, end)."""
    window = [s for s in slots if s.start >= start]
    if end is not None:
        window = [s for s in window if s.start < end]
    return sorted(window, key=lambda s: s.buy_eur_kwh)[:hours_needed]
