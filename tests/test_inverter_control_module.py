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
        schedule=_schedule_with(StorageMode.GridCharge),
        reading=_reading(StorageMode.StandBy, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=False,
    )
    inv.apply_mode.assert_not_awaited()
    # Observation recorded even though we never dispatched.
    assert len(result.samples) == 1
    assert result.samples[0].mode == StorageMode.StandBy


@pytest.mark.asyncio
async def test_first_observation_appends_to_history():
    mod = _module()
    history = InverterModeHistory(samples=())
    result = await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=history, automation_enabled=False,
    )
    assert len(result.samples) == 1
    assert result.samples[0].mode == StorageMode.SelfUse
    assert result.samples[0].timestamp == NOW


@pytest.mark.asyncio
async def test_unchanged_mode_does_not_grow_history():
    existing = InverterModeChange(
        timestamp=NOW - timedelta(hours=2),
        mode=StorageMode.StandBy,
        reg_43110_value=1,
    )
    mod = _module()
    result = await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.StandBy, reg=1),
        history=InverterModeHistory(samples=(existing,)),
        automation_enabled=False,
    )
    assert result.samples == (existing,)


@pytest.mark.asyncio
async def test_mode_change_appends_new_entry():
    existing = InverterModeChange(
        timestamp=NOW - timedelta(hours=2),
        mode=StorageMode.StandBy,
        reg_43110_value=1,
    )
    mod = _module()
    result = await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.GridCharge, reg=33),
        history=InverterModeHistory(samples=(existing,)),
        automation_enabled=False,
    )
    assert len(result.samples) == 2
    assert result.samples[-1].mode == StorageMode.GridCharge
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
        mode=StorageMode.StandBy, reg_43110_value=1,
    )
    recent = InverterModeChange(
        timestamp=NOW - timedelta(hours=3),
        mode=StorageMode.GridCharge, reg_43110_value=33,
    )
    mod = _module()
    result = await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.GridCharge, reg=33),
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
    schedule = _schedule_with(StorageMode.GridCharge)
    await mod.tick(
        now=NOW, schedule=schedule,
        reading=_reading(StorageMode.StandBy, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    inv.apply_mode.assert_awaited_once()
    target_mode = inv.apply_mode.await_args.args[0]
    assert target_mode == StorageMode.GridCharge


@pytest.mark.asyncio
async def test_dispatch_skipped_when_no_schedule():
    inv = _mock_inverter()
    mod = _module(inv)
    await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.StandBy, reg=1),
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
        mode=StorageMode.GridCharge, power_kw=2.0,
        expected_soc_after=0.5, expected_profit_eur=0.1, reason="x",
    )
    schedule = Schedule(slots=[past_slot], total_expected_profit_eur=0.0,
                        degradation_cost_per_kwh=0.0, computed_at=NOW)
    await mod.tick(
        now=NOW, schedule=schedule,
        reading=_reading(StorageMode.StandBy, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    inv.apply_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_target_returns_slot_mode():
    mod = _module()
    schedule = _schedule_with(StorageMode.SelfUse)
    assert mod.current_target(NOW, schedule) == StorageMode.SelfUse


@pytest.mark.asyncio
async def test_current_target_returns_none_when_no_schedule():
    assert _module().current_target(NOW, None) is None


# ---------------------------------------------------------------------------
# Manual StorageMode override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_dispatches_overridden_mode_not_slot_mode():
    inv = _mock_inverter()
    mod = _module(inv)
    schedule = _schedule_with(StorageMode.SelfUse)
    await mod.tick(
        now=NOW, schedule=schedule,
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
        mode_override=StorageMode.Discharge,
    )
    inv.apply_mode.assert_awaited_once()
    assert inv.apply_mode.await_args.args[0] == StorageMode.Discharge


@pytest.mark.asyncio
async def test_override_dispatches_when_no_schedule_slot_covers_now():
    inv = _mock_inverter()
    mod = _module(inv)
    await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.StandBy, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
        mode_override=StorageMode.GridCharge,
    )
    inv.apply_mode.assert_awaited_once()
    assert inv.apply_mode.await_args.args[0] == StorageMode.GridCharge


@pytest.mark.asyncio
async def test_override_dispatched_even_when_automation_off():
    """Operator override bypasses the automation_enabled gate (Phase 1)."""
    inv = _mock_inverter()
    mod = _module(inv)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.SelfUse),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=False,
        mode_override=StorageMode.Discharge,
    )
    inv.apply_mode.assert_awaited_once()
    assert inv.apply_mode.await_args.args[0] == StorageMode.Discharge


@pytest.mark.asyncio
async def test_no_dispatch_when_automation_off_and_no_override():
    """With automation off and no override, the module stays observer-only."""
    inv = _mock_inverter()
    mod = _module(inv)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.SelfUse),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=False,
        mode_override=None,
    )
    inv.apply_mode.assert_not_awaited()
    assert mod.last_dispatch_outcome == "automation_disabled"


@pytest.mark.asyncio
async def test_current_target_returns_override_when_set():
    mod = _module()
    schedule = _schedule_with(StorageMode.SelfUse)
    assert mod.current_target(
        NOW, schedule, mode_override=StorageMode.FeedIn,
    ) == StorageMode.FeedIn


@pytest.mark.asyncio
async def test_current_target_override_wins_even_without_schedule():
    mod = _module()
    assert mod.current_target(
        NOW, None, mode_override=StorageMode.Discharge,
    ) == StorageMode.Discharge


# ---------------------------------------------------------------------------
# Phase 2: commanded-mode tracking + verify loop
# ---------------------------------------------------------------------------


def _module_with_hass(inverter=None) -> InverterControlModule:
    """Build a module with a non-None hass so the verify scheduler runs."""
    return InverterControlModule(
        inverter=inverter or _mock_inverter(),
        battery_config=default_battery_config(),
        local_tz=timezone.utc,
        export_limit_w=10_000,
        inverter_max_power_w=10_000,
        hass=MagicMock(),
    )


class _ScheduledCallback:
    """Captures the most recent ``async_call_later`` invocation.

    Replaces the real helper for tests so we can verify (a) the verify-tick
    is scheduled with the right delay and (b) the captured async callback
    runs the correct read-back logic when manually fired.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, object]] = []
        self.cancels: int = 0

    def __call__(self, _hass, delay, callback):
        self.calls.append((delay, callback))

        def _cancel() -> None:
            self.cancels += 1

        return _cancel

    @property
    def last_callback(self):
        return self.calls[-1][1] if self.calls else None


@pytest.fixture
def call_later(monkeypatch):
    """Patch ``async_call_later`` in the control module for verify-loop tests."""
    sched = _ScheduledCallback()
    monkeypatch.setattr(
        "custom_components.sun_sale.outbound.inverter_control_module."
        "async_call_later",
        sched,
    )
    return sched


@pytest.mark.asyncio
async def test_commanded_change_force_writes_and_schedules_verify(call_later):
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=None)
    mod = _module_with_hass(inv)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    inv.apply_mode.assert_awaited_once()
    assert inv.apply_mode.await_args.kwargs.get("force") is True
    assert mod.last_commanded_mode == StorageMode.Discharge
    assert mod.last_commanded_at == NOW
    assert mod.verify_state == "pending"
    assert len(call_later.calls) == 1
    assert call_later.calls[0][0] == 2  # _VERIFY_INITIAL_DELAY_S


@pytest.mark.asyncio
async def test_unchanged_command_does_not_force_or_reschedule(call_later):
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=None)
    mod = _module_with_hass(inv)
    # First tick commands Discharge — force=True, one verify scheduled.
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    assert len(call_later.calls) == 1
    # Second tick same command — force=False, no new verify.
    later = NOW + timedelta(minutes=5)
    await mod.tick(
        now=later, schedule=_schedule_with(StorageMode.Discharge, now=later),
        reading=_reading(StorageMode.Discharge, reg=2, ts=later),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    assert inv.apply_mode.await_count == 2
    assert inv.apply_mode.await_args.kwargs.get("force") is False
    assert len(call_later.calls) == 1  # no new schedule


@pytest.mark.asyncio
async def test_verify_tick_marks_state_ok_when_observed_matches(call_later):
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    spec_reg_for_discharge = mod._specs[StorageMode.Discharge].reg_43110_value
    inv.get_storage_control_word = MagicMock(return_value=spec_reg_for_discharge)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    # First poll fires at +2 s — observed already matches commanded, so we
    # land on "ok" without burning the rest of the window.
    await call_later.last_callback(NOW + timedelta(seconds=2))
    assert mod.verify_state == "ok"
    assert mod.last_verify_observed_reg == spec_reg_for_discharge
    assert mod.last_verify_at == NOW + timedelta(seconds=2)


@pytest.mark.asyncio
async def test_verify_keeps_polling_within_window_on_mismatch(call_later):
    """Mismatch within the 30 s window must reschedule, not retry."""
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=0xDEAD)  # wrong reg
    mod = _module_with_hass(inv)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    initial_write_count = inv.apply_mode.await_count
    # Fire several polls within the 30 s window — each should schedule the
    # next at +5 s without re-issuing the write.
    for elapsed in (2, 7, 12, 17, 22, 27):
        cb = call_later.last_callback
        await cb(NOW + timedelta(seconds=elapsed))
        assert mod.verify_state == "pending"
    assert inv.apply_mode.await_count == initial_write_count  # no retry yet
    # Each poll past the first scheduled a follow-up at the 5 s cadence.
    assert all(c[0] == 5 for c in call_later.calls[1:])


@pytest.mark.asyncio
async def test_verify_mismatch_retries_then_marks_mismatch(call_later):
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=0xDEAD)  # wrong reg
    mod = _module_with_hass(inv)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    initial_write_count = inv.apply_mode.await_count
    # First window — fire a poll past the 30 s mark. This triggers the
    # single force-write retry and resets the window.
    await call_later.last_callback(NOW + timedelta(seconds=31))
    assert mod.verify_state == "pending"  # mid-retry, still polling
    assert inv.apply_mode.await_count == initial_write_count + 1
    assert inv.apply_mode.await_args.kwargs.get("force") is True
    # Retry window — fire a poll past the 30 s retry mark.
    await call_later.last_callback(NOW + timedelta(seconds=31 + 31))
    assert mod.verify_state == "mismatch"
    # No further retries.
    assert inv.apply_mode.await_count == initial_write_count + 1


@pytest.mark.asyncio
async def test_force_verify_now_noop_before_any_command(call_later):
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=None)
    mod = _module_with_hass(inv)
    await mod.force_verify_now()
    # No commanded mode → no read, no apply_mode call.
    inv.get_storage_control_word.assert_not_called()
    inv.apply_mode.assert_not_awaited()
    assert mod.verify_state is None


@pytest.mark.asyncio
async def test_force_verify_now_runs_verify_immediately(call_later):
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    discharge_reg = mod._specs[StorageMode.Discharge].reg_43110_value
    inv.get_storage_control_word = MagicMock(return_value=discharge_reg)
    # Establish a commanded mode first.
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    pending_before = call_later.cancels
    # Force-verify — should cancel pending scheduled verify and re-run now.
    await mod.force_verify_now()
    assert call_later.cancels == pending_before + 1
    assert mod.verify_state == "ok"


@pytest.mark.asyncio
async def test_new_command_cancels_pending_verify(call_later):
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=None)
    mod = _module_with_hass(inv)
    # First command — schedules verify@30s.
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    assert call_later.cancels == 0
    # Second command (different mode) — should cancel pending verify and
    # schedule a fresh one.
    later = NOW + timedelta(seconds=5)
    await mod.tick(
        now=later, schedule=_schedule_with(StorageMode.GridCharge, now=later),
        reading=_reading(StorageMode.SelfUse, reg=1, ts=later),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    assert call_later.cancels == 1
    assert len(call_later.calls) == 2
    assert mod.last_commanded_mode == StorageMode.GridCharge
