"""Tier 4 DAG nodes — consume Tier 1–3 outputs."""
from __future__ import annotations

import logging

from .. import schedule as schedule_module
from ..dag_engine import DagNode, NodeContext
from ...contract.events import ControlEvent
from ...contract.models import (
    BatteryState,
    CalculationResult,
    ChargingProfile,
    DegradationCost,
    GenerationSeries,
    PriceSeries,
    Schedule,
)

_LOGGER = logging.getLogger(__name__)


class ScheduleNode(DagNode):
    """Greedy pair-match scheduler → Schedule(StorageMode per slot).

    Emits no ControlEvents — dispatch is owned by the post-DAG inverter control
    module (added in the next refactor chunk). ChargingProfile is consumed when
    available to disambiguate STORE vs HOARD for solar slots; without it, solar
    slots default to STORE.
    """

    tier = 4
    output_type = Schedule
    consumes = [
        PriceSeries,
        CalculationResult,
        GenerationSeries,
        BatteryState,
        DegradationCost,
        ChargingProfile,
    ]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[Schedule, list[ControlEvent]]:
        """Run greedy optimizer and produce a StorageMode-tagged Schedule."""
        price_series = ctx.require(PriceSeries)
        calc = ctx.require(CalculationResult)
        battery_state = ctx.require(BatteryState)
        deg_cost = ctx.require(DegradationCost)
        charging_profile = ctx.get(ChargingProfile)
        # GenerationSeries is consumed for tier-ordering but not used directly here.
        ctx.get(GenerationSeries)

        schedule = schedule_module.optimize_schedule(
            price_series=price_series,
            calc=calc,
            battery_config=ctx.config.battery,
            battery_state=battery_state,
            degradation_cost=deg_cost.value_kwh,
            now=ctx.now,
            charging_profile=charging_profile,
        )

        return schedule, []
