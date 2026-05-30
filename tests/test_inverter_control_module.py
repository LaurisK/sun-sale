"""Tests for the InverterControlModule (observer / dispatcher behind one entry point)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.sun_sale.contract.models import (
    InverterModeChange,
    InverterModeHistory,
    InverterModeReading,
    Schedule,
    ScheduleSlot,
    StorageMode,
)
from custom_components.sun_sale.outbound.inverter_control_module import InverterControlModule
from tests.conftest import default_battery_config


NOW = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)


def _mock_inverter() -> MagicMock:
    inv = MagicMock()
    inv.apply_mode = AsyncMock()
    return inv


def _module(inverter=None) -> InverterControlModule:
    return InverterControlModule(
        inverter=inverter or _mock_inverter(),
        battery_config=default_battery_config(),
        local_tz=timezone.utc,
        export_limit_w=10_000,
        inverter_max_power_w=10_000,
    )


def _reading(mode: StorageMode, reg: int | None = None, ts: datetime = NOW) -> InverterModeReading:
    return InverterModeReading(
        timestamp=ts,
        reg_43110_value=reg if reg is not None else 1,
        mode=mode,
        charge_a=0.0,
        discharge_a=0.0,
        rc_setpoint_w=0,
    )


def _schedule_with(mode: StorageMode, now: datetime = NOW) -> Schedule:
    slot = ScheduleSlot(
        start=now - timedelta(minutes=10),
        end=now + timedelta(minutes=50),
        mode=mode,
        power_kw=2.0,
        expected_soc_after=0.5,
        expected_profit_eur=0.1,
        reason="test",
    )
    return Schedule(slots=[slot], total_expected_profit_eur=0.1,
                    degradation_cost_per_kwh=0.02, computed_at=now)


# ---------------------------------------------------------------------------
# Observer-only path (automation OFF)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observer_only_does_not_call_apply_mode():
    inv = _mock_inverter()
    mod = _module(inv)
    result = await mod.tick(
        now=NOW,
        schedule=_schedule_with(StorageMode.GULP),
        reading=_reading(StorageMode.STBY, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=False,
    )
    inv.apply_mode.assert_not_awaited()
    # Observation recorded even though we never dispatched.
    assert len(result.samples) == 1
    assert result.samples[0].mode == StorageMode.STBY


@pytest.mark.asyncio
async def test_first_observation_appends_to_history():
    mod = _module()
    history = InverterModeHistory(samples=())
    result = await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.STORE, reg=1),
        history=history, automation_enabled=False,
    )
    assert len(result.samples) == 1
    assert result.samples[0].mode == StorageMode.STORE
    assert result.samples[0].timestamp == NOW


@pytest.mark.asyncio
async def test_unchanged_mode_does_not_grow_history():
    existing = InverterModeChange(
        timestamp=NOW - timedelta(hours=2),
        mode=StorageMode.STBY,
        reg_43110_value=1,
    )
    mod = _module()
    result = await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.STBY, reg=1),
        history=InverterModeHistory(samples=(existing,)),
        automation_enabled=False,
    )
    assert result.samples == (existing,)


@pytest.mark.asyncio
async def test_mode_change_appends_new_entry():
    existing = InverterModeChange(
        timestamp=NOW - timedelta(hours=2),
        mode=StorageMode.STBY,
        reg_43110_value=1,
    )
    mod = _module()
    result = await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.GULP, reg=33),
        history=InverterModeHistory(samples=(existing,)),
        automation_enabled=False,
    )
    assert len(result.samples) == 2
    assert result.samples[-1].mode == StorageMode.GULP
    assert result.samples[-1].reg_43110_value == 33


@pytest.mark.asyncio
async def test_skip_append_when_register_readback_unavailable():
    # ``reading.reg_43110_value is None`` is the controller-side "unavailable"
    # signal — we must not pollute history with phantom UNKNOWN entries.
    mod = _module()
    reading = InverterModeReading(
        timestamp=NOW, reg_43110_value=None, mode=StorageMode.UNKNOWN,
        charge_a=None, discharge_a=None, rc_setpoint_w=None,
    )
    result = await mod.tick(
        now=NOW, schedule=None, reading=reading,
        history=InverterModeHistory(samples=()), automation_enabled=False,
    )
    assert result.samples == ()


@pytest.mark.asyncio
async def test_old_samples_pruned_at_yesterday_midnight():
    # Yesterday-local-midnight cutoff in UTC tz = NOW.date() - 1 day at 00:00.
    very_old = InverterModeChange(
        timestamp=NOW - timedelta(days=5),
        mode=StorageMode.STBY, reg_43110_value=1,
    )
    recent = InverterModeChange(
        timestamp=NOW - timedelta(hours=3),
        mode=StorageMode.GULP, reg_43110_value=33,
    )
    mod = _module()
    result = await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.GULP, reg=33),
        history=InverterModeHistory(samples=(very_old, recent)),
        automation_enabled=False,
    )
    assert very_old not in result.samples
    assert recent in result.samples


# ---------------------------------------------------------------------------
# Active dispatch path (automation ON)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_calls_apply_mode_with_current_slot_target():
    inv = _mock_inverter()
    mod = _module(inv)
    schedule = _schedule_with(StorageMode.GULP)
    await mod.tick(
        now=NOW, schedule=schedule,
        reading=_reading(StorageMode.STBY, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    inv.apply_mode.assert_awaited_once()
    target_mode = inv.apply_mode.await_args.args[0]
    assert target_mode == StorageMode.GULP


@pytest.mark.asyncio
async def test_dispatch_skipped_when_no_schedule():
    inv = _mock_inverter()
    mod = _module(inv)
    await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.STBY, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    inv.apply_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_skipped_when_no_slot_covers_now():
    inv = _mock_inverter()
    mod = _module(inv)
    # Build a schedule whose only slot is in the past.
    past_slot = ScheduleSlot(
        start=NOW - timedelta(hours=5), end=NOW - timedelta(hours=4),
        mode=StorageMode.GULP, power_kw=2.0,
        expected_soc_after=0.5, expected_profit_eur=0.1, reason="x",
    )
    schedule = Schedule(slots=[past_slot], total_expected_profit_eur=0.0,
                        degradation_cost_per_kwh=0.0, computed_at=NOW)
    await mod.tick(
        now=NOW, schedule=schedule,
        reading=_reading(StorageMode.STBY, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    inv.apply_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_target_returns_slot_mode():
    mod = _module()
    schedule = _schedule_with(StorageMode.STORE)
    assert mod.current_target(NOW, schedule) == StorageMode.STORE


@pytest.mark.asyncio
async def test_current_target_returns_none_when_no_schedule():
    assert _module().current_target(NOW, None) is None
