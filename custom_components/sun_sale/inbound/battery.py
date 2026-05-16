"""Battery stage: normalise BatteryReading + BatteryConfig into BatteryStatus.

Pure Python — no Home Assistant imports.

Mirrors the inbound/pricing.py and inbound/forecast.py pattern: a small,
testable assembly function that downstream DAG nodes call. Total capacity
is the user-configured nominal value; remaining capacity is the SoC-weighted
share of it.
"""
from __future__ import annotations

from ..contract.models import BatteryConfig, BatteryReading, BatteryStatus


def build_battery_status(
    reading: BatteryReading,
    config: BatteryConfig,
) -> BatteryStatus:
    """Combine live telemetry with configured limits into a BatteryStatus."""
    return BatteryStatus(
        total_capacity_kwh=config.nominal_capacity_kwh,
        max_charge_power_kw=config.max_charge_power_kw,
        max_discharge_power_kw=config.max_discharge_power_kw,
        soc=reading.soc,
        remaining_capacity_kwh=reading.soc * config.nominal_capacity_kwh,
    )
