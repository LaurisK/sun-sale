"""Inverter control module — observer + dispatcher behind one entry point.

The coordinator calls ``InverterControlModule.tick(...)`` once per cycle after
the DAG run. The module does three things in this fixed order:

  1. **Observe.** Compare this cycle's ``InverterModeReading`` against the
     last entry of the rolling history. If the decoded mode changed, append
     a new ``InverterModeChange`` and prune samples older than the start of
     yesterday (local time). The result is what feeds the chart's mode-band
     history.

  2. **Plan.** Look up the current ``Schedule`` slot and resolve its target
     mode into a concrete ``StorageModeSpec`` via
     ``pipeline.storage_mode_specs.build_specs``.

  3. **Act (conditional).** When ``automation_enabled`` is True, call
     ``InverterController.apply_mode(target, spec)``. With the switch off
     (the default) the module is observer-only — history grows, plan is
     exposed, but no Modbus writes are issued.

Persistence is the coordinator's responsibility — this module takes the
existing history in, returns the updated one, and the coordinator writes
it back through a ``PersistentStore``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from ..contract.const import (
    DEFAULT_EXPORT_LIMIT_W,
    DEFAULT_INVERTER_MAX_POWER_W,
)
from ..contract.models import (
    BatteryConfig,
    InverterModeChange,
    InverterModeHistory,
    InverterModeReading,
    Schedule,
    ScheduleSlot,
    StorageMode,
    StorageModeSpec,
)
from ..pipeline.storage_mode_specs import build_specs
from .inverter import InverterController

_LOGGER = logging.getLogger(__name__)


class InverterControlModule:
    """Observes inverter mode, maintains the rolling history, and (when enabled) dispatches."""

    def __init__(
        self,
        inverter: InverterController,
        battery_config: BatteryConfig,
        local_tz: Any,
        export_limit_w: int = DEFAULT_EXPORT_LIMIT_W,
        inverter_max_power_w: int = DEFAULT_INVERTER_MAX_POWER_W,
    ) -> None:
        """Initialise with the inverter, battery limits, and deployment caps.

        Args:
            inverter: Platform-aware InverterController (apply_mode is no-op
                on non-Solis platforms).
            battery_config: Battery limits — used to bound currents in
                ``build_specs``.
            local_tz: Local timezone used to compute the start of yesterday
                for history pruning.
            export_limit_w: Backflow / export cap applied in SELL / STORE
                modes. Defaults to the documented Solis maximum.
            inverter_max_power_w: Rated AC output used as the RC active-power
                magnitude when DUMPing.
        """
        self._inverter = inverter
        self._local_tz = local_tz
        self._specs: dict[StorageMode, StorageModeSpec] = build_specs(
            battery_config,
            export_max_w=export_limit_w,
            inverter_max_power_w=inverter_max_power_w,
        )
        self._last_applied_mode: StorageMode | None = None

    async def tick(
        self,
        now: datetime,
        schedule: Schedule | None,
        reading: InverterModeReading,
        history: InverterModeHistory,
        automation_enabled: bool,
        mode_override: StorageMode | None = None,
    ) -> InverterModeHistory:
        """Run one observe → plan → act cycle and return the updated history.

        Args:
            now: Cycle timestamp (tz-aware).
            schedule: Latest DAG-produced Schedule, or ``None`` when the
                pipeline hasn't produced one yet.
            reading: This cycle's observed inverter state.
            history: Existing mode-change history; coordinator-owned.
            automation_enabled: When ``True``, the current slot's target is
                pushed to the inverter via ``apply_mode``. When ``False``,
                the module is observer-only.
            mode_override: When set, overrides the scheduler's current-slot
                choice — this exact StorageMode is dispatched as long as
                ``automation_enabled`` is True. ``None`` keeps sunSale's
                auto choice.

        Returns:
            Updated ``InverterModeHistory``. The coordinator persists this
            back to the rolling-history store.
        """
        updated_history = self._record_observation(now, reading, history)
        if automation_enabled:
            await self._dispatch_current_slot(now, schedule, mode_override)
        return updated_history

    def current_target(
        self,
        now: datetime,
        schedule: Schedule | None,
        mode_override: StorageMode | None = None,
    ) -> StorageMode | None:
        """Return the StorageMode that would be dispatched for ``now``.

        Args:
            now: Cycle timestamp.
            schedule: Latest Schedule, or ``None``.
            mode_override: When set, this is returned directly — it is what
                the dispatcher will push to the inverter.

        Returns:
            Target StorageMode, or ``None`` when no override is set and no
            schedule slot covers ``now``.
        """
        if mode_override is not None:
            return mode_override
        slot = self._current_slot(now, schedule)
        return slot.mode if slot is not None else None

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    def _record_observation(
        self,
        now: datetime,
        reading: InverterModeReading,
        history: InverterModeHistory,
    ) -> InverterModeHistory:
        """Append a new history entry on mode change and prune old samples.

        Strictly append-on-change: when the observed mode matches the last
        recorded mode the history is unchanged. Samples older than the start
        of yesterday (computed in the coordinator's local timezone) are
        dropped.

        Args:
            now: Cycle timestamp.
            reading: Observed inverter state this cycle.
            history: Existing history.

        Returns:
            Updated history (possibly identical to the input).
        """
        samples = list(history.samples)
        last_mode = samples[-1].mode if samples else None
        if reading.mode != last_mode and reading.reg_43110_value is not None:
            samples.append(
                InverterModeChange(
                    timestamp=now,
                    mode=reading.mode,
                    reg_43110_value=reading.reg_43110_value,
                )
            )

        cutoff = self._yesterday_local_midnight(now)
        if samples:
            samples = [s for s in samples if s.timestamp >= cutoff]

        return InverterModeHistory(samples=tuple(samples))

    async def _dispatch_current_slot(
        self,
        now: datetime,
        schedule: Schedule | None,
        mode_override: StorageMode | None = None,
    ) -> None:
        """Apply the current-slot target mode (or the override) to the inverter.

        Args:
            now: Cycle timestamp.
            schedule: Latest Schedule, or ``None``.
            mode_override: When set, dispatched directly and the schedule slot
                is ignored. ``None`` falls back to the slot covering ``now``.
        """
        if mode_override is not None:
            target: StorageMode | None = mode_override
        else:
            slot = self._current_slot(now, schedule)
            target = slot.mode if slot is not None else None
        if target is None:
            return
        spec = self._specs.get(target)
        if spec is None:
            _LOGGER.warning(
                "inverter_control: no spec for target mode %s — skipping dispatch",
                target.value,
            )
            return
        await self._inverter.apply_mode(target, spec)
        if target != self._last_applied_mode:
            _LOGGER.info(
                "inverter_control: dispatched %s%s",
                target.value,
                " (override)" if mode_override is not None else "",
            )
            self._last_applied_mode = target

    @staticmethod
    def _current_slot(
        now: datetime, schedule: Schedule | None
    ) -> ScheduleSlot | None:
        """Return the slot covering ``now`` (no fallback to first slot)."""
        if schedule is None or not schedule.slots:
            return None
        return next((s for s in schedule.slots if s.start <= now < s.end), None)

    def _yesterday_local_midnight(self, now: datetime) -> datetime:
        """Compute ``00:00`` of yesterday in the configured local timezone."""
        local_now = now.astimezone(self._local_tz)
        yday_local = (local_now - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return yday_local.astimezone(now.tzinfo)
