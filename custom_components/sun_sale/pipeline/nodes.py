"""DAG nodes — the computation tier of the sunSale pipeline.

Pure Python — no Home Assistant imports.
Each node declares its tier, output_type, and consumed types.
Observer wiring is auto-built by DagEngine._wire() based on these declarations.

Tier map:
  T1: PricingNode, BatteryStateNode
  T2: GenerationNode, DegradationNode
  T3: LockoutNode
  T4: ScheduleNode
"""
from __future__ import annotations

import logging
from datetime import datetime

from . import base_load as base_load_module
from . import battery as battery_module
from . import calculation, charging_profile as charging_profile_module, forecast_accuracy, schedule as schedule_module
from . import profitability as profitability_module
from ..inbound import battery as battery_inbound
from ..inbound import forecast as forecast_module
from ..inbound import generation as generation_module
from ..inbound import pricing as pricing_module
from .dag_engine import DagNode, NodeContext
from ..contract.events import ControlEvent, InverterActionEvent
from ..contract.models import (
    Action,
    BaseLoadProfile,
    BatteryConfig,
    BatteryReading,
    BatteryRuntimeEstimate,
    BatteryState,
    BatteryStatus,
    CalculationResult,
    ChargingProfile,
    DegradationCost,
    EstimatedCapacity,
    ForecastErrorSeries,
    GenerationHistory,
    GenerationSeries,
    HouseholdLoadHistory,
    NordpoolData,
    ObservedGenerationSeries,
    PriceHistory,
    PriceSeries,
    ProfitabilityScore,
    SolarData,
    Schedule,
    YesterdayPrices,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tier 1 nodes — consume primary data only
# ---------------------------------------------------------------------------

class PricingNode(DagNode):
    """Assemble 72h yesterday→today→tomorrow PriceSeries with tariff applied."""

    tier = 1
    output_type = PriceSeries
    consumes = [NordpoolData, YesterdayPrices]

    async def _compute(self, ctx: NodeContext) -> tuple[PriceSeries, list[ControlEvent]]:
        """Assemble PriceSeries from NordpoolData + YesterdayPrices with tariff applied."""
        nordpool = ctx.require(NordpoolData)
        yesterday = ctx.require(YesterdayPrices)
        series = pricing_module.build_price_series_72h(
            nordpool, yesterday, ctx.config.tariff, now=ctx.now
        )
        return series, []


class BatteryStateNode(DagNode):
    """Combine inverter reading + learned capacity → BatteryState."""

    tier = 1
    output_type = BatteryState
    consumes = [BatteryReading, EstimatedCapacity]

    async def _compute(self, ctx: NodeContext) -> tuple[BatteryState, list[ControlEvent]]:
        """Combine live SoC reading with learned capacity into BatteryState."""
        reading = ctx.require(BatteryReading)
        cap = ctx.require(EstimatedCapacity)
        return BatteryState(soc=reading.soc, estimated_capacity_kwh=cap.value_kwh), []


class BatteryStatusNode(DagNode):
    """Snapshot configured limits + live SoC into BatteryStatus."""

    tier = 1
    output_type = BatteryStatus
    consumes = [BatteryReading]

    async def _compute(self, ctx: NodeContext) -> tuple[BatteryStatus, list[ControlEvent]]:
        """Combine live inverter telemetry with configured limits into BatteryStatus."""
        reading = ctx.require(BatteryReading)
        status = battery_inbound.build_battery_status(reading, ctx.config.battery)
        return status, []


class BaseLoadProfileNode(DagNode):
    """24h hour-of-day baseload profile from rolling household-load history."""

    tier = 1
    output_type = BaseLoadProfile
    consumes = [HouseholdLoadHistory]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[BaseLoadProfile, list[ControlEvent]]:
        """Build 24-bucket baseload profile from rolling household-load history."""
        history = ctx.require(HouseholdLoadHistory)
        profile = base_load_module.build_base_load_profile(
            history, ctx.config.local_tz, now=ctx.now,
        )
        return profile, []


# ---------------------------------------------------------------------------
# Tier 2 nodes — consume Tier 1 secondary + primary
# ---------------------------------------------------------------------------

class GenerationNode(DagNode):
    """Normalise SolarData into GenerationSeries aligned to PriceSeries resolution."""

    tier = 2
    output_type = GenerationSeries
    consumes = [SolarData, PriceSeries]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[GenerationSeries, list[ControlEvent]]:
        """Resample SolarData onto the PriceSeries grid to produce GenerationSeries."""
        solar = ctx.require(SolarData)
        price_series = ctx.require(PriceSeries)
        gen = forecast_module.build_generation_series(solar, price_series.slots, now=ctx.now)
        return gen, []


class ObservedGenerationNode(DagNode):
    """Difference inverter today-total samples → ObservedGenerationSeries.

    Tier 2 because it depends on `PriceSeries` (T1 secondary) for the grid;
    `GenerationHistory` is primary, deposited by the coordinator from the
    persistent sample store.
    """

    tier = 2
    output_type = ObservedGenerationSeries
    consumes = [GenerationHistory, PriceSeries]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[ObservedGenerationSeries, list[ControlEvent]]:
        """Difference persisted today-total samples into per-slot ObservedGenerationSeries."""
        history = ctx.require(GenerationHistory)
        price_series = ctx.require(PriceSeries)
        series = generation_module.build_observed_generation_series(
            history, price_series.slots, now=ctx.now, local_tz=ctx.config.local_tz
        )
        return series, []


class DegradationNode(DagNode):
    """Compute battery degradation cost per kWh from BatteryState + BatteryConfig."""

    tier = 2
    output_type = DegradationCost
    consumes = [BatteryState]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[DegradationCost, list[ControlEvent]]:
        """Compute wear cost per kWh from BatteryState + BatteryConfig."""
        state = ctx.require(BatteryState)
        cost = battery_module.degradation_cost_per_kwh(ctx.config.battery, state)
        return DegradationCost(value_kwh=cost), []


# ---------------------------------------------------------------------------
# Tier 3 nodes — consume Tier 1–2 secondary
# ---------------------------------------------------------------------------

class ChargingProfileNode(DagNode):
    """Decide per-slot disposition of today's remaining solar → ChargingProfile."""

    tier = 3
    output_type = ChargingProfile
    consumes = [BatteryStatus, GenerationSeries, PriceSeries]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[ChargingProfile, list[ControlEvent]]:
        """Decide per-slot solar disposition for today's remaining generation."""
        status = ctx.require(BatteryStatus)
        generation = ctx.require(GenerationSeries)
        prices = ctx.require(PriceSeries)
        profile = charging_profile_module.build_charging_profile(
            battery_status=status,
            generation=generation,
            prices=prices,
            battery_config=ctx.config.battery,
            now=ctx.now,
        )
        return profile, []


class BatteryRuntimeNode(DagNode):
    """Forward-simulate pure baseload drain → BatteryRuntimeEstimate.

    Ignores solar generation and the optimizer schedule by design: this is a
    worst-case "household-only depletion" reserve, comparable across cycles.
    See docs/base_load_missing.md.
    """

    tier = 2
    output_type = BatteryRuntimeEstimate
    consumes = [BatteryStatus, BaseLoadProfile]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[BatteryRuntimeEstimate, list[ControlEvent]]:
        """Forward-simulate pure baseload drain to estimate battery depletion time."""
        estimate = base_load_module.estimate_battery_runtime(
            battery_status=ctx.require(BatteryStatus),
            battery_config=ctx.config.battery,
            profile=ctx.require(BaseLoadProfile),
            local_tz=ctx.config.local_tz,
            now=ctx.now,
        )
        return estimate, []


class ForecastAccuracyNode(DagNode):
    """Pair forecast vs. observed solar slots → ForecastErrorSeries.

    Read-only signal today (MAE/bias/MAPE for monitoring); the same series is
    the input a future calibration stage would consume to fit a per-hour
    correction or to compare forecast sources.
    """

    tier = 3
    output_type = ForecastErrorSeries
    consumes = [GenerationSeries, ObservedGenerationSeries]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[ForecastErrorSeries, list[ControlEvent]]:
        """Align forecast vs. observed solar slots and compute MAE/bias/MAPE."""
        forecast = ctx.require(GenerationSeries)
        observed = ctx.require(ObservedGenerationSeries)
        series = forecast_accuracy.build_forecast_error_series(
            forecast, observed, now=ctx.now,
        )
        return series, []


class ProfitabilityNode(DagNode):
    """Score today's peak against rolling daily-peak history → ProfitabilityScore."""

    tier = 2
    output_type = ProfitabilityScore
    consumes = [PriceSeries, PriceHistory]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[ProfitabilityScore, list[ControlEvent]]:
        """Compute profitability score using today's peak from PriceSeries and rolling history."""
        price_series = ctx.require(PriceSeries)
        history = ctx.require(PriceHistory)
        score = profitability_module.compute_profitability_score(
            price_series=price_series,
            history=history,
            now=ctx.now,
        )
        return score, []


class LockoutNode(DagNode):
    """Detect feed-in lockout windows and per-slot flags → CalculationResult."""

    tier = 3
    output_type = CalculationResult
    consumes = [PriceSeries, GenerationSeries, BatteryState]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[CalculationResult, list[ControlEvent]]:
        """Detect feed-in lockout windows and per-slot sell_allowed flags."""
        price_series = ctx.require(PriceSeries)
        generation = ctx.require(GenerationSeries)
        battery_state = ctx.require(BatteryState)

        result = calculation.calculate(
            prices=price_series,
            generation=generation,
            battery_state=battery_state,
            now=ctx.now,
        )
        return result, []


# ---------------------------------------------------------------------------
# Tier 4 nodes — consume Tier 1–3 secondary
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Shared mutable cell for cross-cycle deduplication state
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Internal slot helpers
# ---------------------------------------------------------------------------

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
