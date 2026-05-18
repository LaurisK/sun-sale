"""Charging profile: decide per-slot disposition of today's remaining solar.

Pure Python — no Home Assistant imports.

Algorithm:
  free_capacity = (max_soc - soc) * total_capacity_kwh   # space available

  Case 1 — today_remaining_generation <= free_capacity:
      all generating slots from now → end-of-day → SOLAR_CHARGE.

  Case 2 — today_remaining_generation > free_capacity:
      Pick the generating slots with the lowest sell_eur_kwh and cumulatively
      assign them to SOLAR_CHARGE until their summed expected_kwh >= free_capacity.
      Marginal slot is kept whole (slight overfill is acceptable).
      The remaining generating slots become SELL when sell_eur_kwh > 0,
      otherwise NO_EXPORT (curtail rather than pay to export).

  Slots with expected_kwh == 0 → IDLE.

Only today's remaining slots (start.date() == now.date() AND start >= now) are
emitted, mirroring the convention used by `GenerationSeries.today_remaining_kwh`.
"""
from __future__ import annotations

from datetime import datetime

from ..contract.models import (
    BatteryConfig,
    BatteryStatus,
    ChargeMode,
    ChargingProfile,
    ChargingProfileSlot,
    GenerationSeries,
    PriceSeries,
)


def build_charging_profile(
    battery_status: BatteryStatus,
    generation: GenerationSeries,
    prices: PriceSeries,
    battery_config: BatteryConfig,
    now: datetime,
) -> ChargingProfile:
    """Decide per-slot solar disposition (charge/sell/curtail) for today's remaining slots.

    Args:
        battery_status: Live SoC and configured capacity limits.
        generation: Price-grid-aligned GenerationSeries (today slots consumed here).
        prices: PriceSeries used to look up sell_eur_kwh per slot.
        battery_config: Battery limits (min_soc, max_soc, capacity).
        now: Cycle timestamp; only slots with start >= now are included.

    Returns:
        ChargingProfile with a ChargingProfileSlot per remaining today slot.
    """
    free_capacity = max(
        0.0,
        (battery_config.max_soc - battery_status.soc) * battery_status.total_capacity_kwh,
    )

    today = now.date()
    today_remaining = [
        g for g in generation.slots
        if g.start.date() == today and g.start >= now
    ]
    today_remaining_kwh = sum(g.expected_kwh for g in today_remaining)
    solar_exceeds = today_remaining_kwh > free_capacity

    price_by_start = {p.start: p for p in prices.slots}

    generating = [g for g in today_remaining if g.expected_kwh > 0]
    if not solar_exceeds:
        allocated_starts = {g.start for g in generating}
    else:
        ranked = sorted(
            generating,
            key=lambda g: (
                price_by_start[g.start].sell_eur_kwh
                if g.start in price_by_start
                else float("inf"),
                g.start,
            ),
        )
        allocated_starts: set[datetime] = set()
        cumulative = 0.0
        for g in ranked:
            if cumulative >= free_capacity:
                break
            allocated_starts.add(g.start)
            cumulative += g.expected_kwh

    profile_slots: list[ChargingProfileSlot] = []
    allocated_solar_kwh = 0.0
    total_no_export_kwh = 0.0
    for g in today_remaining:
        price_slot = price_by_start.get(g.start)
        sell_price = price_slot.sell_eur_kwh if price_slot is not None else 0.0
        if g.expected_kwh <= 0:
            mode = ChargeMode.IDLE
        elif g.start in allocated_starts:
            mode = ChargeMode.SOLAR_CHARGE
            allocated_solar_kwh += g.expected_kwh
        elif sell_price > 0:
            mode = ChargeMode.SELL
        else:
            mode = ChargeMode.NO_EXPORT
            total_no_export_kwh += g.expected_kwh
        profile_slots.append(ChargingProfileSlot(
            start=g.start,
            end=g.end,
            mode=mode,
            expected_kwh=g.expected_kwh,
            sell_eur_kwh=sell_price,
        ))

    return ChargingProfile(
        slots=tuple(profile_slots),
        free_capacity_kwh=free_capacity,
        today_remaining_generation_kwh=today_remaining_kwh,
        solar_exceeds_capacity=solar_exceeds,
        allocated_solar_kwh=allocated_solar_kwh,
        total_no_export_kwh=total_no_export_kwh,
        computed_at=now,
    )
