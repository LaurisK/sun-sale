"""Tests for inbound/battery.py — pure Python, no HA required."""
from custom_components.sun_sale.contract.models import BatteryReading
from custom_components.sun_sale.inbound.battery import build_battery_status
from tests.conftest import default_battery_config


def _reading(soc: float = 0.5) -> BatteryReading:
    return BatteryReading(
        soc=soc,
        power_kw=0.0,
        grid_power_kw=0.0,
        household_load_kw=0.2,
    )


def test_total_capacity_from_config():
    status = build_battery_status(_reading(), default_battery_config())
    assert status.total_capacity_kwh == 10.0


def test_max_charge_power_from_config():
    status = build_battery_status(_reading(), default_battery_config())
    assert status.max_charge_power_kw == 5.0


def test_max_discharge_power_from_config():
    status = build_battery_status(_reading(), default_battery_config())
    assert status.max_discharge_power_kw == 5.0


def test_soc_passthrough():
    status = build_battery_status(_reading(soc=0.73), default_battery_config())
    assert status.soc == 0.73


def test_remaining_capacity_at_half_soc():
    status = build_battery_status(_reading(soc=0.5), default_battery_config())
    assert status.remaining_capacity_kwh == 5.0


def test_remaining_capacity_at_empty():
    status = build_battery_status(_reading(soc=0.0), default_battery_config())
    assert status.remaining_capacity_kwh == 0.0


def test_remaining_capacity_at_full():
    status = build_battery_status(_reading(soc=1.0), default_battery_config())
    assert status.remaining_capacity_kwh == 10.0


def test_status_is_immutable():
    import dataclasses
    status = build_battery_status(_reading(), default_battery_config())
    try:
        status.soc = 0.99  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("BatteryStatus should be frozen")


# ---------------------------------------------------------------------------
# Node wiring: BatteryStatusNode within the DAG engine
# ---------------------------------------------------------------------------

def test_battery_status_node_produces_status_from_primary():
    import asyncio
    from datetime import datetime, timezone

    from custom_components.sun_sale.contract.models import (
        BatteryStatus,
        SunSaleConfig,
    )
    from custom_components.sun_sale.pipeline.dag_engine import NodeContext
    from custom_components.sun_sale.pipeline.nodes import BatteryStatusNode

    now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    config = SunSaleConfig(
        tariff=None,  # not consumed by this node
        battery=default_battery_config(),
    )
    ctx = NodeContext(
        primary={type(_reading()): _reading(soc=0.42)},
        secondary={},
        config=config,
        now=now,
    )

    node = BatteryStatusNode()
    asyncio.run(node.run(ctx))

    status = ctx.secondary[BatteryStatus]
    assert isinstance(status, BatteryStatus)
    assert status.soc == 0.42
    assert status.total_capacity_kwh == 10.0
    assert status.remaining_capacity_kwh == 4.2
    assert status.max_charge_power_kw == 5.0
    assert status.max_discharge_power_kw == 5.0
