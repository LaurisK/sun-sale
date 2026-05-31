"""Tier 3 DAG nodes — consume Tier 1–2 outputs."""
from __future__ import annotations

import logging

from .. import calculation
from .. import charging_profile as charging_profile_module
from .. import forecast_accuracy
from .. import monthly_bill as monthly_bill_module
from ..dag_engine import DagNode, NodeContext
from ...contract.events import ControlEvent
from ...contract.models import (
    BatteryState,
    BatteryStatus,
    CalculationResult,
    ChargingProfile,
    ForecastAccuracyResult,
    ForecastQualityStore,
    GenerationSeries,
    MonthlyBillResult,
    MonthlyBillState,
    ObservedGenerationSeries,
    ObservedGridSeries,
    PriceSeries,
    SunTimes,
)

_LOGGER = logging.getLogger(__name__)


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


class ForecastAccuracyNode(DagNode):
    """Per-slot error series + EMA quality buckets → ForecastAccuracyResult.

    Combines per-cycle slot alignment (MAE/bias/MAPE) with persistent EMA
    quality tracking across three bucket groups (intensity, solar-day position,
    forecast horizon). ForecastQualityStore and SunTimes come from primary and
    are NOT listed in consumes to avoid self-referential DAG wiring.
    """

    tier = 3
    output_type = ForecastAccuracyResult
    consumes = [GenerationSeries, ObservedGenerationSeries]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[ForecastAccuracyResult, list[ControlEvent]]:
        """Build error series and update EMA quality buckets in one pass."""
        result = forecast_accuracy.build_forecast_accuracy_result(
            forecast=ctx.require(GenerationSeries),
            observed=ctx.require(ObservedGenerationSeries),
            quality_store=ctx.get(ForecastQualityStore),
            sun_times=ctx.get(SunTimes),
            local_tz=ctx.config.local_tz,
            now=ctx.now,
        )
        return result, []


class MonthlyBillNode(DagNode):
    """Accumulate per-slot electricity bill from yday 00:00 to now → MonthlyBillResult.

    Consumes ObservedGridSeries (per-slot gross import/export kWh) and PriceSeries
    (buy/sell prices) to compute net cost per slot. A carry persists the bill from
    month_start to yday_start; it advances at day rollover and resets at month rollover.
    MonthlyBillState comes from primary (loaded by coordinator) and is NOT listed in
    consumes to match the ForecastQualityStore pattern for optional persistent state.
    Tier 3 because it depends on ObservedGridSeries (T2).
    """

    tier = 3
    output_type = MonthlyBillResult
    consumes = [PriceSeries, ObservedGridSeries]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[MonthlyBillResult, list[ControlEvent]]:
        """Compute monthly electricity bill: carry + per-slot yday-to-now costs."""
        result = monthly_bill_module.build_monthly_bill_result(
            grid_series=ctx.require(ObservedGridSeries),
            price_series=ctx.require(PriceSeries),
            stored_state=ctx.get(MonthlyBillState),
            local_tz=ctx.config.local_tz,
            now=ctx.now,
        )
        return result, []


class LockoutNode(DagNode):
    """Detect feed-in lockout windows and per-slot flags → CalculationResult."""

    tier = 3
    output_type = CalculationResult
    consumes = [PriceSeries, GenerationSeries, BatteryState]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[CalculationResult, list[ControlEvent]]:
        """Detect feed-in lockout windows and per-slot solar attribution."""
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
