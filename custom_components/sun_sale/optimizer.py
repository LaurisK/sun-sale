"""Core battery scheduling optimizer.

Pure Python — no Home Assistant imports. Takes a PriceSeries and CalculationResult,
returns an hourly action schedule that maximises profit.

Algorithm: greedy pair-matching.
  1. Enumerate all (buy_hour, sell_hour) pairs where buy < sell and sell_allowed=True.
  2. Rank by net profit per kWh descending.
  3. Greedily assign pairs, tracking per-slot available power and running SoC.
  4. Fill remaining hours with CHARGE_FROM_SOLAR (if solar available) or IDLE.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .battery import trade_profit_per_kwh
from .models import (
    Action,
    BatteryConfig,
    BatteryState,
    CalculationResult,
    PriceSeries,
    PriceSlot,
    Schedule,
    ScheduleSlot,
    SlotDecision,
)


def optimize_schedule(
    price_series: PriceSeries,
    calc: CalculationResult,
    battery_config: BatteryConfig,
    battery_state: BatteryState,
    degradation_cost: float,
    now: datetime,
) -> Schedule:
    """Produce an hourly action schedule that maximises profit.

    Slots flagged sell_allowed=False in calc are excluded from discharge pairs.
    """
    if not price_series.slots:
        return Schedule(slots=[], total_expected_profit_eur=0.0,
                        degradation_cost_per_kwh=degradation_cost, computed_at=now)

    # Align price slots with their decisions; filter to future only
    decision_by_start: dict[datetime, SlotDecision] = {d.start: d for d in calc.slots}
    future_pairs: list[tuple[PriceSlot, SlotDecision]] = [
        (p, decision_by_start[p.start])
        for p in price_series.slots
        if p.end > now and p.start in decision_by_start
    ]

    if not future_pairs:
        return Schedule(slots=[], total_expected_profit_eur=0.0,
                        degradation_cost_per_kwh=degradation_cost, computed_at=now)

    n = len(future_pairs)
    efficiency = battery_config.round_trip_efficiency
    capacity = battery_state.estimated_capacity_kwh
    max_charge = battery_config.max_charge_power_kw
    max_discharge = battery_config.max_discharge_power_kw

    charge_budget = [max_charge] * n
    discharge_budget = [max_discharge] * n
    committed_kwh: list[float] = [0.0] * n
    actions: list[Action | None] = [None] * n
    powers: list[float] = [0.0] * n

    pairs = _rank_trade_pairs(future_pairs, degradation_cost, efficiency)

    for buy_idx, sell_idx, profit in pairs:
        if profit <= 0:
            break

        tradeable = min(charge_budget[buy_idx], discharge_budget[sell_idx])
        if tradeable < 0.01:
            continue

        test = committed_kwh.copy()
        test[buy_idx] += tradeable
        test[sell_idx] -= tradeable * efficiency

        if _simulate_soc(test, battery_state.soc, capacity,
                         battery_config.min_soc, battery_config.max_soc) is None:
            tradeable /= 2.0
            if tradeable < 0.01:
                continue
            test = committed_kwh.copy()
            test[buy_idx] += tradeable
            test[sell_idx] -= tradeable * efficiency
            if _simulate_soc(test, battery_state.soc, capacity,
                             battery_config.min_soc, battery_config.max_soc) is None:
                continue

        committed_kwh[buy_idx] += tradeable
        committed_kwh[sell_idx] -= tradeable * efficiency
        charge_budget[buy_idx] -= tradeable
        discharge_budget[sell_idx] -= tradeable * efficiency

        actions[buy_idx] = Action.CHARGE_FROM_GRID
        powers[buy_idx] += tradeable
        actions[sell_idx] = Action.DISCHARGE_TO_GRID
        powers[sell_idx] += tradeable * efficiency

    # Fill unassigned slots: solar passthrough or idle
    for i, (price_slot, decision) in enumerate(future_pairs):
        if actions[i] is None:
            solar = decision.expected_solar_kwh
            if solar > 0:
                actions[i] = Action.CHARGE_FROM_SOLAR
                powers[i] = solar
            else:
                actions[i] = Action.IDLE

    # Build ScheduleSlot list with running SoC and profit
    slots: list[ScheduleSlot] = []
    running_soc = battery_state.soc
    total_profit = 0.0

    for i, (price_slot, _decision) in enumerate(future_pairs):
        action = actions[i]
        power = powers[i]
        kwh = committed_kwh[i]

        soc_after = max(
            battery_config.min_soc,
            min(battery_config.max_soc, running_soc + kwh / capacity),
        )

        if action == Action.CHARGE_FROM_GRID:
            slot_profit = -(price_slot.buy_eur_kwh * power) - degradation_cost * power
            reason = f"Charge from grid at {price_slot.buy_eur_kwh:.4f} EUR/kWh"
        elif action == Action.DISCHARGE_TO_GRID:
            slot_profit = price_slot.sell_eur_kwh * power - degradation_cost * power
            reason = f"Sell to grid at {price_slot.sell_eur_kwh:.4f} EUR/kWh"
        elif action == Action.CHARGE_FROM_SOLAR:
            slot_profit = 0.0
            reason = f"Solar self-use: {power:.2f} kWh"
        else:
            slot_profit = 0.0
            reason = "Idle"

        slots.append(ScheduleSlot(
            start=price_slot.start,
            end=price_slot.end,
            action=action,
            power_kw=power,
            expected_soc_after=soc_after,
            expected_profit_eur=slot_profit,
            reason=reason,
        ))
        running_soc = soc_after
        total_profit += slot_profit

    return Schedule(
        slots=slots,
        total_expected_profit_eur=total_profit,
        degradation_cost_per_kwh=degradation_cost,
        computed_at=now,
    )


def _rank_trade_pairs(
    slots: list[tuple[PriceSlot, SlotDecision]],
    degradation_cost: float,
    efficiency: float,
) -> list[tuple[int, int, float]]:
    """Return (buy_idx, sell_idx, profit_per_kwh) sorted by profit descending.

    Sell slots with sell_allowed=False are excluded from the search.
    """
    pairs = []
    n = len(slots)
    for buy_idx in range(n):
        for sell_idx in range(buy_idx + 1, n):
            if not slots[sell_idx][1].sell_allowed:
                continue
            profit = trade_profit_per_kwh(
                slots[buy_idx][0].buy_eur_kwh,
                slots[sell_idx][0].sell_eur_kwh,
                degradation_cost,
                efficiency,
            )
            if profit > 0:
                pairs.append((buy_idx, sell_idx, profit))
    pairs.sort(key=lambda x: x[2], reverse=True)
    return pairs


def _simulate_soc(
    committed_kwh: list[float],
    initial_soc: float,
    capacity: float,
    min_soc: float,
    max_soc: float,
) -> list[float] | None:
    """Forward-simulate SoC through a schedule. Returns None if any bound is violated."""
    soc = initial_soc
    result = []
    for kwh in committed_kwh:
        soc += kwh / capacity
        if soc < min_soc - 1e-6 or soc > max_soc + 1e-6:
            return None
        result.append(soc)
    return result
