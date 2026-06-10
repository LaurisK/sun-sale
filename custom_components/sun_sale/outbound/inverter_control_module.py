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

  3. **Act (conditional).** When ``automation_enabled`` is True, or when
     ``mode_override`` is set (operator intent always reaches the inverter),
     call ``InverterController.apply_mode(target, spec)``. With the switch
     off AND no override set, the module is observer-only — history grows,
     plan is exposed, but no Modbus writes are issued.

  4. **Verify (commanded-change only).** When the resolved target differs
     from the last-commanded mode, the dispatch is a *force-write* (bypasses
     the per-register cache idempotency in ``apply_mode``) and a verify
     callback is scheduled via ``async_call_later`` ~30 s later. The
     callback reads register 43110 and compares to the spec's bitmask. On
     match, ``verify_state`` flips to ``ok``. On mismatch, the module
     force-writes once more and schedules a second verify; if that also
     mismatches, ``verify_state`` becomes ``mismatch`` and is surfaced on
     the diagnostic sensor so the operator can see that the write is not
     reaching the inverter (Modbus chain issue, mode lock, etc.).

Persistence is the coordinator's responsibility — this module takes the
existing history in, returns the updated one, and the coordinator writes
it back through a ``PersistentStore``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later

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

# Verify-tick cadence after a commanded-mode change (seconds).
#  - INITIAL: first poll fires almost immediately so a fast-engaging inverter
#    is detected without an arbitrary wait.
#  - POLL_INTERVAL: subsequent polls within the window — keeps the UX badge
#    snappy (~5 s) without saturating the Modbus chain.
#  - WINDOW: total time we'll wait per attempt before either retrying the
#    write (first time) or declaring the write stuck (second time). Sized to
#    outlast solis_modbus's slowest poll cycle (~30 s) so a mismatch verdict
#    means the inverter really hasn't applied the command.
_VERIFY_INITIAL_DELAY_S = 2
_VERIFY_POLL_INTERVAL_S = 5
_VERIFY_WINDOW_S = 30


class InverterControlModule:
    """Observes inverter mode, maintains the rolling history, and (when enabled) dispatches."""

    def __init__(
        self,
        inverter: InverterController,
        battery_config: BatteryConfig,
        local_tz: Any,
        export_limit_w: int = DEFAULT_EXPORT_LIMIT_W,
        inverter_max_power_w: int = DEFAULT_INVERTER_MAX_POWER_W,
        hass: HomeAssistant | None = None,
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
            hass: Home Assistant instance, used to schedule the verify-tick
                callbacks after a commanded-mode change. May be ``None`` in
                unit tests, in which case verify-ticks are disabled (a
                ``MagicMock`` works equally well — the module just calls
                ``async_call_later`` on it).
        """
        self._inverter = inverter
        self._hass = hass
        self._local_tz = local_tz
        self._specs: dict[StorageMode, StorageModeSpec] = build_specs(
            battery_config,
            export_max_w=export_limit_w,
            inverter_max_power_w=inverter_max_power_w,
        )
        self._last_applied_mode: StorageMode | None = None
        # Phase 0 visibility: per-tick dispatch outcome surfaced on the
        # ObservedInverterModeSensor diagnostic attributes.
        self._last_dispatch_outcome: str | None = None
        self._last_dispatch_target: StorageMode | None = None
        self._last_dispatch_at: datetime | None = None
        self._last_automation_enabled: bool | None = None
        # Phase 2: commanded-mode tracking + verify loop. ``last_commanded_*``
        # are our own truth (what we asked the inverter to do); the verify
        # loop reads back from solis_modbus a beat later to confirm.
        self._last_commanded_mode: StorageMode | None = None
        self._last_commanded_at: datetime | None = None
        self._verify_state: str | None = None  # pending / ok / mismatch
        self._last_verify_at: datetime | None = None
        self._last_verify_observed_reg: int | None = None
        self._verify_cancel = None  # cancel-callback from async_call_later
        self._verify_retried: bool = False
        # Start of the current verify window — used to decide between "still
        # polling" and "window exhausted, retry or give up".
        self._verify_window_started_at: datetime | None = None

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
                the scheduler path is silent; an explicit ``mode_override``
                still dispatches (operator intent bypasses this gate).
            mode_override: When set, overrides the scheduler's current-slot
                choice — this exact StorageMode is dispatched **regardless of
                ``automation_enabled``** (operator intent is honored even
                when scheduled automation is off). ``None`` keeps sunSale's
                scheduled choice.

        Returns:
            Updated ``InverterModeHistory``. The coordinator persists this
            back to the rolling-history store.
        """
        updated_history = self._record_observation(now, reading, history)
        self._last_dispatch_at = now
        self._last_automation_enabled = automation_enabled

        # Dispatch when scheduled automation is on OR an explicit operator
        # override is set. The override bypasses ``automation_enabled`` by
        # design — selecting a mode in the UI is an operator command and must
        # reach the inverter even when scheduled writes are paused.
        if not automation_enabled and mode_override is None:
            self._last_dispatch_outcome = "automation_disabled"
            self._last_dispatch_target = None
            return updated_history

        outcome, target = await self._dispatch_current_slot(
            now, schedule, mode_override
        )
        self._last_dispatch_outcome = outcome
        self._last_dispatch_target = target
        return updated_history

    @property
    def last_dispatch_outcome(self) -> str | None:
        """Return the outcome label of the most recent ``tick``.

        One of ``ok`` (write attempted), ``no_target`` (no override + no
        schedule slot covering now), ``no_spec`` (target mode lacks a
        register spec), ``automation_disabled`` (no override and automation
        off — observer-only), or ``None`` before the first tick.
        """
        return self._last_dispatch_outcome

    @property
    def last_dispatch_target(self) -> StorageMode | None:
        """Return the StorageMode the most recent ``tick`` attempted to dispatch.

        Set even when the dispatch was blocked (e.g. by disabled
        automation), so the diagnostic sensor can show "would-have-been
        dispatched" alongside the outcome.
        """
        return self._last_dispatch_target

    @property
    def last_dispatch_at(self) -> datetime | None:
        """Return the timestamp of the most recent ``tick`` invocation."""
        return self._last_dispatch_at

    @property
    def automation_enabled_at_last_dispatch(self) -> bool | None:
        """Return the ``automation_enabled`` value observed in the latest tick."""
        return self._last_automation_enabled

    @property
    def last_commanded_mode(self) -> StorageMode | None:
        """Return the mode most recently force-written to the inverter.

        This is our own truth (set when ``_dispatch_current_slot`` detects a
        commanded change), independent of the solis_modbus state cache. The
        verify loop checks the inverter's actual register against this.
        """
        return self._last_commanded_mode

    @property
    def last_commanded_at(self) -> datetime | None:
        """Return the timestamp of the most recent commanded-mode change."""
        return self._last_commanded_at

    @property
    def verify_state(self) -> str | None:
        """Return the current verify state.

        One of ``pending`` (commanded change issued, verify hasn't run yet
        or is mid-retry), ``ok`` (verify saw the inverter at the commanded
        register value), ``mismatch`` (still wrong after one retry — the
        write isn't taking; check the inverter / Modbus chain), or ``None``
        before any command has been issued this run.
        """
        return self._verify_state

    @property
    def last_verify_at(self) -> datetime | None:
        """Return the timestamp of the most recent verify-tick reading."""
        return self._last_verify_at

    @property
    def last_verify_observed_reg(self) -> int | None:
        """Return the register 43110 value the most recent verify-tick read."""
        return self._last_verify_observed_reg

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
    ) -> tuple[str, StorageMode | None]:
        """Apply the current-slot target mode (or the override) to the inverter.

        Args:
            now: Cycle timestamp.
            schedule: Latest Schedule, or ``None``.
            mode_override: When set, dispatched directly and the schedule slot
                is ignored. ``None`` falls back to the slot covering ``now``.

        Returns:
            ``(outcome, target)`` — outcome is one of ``ok``, ``no_target``,
            ``no_spec``; target is the StorageMode the dispatcher resolved
            to (``None`` only when ``outcome == no_target``).
        """
        source = "override" if mode_override is not None else "schedule"
        if mode_override is not None:
            target: StorageMode | None = mode_override
        else:
            slot = self._current_slot(now, schedule)
            target = slot.mode if slot is not None else None
        if target is None:
            _LOGGER.debug(
                "inverter_control: no target this cycle (source=%s)", source,
            )
            return ("no_target", None)
        spec = self._specs.get(target)
        if spec is None:
            _LOGGER.warning(
                "inverter_control: no spec for target mode %s (source=%s) — "
                "skipping dispatch",
                target.value, source,
            )
            return ("no_spec", target)
        # A commanded-mode change is the trigger for force-write + verify:
        # the solis_modbus state cache may lag a successful write by up to a
        # poll interval, so trusting per-register idempotency on the very
        # cycle that issues a new command risks silently dropping the write.
        commanded_changed = target != self._last_commanded_mode
        await self._inverter.apply_mode(target, spec, force=commanded_changed)
        if commanded_changed:
            self._last_commanded_mode = target
            self._last_commanded_at = now
            self._verify_state = "pending"
            self._last_verify_at = None
            self._last_verify_observed_reg = None
            self._verify_retried = False
            self._verify_window_started_at = now
            _LOGGER.info(
                "inverter_control: dispatched %s (source=%s, force-write, "
                "first verify in %ds, polling every %ds up to %ds)",
                target.value, source,
                _VERIFY_INITIAL_DELAY_S,
                _VERIFY_POLL_INTERVAL_S,
                _VERIFY_WINDOW_S,
            )
            self._last_applied_mode = target
            self._schedule_verify(_VERIFY_INITIAL_DELAY_S)
        else:
            _LOGGER.debug(
                "inverter_control: re-dispatched %s (source=%s, no commanded "
                "change)",
                target.value, source,
            )
        return ("ok", target)

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

    # ------------------------------------------------------------------ #
    # Verify loop                                                          #
    # ------------------------------------------------------------------ #

    async def force_verify_now(self) -> None:
        """Run a verify cycle immediately, bypassing the +30 s wait.

        Public entry point for the ``sun_sale.force_verify_inverter_mode``
        service. Cancels any pending verify, then re-runs the same logic
        the scheduled callback would: re-read register 43110, compare to
        ``last_commanded_mode``, log + flip ``verify_state`` accordingly,
        and trigger a single retry on first mismatch (matching the regular
        verify-loop semantics).
        """
        if self._verify_cancel is not None:
            try:
                self._verify_cancel()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "inverter_control: force_verify_now cancel raised — ignoring",
                    exc_info=True,
                )
            self._verify_cancel = None
        if self._last_commanded_mode is None:
            _LOGGER.info(
                "inverter_control: force_verify_now — no commanded mode yet, "
                "nothing to verify",
            )
            return
        # Use real wall-clock time for elapsed calculations — the operator
        # explicitly asked for a "now" check.
        await self._on_verify_tick(datetime.now(self._local_tz))

    def _schedule_verify(self, delay_s: int) -> None:
        """Schedule a single verify-tick after ``delay_s`` seconds.

        Cancels any previously-scheduled verify so an in-flight commanded
        change always supersedes a stale one. When ``hass`` is ``None``
        (unit-test wiring) the call is a no-op and the verify loop is
        effectively disabled — tests that need to exercise it inject a
        mock that captures the scheduled callback for manual firing.

        Args:
            delay_s: Wall-clock delay until the verify callback fires.
        """
        if self._verify_cancel is not None:
            try:
                self._verify_cancel()
            except Exception:  # noqa: BLE001 — cancel must never raise
                _LOGGER.debug(
                    "inverter_control: prior verify-cancel raised — ignoring",
                    exc_info=True,
                )
            self._verify_cancel = None
        if self._hass is None:
            return
        self._verify_cancel = async_call_later(
            self._hass, delay_s, self._on_verify_tick,
        )

    async def _on_verify_tick(self, now: datetime) -> None:
        """Read the inverter back and decide whether commanded was applied.

        Fired by ``async_call_later`` (or ``force_verify_now``). On match,
        ``verify_state`` flips to ``ok`` and the loop stops. On mismatch,
        the elapsed time since the current window started decides:

          * Within window → schedule the next poll at +``POLL_INTERVAL`` s.
          * Window exhausted, not yet retried → re-issue the force-write,
            reset the window, and resume polling.
          * Window exhausted, already retried → ``mismatch``, log error,
            stop. Manual operator intervention required.

        The check is intentionally narrow — it compares register 43110 to
        the spec's ``reg_43110_value``, the single bitmask that defines
        mode membership. Ancillary values (currents, RC setpoint, export
        cap) are left to the next dispatch cycle's natural idempotency.

        Args:
            now: Tick time supplied by HA's ``async_call_later`` (or by
                ``force_verify_now``). Used for both the verify timestamp
                and the elapsed-time computation so tests can drive the
                loop deterministically.
        """
        self._verify_cancel = None
        commanded = self._last_commanded_mode
        if commanded is None:
            return  # commanded was cleared since the verify was scheduled
        spec = self._specs.get(commanded)
        if spec is None:
            # Shouldn't happen — _dispatch_current_slot already guarded —
            # but if a future StorageMode lacks a spec, fail closed.
            self._verify_state = "mismatch"
            return

        observed_reg = self._inverter.get_storage_control_word()
        self._last_verify_at = now
        self._last_verify_observed_reg = observed_reg

        if observed_reg == spec.reg_43110_value:
            self._verify_state = "ok"
            _LOGGER.info(
                "inverter_control: verify OK — commanded=%s reg=0x%x",
                commanded.value, observed_reg,
            )
            return

        observed_str = (
            f"0x{observed_reg:x}" if observed_reg is not None else "None"
        )
        window_start = self._verify_window_started_at or now
        elapsed = (now - window_start).total_seconds()

        if elapsed < _VERIFY_WINDOW_S:
            # Still within the current window — keep polling. Log at DEBUG
            # so a normal pending→ok transition stays quiet.
            _LOGGER.debug(
                "inverter_control: verify pending — commanded=%s "
                "target_reg=0x%x observed_reg=%s elapsed=%.0fs; next poll "
                "in %ds",
                commanded.value, spec.reg_43110_value, observed_str,
                elapsed, _VERIFY_POLL_INTERVAL_S,
            )
            self._schedule_verify(_VERIFY_POLL_INTERVAL_S)
            return

        # Window exhausted.
        if not self._verify_retried:
            _LOGGER.warning(
                "inverter_control: verify mismatch after %.0fs — commanded=%s "
                "target_reg=0x%x observed_reg=%s; re-issuing force-write",
                elapsed, commanded.value, spec.reg_43110_value, observed_str,
            )
            self._verify_retried = True
            await self._inverter.apply_mode(commanded, spec, force=True)
            self._verify_window_started_at = now  # fresh window for the retry
            self._schedule_verify(_VERIFY_INITIAL_DELAY_S)
            # verify_state stays "pending" — the retry is still in flight
            return

        self._verify_state = "mismatch"
        _LOGGER.error(
            "inverter_control: verify mismatch persists after retry "
            "(%.0fs elapsed in retry window) — commanded=%s "
            "target_reg=0x%x observed_reg=%s. The write is not taking "
            "effect; check the Modbus chain and inverter.",
            elapsed, commanded.value, spec.reg_43110_value, observed_str,
        )
