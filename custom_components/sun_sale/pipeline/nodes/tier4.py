"""Tier 4 DAG nodes — consume Tier 1–3 outputs."""
from __future__ import annotations

import logging
from datetime import datetime

from .. import schedule as schedule_module
from ..dag_engine import DagNode, NodeContext
from ...contract.events import ControlEvent, InverterActionEvent
from ...contract.models import (
    BatteryState,
    CalculationResult,
    DegradationCost,
    GenerationSeries,
    PriceSeries,
    Schedule,
)

_LOGGER = logging.getLogger(__name__)


class _LastActionRef:
    """Mutable reference cell holding the last dispatched action key string."""

    def __init__(self) -> None:
        """Initialise with no prior action."""
        self.value: str | None = None


def make_last_ref() -> _LastActionRef:
    """Create a fresh cross-cycle deduplication reference cell.

    Returns:
        New _LastActionRef with value=None.
    """
    return _LastActionRef()


def _current_schedule_slot(schedule: Schedule, now: datetime):
    """Return the schedule slot active at now, falling back to the first slot.

    Args:
        schedule: Computed schedule.
        now: Current time.

    Returns:
        Matching ScheduleSlot or schedule.slots[0], or None for an empty schedule.
    """
    if not schedule.slots:
        return None
    return next((s for s in schedule.slots if s.start <= now < s.end), schedule.slots[0])


class ScheduleNode(DagNode):
    """Greedy pair-match scheduler → Schedule + optional InverterActionEvent."""

    tier = 4
    output_type = Schedule
    consumes = [PriceSeries, CalculationResult, GenerationSeries, BatteryState, DegradationCost]

    def __init__(self, last_inverter_action_ref: _LastActionRef) -> None:
        """Initialise with the cross-cycle deduplication reference cell.

        Args:
            last_inverter_action_ref: Mutable cell shared with the coordinator
                for cross-cycle action deduplication.
        """
        super().__init__()
        self._last = last_inverter_action_ref

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[Schedule, list[ControlEvent]]:
        """Run greedy optimizer; emit InverterActionEvent when current-slot action changes."""
        price_series = ctx.require(PriceSeries)
        calc = ctx.require(CalculationResult)
        generation = ctx.require(GenerationSeries)
        battery_state = ctx.require(BatteryState)
        deg_cost = ctx.require(DegradationCost)

        schedule = schedule_module.optimize_schedule(
            price_series=price_series,
            calc=calc,
            battery_config=ctx.config.battery,
            battery_state=battery_state,
            degradation_cost=deg_cost.value_kwh,
            now=ctx.now,
        )

        events: list[ControlEvent] = []
        current = _current_schedule_slot(schedule, ctx.now)
        if current is not None:
            key = f"{current.action.value}:{current.power_kw:.3f}"
            if key != self._last.value:
                self._last.value = key
                events.append(InverterActionEvent(action=current.action, power_kw=current.power_kw))

        return schedule, events
