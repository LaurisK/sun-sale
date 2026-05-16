"""Calculator stage: derive per-slot decision flags from prices and generation.

Pure Python — no Home Assistant imports.
Consumption is out of scope for v1; GenerationSeries stays solar-only.
"""
from __future__ import annotations

from datetime import datetime

from ..contract.models import (
    BatteryState,
    CalculationResult,
    EVChargerState,
    GenerationSeries,
    PriceSeries,
    SlotDecision,
)


def calculate(
    prices: PriceSeries,
    generation: GenerationSeries,
    battery_state: BatteryState,
    ev_state: EVChargerState | None,
    now: datetime,
) -> CalculationResult:
    """Produce a CalculationResult from price and generation data.

    v1 responsibility: feed-in lockout.
      - Slots with sell_eur_kwh <= 0 are flagged sell_allowed=False.
      - Contiguous locked-out slots are coalesced into feed_in_lockout_windows.
      - Expected solar production in locked-out slots is reported (no decision taken).
    """
    slots: list[SlotDecision] = []

    for price_slot in prices.slots:
        sell_allowed = price_slot.sell_allowed
        expected_kwh = generation.energy_between(price_slot.start, price_slot.end)
        notes: list[str] = []

        negative_kwh = expected_kwh if not sell_allowed else 0.0

        # Warn if locked-out production exceeds available battery headroom
        if not sell_allowed and expected_kwh > 0:
            headroom_kwh = battery_state.estimated_capacity_kwh * (1.0 - battery_state.soc)
            if expected_kwh > headroom_kwh:
                notes.append("battery_full_during_lockout")

        # Flag slots where we get paid to take energy from the grid
        if price_slot.buy_eur_kwh < 0:
            notes.append("paid_to_charge")

        slots.append(SlotDecision(
            start=price_slot.start,
            end=price_slot.end,
            sell_allowed=sell_allowed,
            expected_solar_kwh=expected_kwh,
            expected_solar_negative_sale_kwh=negative_kwh,
            notes=tuple(notes),
        ))

    lockout_windows = _coalesce_lockout_windows(slots)
    total_negative_kwh = sum(s.expected_solar_negative_sale_kwh for s in slots)

    return CalculationResult(
        slots=tuple(slots),
        feed_in_lockout_windows=lockout_windows,
        total_negative_sale_kwh=total_negative_kwh,
        computed_at=now,
    )


def _coalesce_lockout_windows(
    slots: list[SlotDecision],
) -> tuple[tuple[datetime, datetime], ...]:
    windows: list[tuple[datetime, datetime]] = []
    win_start: datetime | None = None
    win_end: datetime | None = None
    for slot in slots:
        if not slot.sell_allowed:
            if win_start is None:
                win_start = slot.start
            win_end = slot.end
        else:
            if win_start is not None:
                windows.append((win_start, win_end))  # type: ignore[arg-type]
                win_start = None
                win_end = None
    if win_start is not None:
        windows.append((win_start, win_end))  # type: ignore[arg-type]
    return tuple(windows)
