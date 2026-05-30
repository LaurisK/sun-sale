"""Monthly electricity bill computation — pure Python, no HA imports.

Builds a per-price-slot cost series for the live window [yday_start, now)
and adds a persisted `carry_eur` that covers the bill from month-start up
to the start of yesterday. The two together give the running monthly bill.

Per-slot import/export kWh are sourced from the upstream `ObservedGridSeries`
(see ``inbound/grid.py``). Costs are priced through `PriceSeries`:

    slot_cost = imported_kWh * buy_eur_kwh − exported_kWh * sell_eur_kwh

No floor is applied to `sell_eur_kwh`; negative sell prices charge the
exporter as they would on the real market.

Recalculation contract — `ObservedGridSeries` may refine today's slots via
the end-of-day counter correction right up until midnight, so any slot
dated to today is treated as provisional. The persisted `carry_eur`
therefore never includes today; only at the next day-rollover is the
just-finished day baked in, and that bake-in re-reads the current
`ObservedGridSeries` so late-arriving refinements flow through.

State machine (`MonthlyBillState`, persisted by the coordinator):
  * month rollover  → finalize prev month: previous_month_eur = stored.carry_eur
                      + compute_bill_slots(old_yday_start → new_month_start);
                      then carry resets to 0 for the new month
  * day rollover    → carry += compute_bill_slots(
                          max(old_yday_start, month_start) → new_yday_start)
  * same local day  → carry unchanged

Live slots are always computed over `[max(yday_start, month_start), now)`, so on
the first day of a new month they cover only the current month (the previous
month's tail lives in `previous_month_eur`, not in `slots`).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..contract.models import (
    BillSlot,
    MonthlyBillResult,
    MonthlyBillState,
    ObservedGridSeries,
    PriceSeries,
)

if TYPE_CHECKING:
    from datetime import tzinfo


def compute_bill_slots(
    grid_series: ObservedGridSeries,
    price_series: PriceSeries,
    t_start: datetime,
    t_end: datetime,
) -> list[BillSlot]:
    """Return per-price-slot electricity cost for grid flow in [t_start, t_end).

    Iterates the price slots overlapping ``[t_start, t_end)`` and looks up each
    slot's imported/exported kWh in ``grid_series``. The grid-series resolution
    matches the price grid, so the match is by ``start`` time; the small fraction
    of slots that the series has not yet covered (e.g. windows older than the
    grid history's retention) contribute zero on both sides.

    Cost uses the slot's ``buy_eur_kwh`` on imports and ``sell_eur_kwh`` on
    exports (no floor — negative sell prices are honoured). When ``t_start`` or
    ``t_end`` falls partway through a price slot, the cost is pro-rated by the
    overlap fraction so partial-slot windows (e.g. month-rollover bridges)
    bill accurately.

    Args:
        grid_series: Observed per-slot gross import/export kWh from
            ``inbound/grid.py:build_observed_grid_series``.
        price_series: Tariff-adjusted price series covering at least [t_start, t_end).
        t_start: Start of the billing window (inclusive, tz-aware).
        t_end: End of the billing window (exclusive, tz-aware).

    Returns:
        List of BillSlot objects, one per overlapping price slot.
    """
    grid_by_start = {gs.start: gs for gs in grid_series.slots}

    result: list[BillSlot] = []
    for price_slot in price_series.window(t_start, t_end):
        slot_start = max(price_slot.start, t_start)
        slot_end = min(price_slot.end, t_end)

        full_secs = (price_slot.end - price_slot.start).total_seconds()
        win_secs = (slot_end - slot_start).total_seconds()
        overlap_fraction = (win_secs / full_secs) if full_secs > 0 else 0.0

        gs = grid_by_start.get(price_slot.start)
        if gs is not None and overlap_fraction > 0:
            imported_kwh = gs.imported_kwh * overlap_fraction
            exported_kwh = gs.exported_kwh * overlap_fraction
        else:
            imported_kwh = 0.0
            exported_kwh = 0.0

        net_cost_eur = (
            imported_kwh * price_slot.buy_eur_kwh
            - exported_kwh * price_slot.sell_eur_kwh
        )

        result.append(BillSlot(
            start=slot_start,
            end=slot_end,
            imported_kwh=round(imported_kwh, 4),
            exported_kwh=round(exported_kwh, 4),
            buy_eur_kwh=price_slot.buy_eur_kwh,
            sell_eur_kwh=price_slot.sell_eur_kwh,
            net_cost_eur=round(net_cost_eur, 6),
        ))
    return result


def build_monthly_bill_result(
    grid_series: ObservedGridSeries,
    price_series: PriceSeries,
    stored_state: MonthlyBillState | None,
    local_tz: tzinfo,
    now: datetime,
) -> MonthlyBillResult:
    """Compute the net monthly electricity bill and advance the carry as needed.

    Branching on stored_state vs. the current cycle's month/day:

    * Month rollover: finalize the previous month by combining stored.carry_eur
      with a freshly computed bridge over [old_yday_start, current_month_start)
      → save into previous_month_eur. Reset carry to 0 for the new month.
    * Day rollover (same month): fold the just-finished day into carry by
      computing [max(old_yday_start, month_start), new_yday_start) fresh.
    * Same local day: carry unchanged.

    Live slots are computed over [max(yday_start, month_start), now) so they
    never include data attributed to the previous month.

    Args:
        grid_series: ObservedGridSeries with per-slot gross import/export kWh.
        price_series: Full 72-hour tariff-adjusted price series.
        stored_state: Previously persisted state, or None on first run.
        local_tz: HA installation timezone, used to locate local midnights.
        now: Current cycle UTC timestamp.

    Returns:
        MonthlyBillResult with per-slot data, carry, previous-month total,
        and updated state for persistence.
    """
    local_now = now.astimezone(local_tz)
    current_month_str = local_now.strftime("%Y-%m")
    month_start = _local_midnight_utc(
        date(local_now.year, local_now.month, 1), local_tz,
    )
    yday_date = local_now.date() - timedelta(days=1)
    yday_str = yday_date.isoformat()
    yday_start = _local_midnight_utc(yday_date, local_tz)

    if stored_state is None:
        carry_eur = 0.0
        previous_month_str = ""
        previous_month_eur = 0.0
    elif stored_state.month_str != current_month_str:
        old_yday_start = _local_midnight_utc(
            date.fromisoformat(stored_state.yday_str), local_tz,
        )
        prev_bridge_slots = compute_bill_slots(
            grid_series, price_series, old_yday_start, month_start,
        )
        previous_month_eur = stored_state.carry_eur + sum(
            s.net_cost_eur for s in prev_bridge_slots
        )
        previous_month_str = stored_state.month_str
        carry_eur = 0.0
    elif stored_state.yday_str != yday_str:
        old_yday_start = _local_midnight_utc(
            date.fromisoformat(stored_state.yday_str), local_tz,
        )
        bridge_start = max(old_yday_start, month_start)
        bridge_slots = compute_bill_slots(
            grid_series, price_series, bridge_start, yday_start,
        )
        bridge_eur = sum(s.net_cost_eur for s in bridge_slots)
        carry_eur = stored_state.carry_eur + bridge_eur
        previous_month_str = stored_state.previous_month_str
        previous_month_eur = stored_state.previous_month_eur
    else:
        carry_eur = stored_state.carry_eur
        previous_month_str = stored_state.previous_month_str
        previous_month_eur = stored_state.previous_month_eur

    live_start = max(yday_start, month_start)
    slots = compute_bill_slots(grid_series, price_series, live_start, now)
    yday_to_now_eur = sum(s.net_cost_eur for s in slots)

    return MonthlyBillResult(
        slots=tuple(slots),
        carry_eur=carry_eur,
        yday_to_now_eur=yday_to_now_eur,
        total_month_eur=carry_eur + yday_to_now_eur,
        month_str=current_month_str,
        previous_month_str=previous_month_str,
        previous_month_eur=previous_month_eur,
        updated_state=MonthlyBillState(
            month_str=current_month_str,
            carry_eur=carry_eur,
            yday_str=yday_str,
            previous_month_str=previous_month_str,
            previous_month_eur=previous_month_eur,
        ),
        computed_at=now,
    )


def _local_midnight_utc(d: date, local_tz: tzinfo) -> datetime:
    """Return UTC datetime for local midnight on the given date.

    Args:
        d: Local calendar date.
        local_tz: Local timezone.

    Returns:
        UTC-aware datetime corresponding to 00:00 local time on `d`.
    """
    return datetime(d.year, d.month, d.day, tzinfo=local_tz).astimezone(timezone.utc)
