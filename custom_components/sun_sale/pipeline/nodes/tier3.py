"""Tier 3 DAG nodes — consume Tier 1–2 outputs."""
from __future__ import annotations

import logging

from .. import calculation
from .. import charging_profile as charging_profile_module
from .. import forecast_accuracy
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
    ObservedGenerationSeries,
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
