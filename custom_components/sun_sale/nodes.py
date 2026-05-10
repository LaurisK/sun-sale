"""DAG nodes — the computation tier of the sunSale pipeline.

Pure Python — no Home Assistant imports.
Each node declares its tier, output_type, and consumed types.
Observer wiring is auto-built by DagEngine._wire() based on these declarations.

Tier map:
  T1: PricingNode, BatteryStateNode
  T2: GenerationNode, DegradationNode, EVSchedulerNode (optional)
  T3: LockoutNode
  T4: OptimizerNode
  T5: DashboardNode (sink)
"""
from __future__ import annotations

import logging
from datetime import datetime

from . import battery as battery_module
from . import calculator, ev_scheduler, optimizer
from . import forecast as forecast_module
from . import pricing as pricing_module
from . import dashboard as dashboard_module
from .dag_engine import DagNode, NodeContext
from .events import ControlEvent, EVActionEvent, InverterActionEvent
from .models import (
    Action,
    BatteryConfig,
    BatteryReading,
    BatteryState,
    CalculationResult,
    DashboardData,
    DegradationCost,
    EstimatedCapacity,
    EVChargerConfig,
    EVChargerState,
    EVSchedule,
    GenerationSeries,
    NordpoolPrices,
    PriceSeries,
    RawSolarData,
    Schedule,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tier 1 nodes — consume primary data only
# ---------------------------------------------------------------------------

class PricingNode(DagNode):
    """Apply tariff formulas to Nordpool raw prices → PriceSeries."""

    tier = 1
    output_type = PriceSeries
    consumes = [NordpoolPrices]

    async def _compute(self, ctx: NodeContext) -> tuple[PriceSeries, list[ControlEvent]]:
        nordpool = ctx.require(NordpoolPrices)
        series = pricing_module.build_price_series(
            nordpool.slots, ctx.config.tariff, now=ctx.now
        )
        return series, []


class BatteryStateNode(DagNode):
    """Combine inverter reading + learned capacity → BatteryState."""

    tier = 1
    output_type = BatteryState
    consumes = [BatteryReading, EstimatedCapacity]

    async def _compute(self, ctx: NodeContext) -> tuple[BatteryState, list[ControlEvent]]:
        reading = ctx.require(BatteryReading)
        cap = ctx.require(EstimatedCapacity)
        return BatteryState(soc=reading.soc, estimated_capacity_kwh=cap.value_kwh), []


# ---------------------------------------------------------------------------
# Tier 2 nodes — consume Tier 1 secondary + primary
# ---------------------------------------------------------------------------

class GenerationNode(DagNode):
    """Normalise RawSolarData into GenerationSeries aligned to PriceSeries resolution."""

    tier = 2
    output_type = GenerationSeries
    consumes = [RawSolarData, PriceSeries]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[GenerationSeries, list[ControlEvent]]:
        raw = ctx.require(RawSolarData)
        price_series = ctx.require(PriceSeries)
        gen = forecast_module.build_generation_series(raw, price_series, now=ctx.now)
        return gen, []


class DegradationNode(DagNode):
    """Compute battery degradation cost per kWh from BatteryState + BatteryConfig."""

    tier = 2
    output_type = DegradationCost
    consumes = [BatteryState]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[DegradationCost, list[ControlEvent]]:
        state = ctx.require(BatteryState)
        cost = battery_module.degradation_cost_per_kwh(ctx.config.battery, state)
        return DegradationCost(value_kwh=cost), []


class EVSchedulerNode(DagNode):
    """Schedule EV charging into cheapest hours → EVSchedule + optional EVActionEvent.

    Only registered by the coordinator when EV is enabled.
    """

    tier = 2
    output_type = EVSchedule
    consumes = [PriceSeries, EVChargerState]

    def __init__(self, last_ev_action_ref: _LastActionRef) -> None:
        super().__init__()
        self._last = last_ev_action_ref

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[EVSchedule | None, list[ControlEvent]]:
        price_series = ctx.require(PriceSeries)
        ev_state = ctx.require(EVChargerState)
        ev_config = ctx.config.ev
        if ev_config is None:
            return None, []

        ev_sched = ev_scheduler.schedule_ev_charge(
            price_series=price_series,
            ev_config=ev_config,
            ev_state=ev_state,
            now=ctx.now,
        )

        events: list[ControlEvent] = []
        current = _current_ev_slot(ev_sched, ctx.now)
        if current is not None:
            key = f"{current.charge_power_kw:.3f}"
            if key != self._last.value:
                self._last.value = key
                events.append(EVActionEvent(charge_power_kw=current.charge_power_kw))

        return ev_sched, events


# ---------------------------------------------------------------------------
# Tier 3 nodes — consume Tier 1–2 secondary
# ---------------------------------------------------------------------------

class LockoutNode(DagNode):
    """Detect feed-in lockout windows and per-slot flags → CalculationResult."""

    tier = 3
    output_type = CalculationResult
    consumes = [PriceSeries, GenerationSeries, BatteryState]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[CalculationResult, list[ControlEvent]]:
        price_series = ctx.require(PriceSeries)
        generation = ctx.require(GenerationSeries)
        battery_state = ctx.require(BatteryState)
        ev_state: EVChargerState | None = ctx.get(EVChargerState)

        result = calculator.calculate(
            prices=price_series,
            generation=generation,
            battery_state=battery_state,
            ev_state=ev_state,
            now=ctx.now,
        )
        return result, []


# ---------------------------------------------------------------------------
# Tier 4 nodes — consume Tier 1–3 secondary
# ---------------------------------------------------------------------------

class OptimizerNode(DagNode):
    """Greedy pair-match optimizer → Schedule + optional InverterActionEvent."""

    tier = 4
    output_type = Schedule
    consumes = [PriceSeries, CalculationResult, GenerationSeries, BatteryState, DegradationCost]

    def __init__(self, last_inverter_action_ref: _LastActionRef) -> None:
        super().__init__()
        self._last = last_inverter_action_ref

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[Schedule, list[ControlEvent]]:
        price_series = ctx.require(PriceSeries)
        calc = ctx.require(CalculationResult)
        generation = ctx.require(GenerationSeries)
        battery_state = ctx.require(BatteryState)
        deg_cost = ctx.require(DegradationCost)

        schedule = optimizer.optimize_schedule(
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


# ---------------------------------------------------------------------------
# Tier 5 nodes — sink; consume Tier 1–4 secondary
# ---------------------------------------------------------------------------

class DashboardNode(DagNode):
    """Build presentation data for the web panel → DashboardData."""

    tier = 5
    output_type = DashboardData
    consumes = [NordpoolPrices, RawSolarData, BatteryReading, PriceSeries, GenerationSeries, Schedule]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[DashboardData, list[ControlEvent]]:
        nordpool = ctx.require(NordpoolPrices)
        solar = ctx.require(RawSolarData)
        reading = ctx.require(BatteryReading)
        schedule: Schedule | None = ctx.get(Schedule)

        future_slots = dashboard_module.build_future_slots(
            nordpool=nordpool,
            solar=solar,
            reading=reading,
            schedule=schedule,
            battery_config=ctx.config.battery,
            tariff_config=ctx.config.tariff,
            now=ctx.now,
        )
        frozen = dashboard_module.build_solar_frozen_forecast(solar=solar, now=ctx.now)
        return DashboardData(future_slots=future_slots, solar_frozen_forecast=frozen), []


# ---------------------------------------------------------------------------
# Shared mutable cell for cross-cycle deduplication state
# ---------------------------------------------------------------------------

class _LastActionRef:
    """Mutable reference cell holding the last dispatched action key string."""
    def __init__(self) -> None:
        self.value: str | None = None


def make_last_ref() -> _LastActionRef:
    return _LastActionRef()


# ---------------------------------------------------------------------------
# Internal slot helpers
# ---------------------------------------------------------------------------

def _current_schedule_slot(schedule: Schedule, now: datetime):
    if not schedule.slots:
        return None
    return next((s for s in schedule.slots if s.start <= now < s.end), schedule.slots[0])


def _current_ev_slot(ev_sched: EVSchedule, now: datetime):
    if not ev_sched.slots:
        return None
    return next((s for s in ev_sched.slots if s.start <= now < s.end), None)
