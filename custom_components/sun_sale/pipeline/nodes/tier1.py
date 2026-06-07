"""Tier 1 DAG nodes — consume primary data only."""
from __future__ import annotations

import logging

from .. import base_load as base_load_module
from ...inbound import battery as battery_inbound
from ...inbound import pricing as pricing_module
from ..dag_engine import DagNode, NodeContext
from ...contract.events import ControlEvent
from ...contract.models import (
    BaseLoadProfile,
    BatteryReading,
    BatteryState,
    BatteryStatus,
    ConsumptionDailyBuckets,
    EstimatedCapacity,
    NordpoolData,
    PriceSeries,
    YesterdayPrices,
)

_LOGGER = logging.getLogger(__name__)


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
    """24h hour-of-day P15 baseload profile from per-day consumption rollups."""

    tier = 1
    output_type = BaseLoadProfile
    consumes = [ConsumptionDailyBuckets]

    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[BaseLoadProfile, list[ControlEvent]]:
        """Build 24-bucket P15 profile from the rolling daily-bucket history."""
        buckets = ctx.require(ConsumptionDailyBuckets)
        profile = base_load_module.build_base_load_profile(
            buckets, ctx.config.local_tz, now=ctx.now,
        )
        return profile, []
