"""Tier 2 DAG nodes — consume Tier 1 outputs + primary data."""
from __future__ import annotations

import logging

from .. import base_load as base_load_module
from .. import battery as battery_module
from .. import profitability as profitability_module
from ...inbound import forecast as forecast_module
from ...inbound import generation as generation_module
from ...inbound import grid as grid_module
from ..dag_engine import DagNode, NodeContext
from ...contract.events import ControlEvent
from ...contract.models import (
    BakedObservedHistory,
    BaseLoadProfile,
    BatteryRuntimeEstimate,
    BatteryState,
    BatteryStatus,
    DegradationCost,
    GenerationSeries,
    GridExportPowerHistory,
    GridImportPowerHistory,
    ObservedGenerationSeries,
    ObservedGridSeries,
    PriceHistory,
    PriceSeries,
    ProfitabilityScore,
    PvPowerHistory,
    SolarData,
)

_LOGGER = logging.getLogger(__name__)


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
        gen = forecast_module.build_generation_series(
            solar, price_series.slots, now=ctx.now, local_tz=ctx.config.local_tz
        )
        return gen, []


class ObservedGenerationNode(DagNode):
    """Average PV power samples → ObservedGenerationSeries (raw, today + yesterday).

    Yesterday's slots are raw averages at this stage; the once-per-day bake-in
    (Phase 3) will substitute proportionally-corrected slot values sourced
    from the inverter's yesterday-total. Today stays raw until the next
    rollover. Tier 2 because it depends on ``PriceSeries`` (T1 secondary).
    """

    tier = 2
    output_type = ObservedGenerationSeries
    consumes = [PvPowerHistory, PriceSeries, BakedObservedHistory]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[ObservedGenerationSeries, list[ControlEvent]]:
        """Build per-slot ObservedGenerationSeries from PV power + baked history."""
        pv_power_history = ctx.require(PvPowerHistory)
        price_series = ctx.require(PriceSeries)
        baked_history = ctx.require(BakedObservedHistory)
        series = generation_module.build_observed_generation_series(
            pv_power_history, price_series.slots,
            now=ctx.now, local_tz=ctx.config.local_tz,
            baked_history=baked_history,
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


class ObservedGridNode(DagNode):
    """Average signed grid-power samples → per-slot ObservedGridSeries.

    Splits each sample into its positive (import) and negative (export)
    components before averaging so gross flows are preserved even when the
    signed mean is near zero. Yesterday and the older day are raw averages
    at this stage; the once-per-day bake-in (Phase 3) will substitute
    proportionally-corrected slot values sourced from the inverter's
    daily-resetting import / export counters. Tier 2 because it depends on
    ``PriceSeries`` (T1 secondary).
    """

    tier = 2
    output_type = ObservedGridSeries
    consumes = [
        GridImportPowerHistory,
        GridExportPowerHistory,
        PriceSeries,
        BakedObservedHistory,
    ]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[ObservedGridSeries, list[ControlEvent]]:
        """Build per-slot ObservedGridSeries from per-direction histories."""
        price_series = ctx.require(PriceSeries)
        baked_history = ctx.require(BakedObservedHistory)
        series = grid_module.build_observed_grid_series(
            import_power_history=ctx.require(GridImportPowerHistory),
            export_power_history=ctx.require(GridExportPowerHistory),
            price_slots=price_series.slots,
            now=ctx.now,
            local_tz=ctx.config.local_tz,
            baked_history=baked_history,
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
