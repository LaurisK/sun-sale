"""Inverter control abstraction.

This module owns all platform-specific HA service calls that touch the
inverter, plus the read-side helpers used by translators to capture the
inverter's current state.

For Solis (the only fully-supported platform at the moment), the controller
exposes a single primitive — ``apply_mode(mode, spec)`` — that drives the
inverter into the target ``StorageMode`` by writing the bit switches that
compose register 43110 plus the number entities for export limit, charge /
discharge currents, and the Remote-Control active-power setpoint.

Every write is idempotent: the current readback is consulted first, and
the underlying HA service is only invoked when the value differs from the
target. This eliminates flash wear from no-op rewrites and keeps Modbus
traffic to a minimum.

Other platforms (Huawei, SolarEdge, GoodWe, generic) still produce the
state-read telemetry needed by the rest of the pipeline; their
``apply_mode`` is a logged no-op pending platform-specific implementations.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Iterable

from homeassistant.core import HomeAssistant

from ..contract.models import (
    BatteryConfig,
    StorageMode,
    StorageModeSpec,
)

_LOGGER = logging.getLogger(__name__)


class InverterPlatform(Enum):
    """Supported HA inverter integrations.

    Only ``SOLIS`` has a complete register-level write implementation; the
    remaining platforms expose telemetry today and a no-op ``apply_mode`` —
    a platform-specific writer can be added per enum value without touching
    callers.
    """

    HUAWEI_SOLAR = "huawei_solar"
    SOLAREDGE = "solaredge"
    GOODWE = "goodwe"
    SOLIS = "solis_modbus"
    GENERIC = "generic"


_POWER_UNIT_SCALERS: dict[str, float] = {
    "W": 1.0 / 1000.0,
    "mW": 1.0 / 1_000_000.0,
    "kW": 1.0,
    "MW": 1000.0,
}

# Tolerance (amps / watts) below which a number write is treated as a no-op.
_NUMBER_WRITE_EPSILON = 0.5

# Register 43110 bit map — see ``docs/solis_control.md`` §2.
_REG_43110_BIT_ROLES: dict[int, str] = {
    0: "self_use_switch",
    1: "tou_mode_switch",
    5: "allow_grid_charge_switch",
    6: "feed_in_priority_switch",
}

# Remote-control (RC) function — registers 43128 (setpoint) / 43132
# (function selector) / 43282 (deadman timeout). The setpoint only acts
# while the selector is engaged, and the inverter reverts the whole RC
# function when no RC write arrives within the timeout (1..30 min) — see
# docs/solis_control.md §3 caveats and Pho3niX90/solis_modbus#352.
_RC_ADJUSTMENT_OFF = "OFF"
_RC_ADJUSTMENT_AC_PORT = "Inverter AC Grid Port"
_RC_ADJUSTMENT_VALUES: dict[str, int] = {
    "OFF": 0,
    "System Grid Connection Point": 1,
    "Inverter AC Grid Port": 2,
}
# Register value of the engaged selector option — exported for the control
# module's register_status row.
RC_ADJUSTMENT_AC_PORT_VALUE = _RC_ADJUSTMENT_VALUES[_RC_ADJUSTMENT_AC_PORT]
# Deadman window written to 43282 (the entity max). The control module
# refreshes it every coordinator tick while an RC-backed mode is held, so
# the inverter only falls back to its base 43110 mode if sunSale stops
# dispatching for a full window.
RC_TIMEOUT_MINUTES = 30.0


def normalize_power_to_kw(value: float, unit: str) -> float:
    """Rescale a power value to kW based on its ``unit_of_measurement``.

    Treats an empty or unrecognised unit as kW (the canonical internal
    unit), so existing configs that already return kW remain unaffected.

    Args:
        value: Raw sensor value as published by HA.
        unit: HA ``unit_of_measurement`` attribute (e.g. "W", "kW").

    Returns:
        Value expressed in kW.
    """
    scaler = _POWER_UNIT_SCALERS.get(unit.strip())
    if scaler is None:
        return value
    return value * scaler


class InverterController:
    """Platform-aware reader / writer for the configured inverter.

    For Solis the role-keyed ``entity_ids`` dict is populated by
    ``inbound/solis_entity_resolver.py`` or by the manual-mapping form
    in ``config_flow.py``. The required role keys are:

      Telemetry (always required):
        ``battery_soc``, ``battery_power``, ``grid_power``

      Storage Control word (register 43110):
        ``storage_control_readback`` (sensor)
        ``self_use_switch``          (bit 0)
        ``tou_mode_switch``          (bit 1)
        ``allow_grid_charge_switch`` (bit 5)
        ``feed_in_priority_switch``  (bit 6)

      Number entities:
        ``battery_max_charge_current``     (charge amps)
        ``battery_max_discharge_current``  (discharge amps)
        ``rc_setpoint``                    (RC active-power setpoint, W — reg 43128)
        ``rc_timeout``                     (RC deadman timeout, min — reg 43282)
        ``backflow_power``                 (export cap, W)

      Select entities:
        ``rc_grid_adjustment_select``      (RC function selector — reg 43132)

      Other switches:
        ``grid_feed_in_power_limit_switch``     (export-limit enable)
        ``allow_export_under_self_use_switch``  (master export gate)

    Missing entity IDs degrade gracefully — the write is logged as a warning
    and skipped, never raised.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        platform: InverterPlatform,
        entity_ids: dict[str, str],
        battery_config: BatteryConfig | None = None,
    ) -> None:
        """Initialise controller with platform, entity map, and optional battery config.

        Args:
            hass: Home Assistant instance for service calls and state reads.
            platform: Inverter platform enum determining the dispatch path.
            entity_ids: Platform-specific entity-ID map (see class docstring).
            battery_config: Battery parameters used to bound currents.
        """
        self._hass = hass
        self._platform = platform
        self._entity_ids = entity_ids
        self._battery_config = battery_config

    # ------------------------------------------------------------------ #
    # State reads — telemetry (any platform)                              #
    # ------------------------------------------------------------------ #

    def get_battery_soc(self) -> float | None:
        """Return battery SoC as 0.0–1.0, or ``None`` when the sensor is unavailable.

        Unlike battery / grid power — where 0.0 is a benign degraded reading —
        SoC has no safe fabricated default: a guessed value would feed the
        scheduler a fictional battery state and, with automation on, dispatch
        against it. Returning ``None`` lets ``BatteryTranslator`` drop the whole
        ``BatteryReading`` so the pipeline degrades to ``no_target`` rather than
        planning on invented telemetry.

        Returns:
            SoC as a 0.0–1.0 fraction, or ``None`` when the SoC sensor is
            absent, unavailable, or unparseable.
        """
        return self._read_optional_float("battery_soc", normalize_pct=True)

    def get_battery_power(self) -> float:
        """Return battery power in kW (positive = charging). 0.0 when unavailable."""
        return self._read_power_kw("battery_power", fallback=0.0)

    def get_grid_power(self) -> float:
        """Return grid power in kW (positive = importing). 0.0 when unavailable.

        On Solis the primary auto-detected entity is the derived
        ``grid_power_net`` sensor, which already matches sunSale's
        positive=import convention so no sign-flip is applied. When the
        primary is missing / stale (e.g. the Modbus meter chain dropping
        out), the controller falls back to ``ac_grid_port_power`` whose
        positive=inverter→grid convention requires a sign flip.
        """
        primary = self._read_power_kw_optional("grid_power")
        if primary is not None:
            return primary
        fallback = self._read_power_kw_optional("grid_power_fallback")
        if fallback is None:
            return 0.0
        if self._platform == InverterPlatform.SOLIS:
            return -fallback
        return fallback

    # ------------------------------------------------------------------ #
    # State reads — Solis state machine (consumed by InverterModeTranslator) #
    # ------------------------------------------------------------------ #

    def get_storage_control_word(self) -> int | None:
        """Return the current value of register 43110.

        Returns:
            Integer bitmask, or ``None`` when the readback sensor is
            absent / unavailable. The translator decodes this to a
            ``StorageMode`` via ``storage_mode_specs.decode_mode``.
        """
        raw = self._read_optional_float("storage_control_readback")
        return int(raw) if raw is not None else None

    def get_charge_current_a(self) -> float | None:
        """Return the configured battery max charge current in amps."""
        return self._read_optional_float("battery_max_charge_current")

    def get_discharge_current_a(self) -> float | None:
        """Return the configured battery max discharge current in amps."""
        return self._read_optional_float("battery_max_discharge_current")

    def get_rc_setpoint_w(self) -> int | None:
        """Return the Remote-Control AC active-power setpoint in watts."""
        raw = self._read_optional_float("rc_setpoint")
        return int(raw) if raw is not None else None

    def get_backflow_power_w(self) -> int | None:
        """Return the currently configured export (backflow) limit in watts."""
        raw = self._read_optional_float("backflow_power")
        return int(raw) if raw is not None else None

    def get_rc_adjustment_value(self) -> int | None:
        """Return the RC Grid Adjustment selector (register 43132) as its register value.

        Reads the select entity's state and maps the option label to the
        register value (0=OFF, 1=System Grid Connection Point, 2=Inverter AC
        Grid Port). The select holds its option optimistically on write, so
        this readback tracks sunSale's own writes without waiting for the
        next solis_modbus poll.

        Returns:
            Register value, or ``None`` when the select is unmapped,
            unavailable, or in an unrecognised state.
        """
        entity_id = self._entity_ids.get("rc_grid_adjustment_select", "")
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None:
            return None
        return _RC_ADJUSTMENT_VALUES.get(state.state)

    # ------------------------------------------------------------------ #
    # Write side — apply_mode(StorageMode)                                 #
    # ------------------------------------------------------------------ #

    async def apply_mode(
        self,
        mode: StorageMode,
        spec: StorageModeSpec,
        force: bool = False,
    ) -> None:
        """Drive the inverter to the target StorageMode via the minimum set of writes.

        Each write step (bit switches, export limit, currents, RC setpoint)
        consults its current readback first and skips the write when the
        readback already matches the target. This makes consecutive calls
        with the same mode free of side effects.

        On non-Solis platforms this is currently a no-op pending a
        platform-specific implementation; the call is logged so observability
        is not lost.

        Args:
            mode: Target StorageMode (used for logging only — the concrete
                register targets live in ``spec``).
            spec: Concrete register targets for the requested mode.
            force: When ``True``, skip the cached-readback comparison and
                always issue the underlying service call. Used by the
                control module's verify-loop on commanded-mode change and
                on retry-after-mismatch, where trusting the (possibly
                stale) solis_modbus cache would risk hiding a failed write.
        """
        if self._platform != InverterPlatform.SOLIS:
            _LOGGER.debug(
                "apply_mode(%s) — platform %s has no register-level implementation",
                mode.value, self._platform.value,
            )
            return
        _LOGGER.debug(
            "apply_mode(%s, force=%s) — reg_43110=0x%x charge_a=%s "
            "discharge_a=%s export_limit_w=%s rc_setpoint_w=%s",
            mode.value, force,
            spec.reg_43110_value,
            spec.charge_a,
            spec.discharge_a,
            spec.export_limit_w,
            spec.rc_setpoint_w,
        )

        await self._apply_43110_bits(spec.reg_43110_value, force=force)
        if spec.charge_a is not None:
            await self._set_number(
                "battery_max_charge_current",
                spec.charge_a,
                tolerance_a=_NUMBER_WRITE_EPSILON,
                force=force,
            )
        if spec.discharge_a is not None:
            await self._set_number(
                "battery_max_discharge_current",
                spec.discharge_a,
                tolerance_a=_NUMBER_WRITE_EPSILON,
                force=force,
            )
        if spec.export_limit_w is not None:
            await self._set_number(
                "backflow_power",
                float(spec.export_limit_w),
                tolerance_a=_NUMBER_WRITE_EPSILON,
                force=force,
            )
        # RC function (43132 selector / 43282 timeout / 43128 setpoint).
        # Write order matters on RC-backed modes: the inverter ignores the
        # setpoint while the selector is OFF, and the timeout only latches
        # when written *after* the function is enabled — otherwise it falls
        # back to a ~5-min default (Pho3niX90/solis_modbus#352).
        if spec.rc_setpoint_w != 0:
            if not self._entity_ids.get("rc_grid_adjustment_select"):
                _LOGGER.warning(
                    "apply_mode(%s): RC-backed mode but no rc_grid_adjustment "
                    "select is mapped — the RC setpoint will not engage",
                    mode.value,
                )
            await self._set_select(
                "rc_grid_adjustment_select", _RC_ADJUSTMENT_AC_PORT, force=force,
            )
            await self._set_number(
                "rc_timeout",
                RC_TIMEOUT_MINUTES,
                tolerance_a=_NUMBER_WRITE_EPSILON,
                force=force,
            )
            await self._set_number(
                "rc_setpoint",
                float(spec.rc_setpoint_w),
                tolerance_a=_NUMBER_WRITE_EPSILON,
                force=force,
            )
        else:
            # Zero the setpoint while the function is still engaged, then
            # release the selector so a stale setpoint can never act again.
            await self._set_number(
                "rc_setpoint",
                0.0,
                tolerance_a=_NUMBER_WRITE_EPSILON,
                force=force,
            )
            await self._set_select(
                "rc_grid_adjustment_select", _RC_ADJUSTMENT_OFF, force=force,
            )

    async def refresh_rc(self, spec: StorageModeSpec) -> None:
        """Re-assert the RC deadman registers while an RC-backed mode is held.

        The inverter reverts the RC function when no RC write arrives within
        the RC timeout (register 43282, max 30 min), so a held GridCharge /
        Discharge must be refreshed every coordinator tick. The RC registers
        are RAM-only — the per-tick rewrite causes no flash wear. The selector
        (43132) is only re-engaged when its readback shows it dropped, because
        the underlying select entity writes to the wire unconditionally.

        Args:
            spec: Spec of the currently held mode; no-op unless it carries a
                non-zero RC setpoint.
        """
        if self._platform != InverterPlatform.SOLIS:
            return
        if spec.rc_setpoint_w == 0:
            return
        if self.get_rc_adjustment_value() != RC_ADJUSTMENT_AC_PORT_VALUE:
            await self._set_select(
                "rc_grid_adjustment_select", _RC_ADJUSTMENT_AC_PORT, force=True,
            )
        await self._set_number(
            "rc_timeout",
            RC_TIMEOUT_MINUTES,
            tolerance_a=_NUMBER_WRITE_EPSILON,
            force=True,
        )
        await self._set_number(
            "rc_setpoint",
            float(spec.rc_setpoint_w),
            tolerance_a=_NUMBER_WRITE_EPSILON,
            force=True,
        )

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    async def _apply_43110_bits(
        self, target_value: int, force: bool = False,
    ) -> None:
        """Toggle the bit switches whose desired state differs from the readback.

        Reads the current value of register 43110 via the ``storage_control_readback``
        sensor; for each known bit (0/1/5/6) compares the target's bit to the
        observed bit and turns the corresponding switch on / off when they differ.
        Bits whose role switch is not mapped in ``entity_ids`` are silently skipped.

        Args:
            target_value: Desired bitmask value for register 43110.
            force: When ``True``, skip the readback comparison and always
                call the underlying switch service. The solis_modbus state
                cache can lag a failed-or-pending write by up to a poll
                interval, so trusting it on commanded-mode change is unsafe.
        """
        current = self.get_storage_control_word()
        for bit, role in _REG_43110_BIT_ROLES.items():
            target_bit = (target_value >> bit) & 1
            current_bit = ((current >> bit) & 1) if current is not None else None
            entity_id = self._entity_ids.get(role, "")
            if not force and current_bit == target_bit:
                _LOGGER.debug(
                    "inverter: 43110 bit %d (%s) skip — cached_bit=%s "
                    "target_bit=%s cached_reg=%s",
                    bit, role, current_bit, target_bit, current,
                )
                continue
            if not entity_id:
                _LOGGER.debug(
                    "inverter: 43110 bit %d (%s) skip — no entity mapped "
                    "(cached_bit=%s target_bit=%s force=%s)",
                    bit, role, current_bit, target_bit, force,
                )
                continue
            service = "turn_on" if target_bit else "turn_off"
            _LOGGER.debug(
                "inverter: 43110 bit %d (%s) write — cached_bit=%s "
                "target_bit=%s service=switch.%s entity=%s force=%s",
                bit, role, current_bit, target_bit, service, entity_id, force,
            )
            await self._hass.services.async_call(
                "switch", service,
                {"entity_id": entity_id},
                blocking=True,
            )

    async def _set_number(
        self,
        role: str,
        target_value: float,
        tolerance_a: float,
        force: bool = False,
    ) -> None:
        """Write a number entity only when its readback differs by more than tolerance.

        Args:
            role: Entity-ID map key (e.g. ``battery_max_charge_current``).
            target_value: Desired value to set.
            tolerance_a: Absolute tolerance under which the write is skipped.
            force: When ``True``, skip the readback comparison and always
                issue the underlying ``number.set_value`` service call.
        """
        entity_id = self._entity_ids.get(role, "")
        if not entity_id:
            _LOGGER.debug(
                "inverter: number(%s) skip — no entity mapped (target=%g force=%s)",
                role, target_value, force,
            )
            return
        current = self._read_optional_float(role)
        if (
            not force
            and current is not None
            and abs(current - target_value) <= tolerance_a
        ):
            _LOGGER.debug(
                "inverter: number(%s) skip — cached=%g target=%g tol=%g entity=%s",
                role, current, target_value, tolerance_a, entity_id,
            )
            return
        _LOGGER.debug(
            "inverter: number(%s) write — cached=%s target=%g entity=%s force=%s",
            role, current, target_value, entity_id, force,
        )
        await self._hass.services.async_call(
            "number", "set_value",
            {"entity_id": entity_id, "value": target_value},
            blocking=True,
        )

    async def _set_select(
        self,
        role: str,
        option: str,
        force: bool = False,
    ) -> None:
        """Select an option on a select entity only when its state differs.

        Args:
            role: Entity-ID map key (e.g. ``rc_grid_adjustment_select``).
            option: Target option label.
            force: When ``True``, skip the current-state comparison and always
                issue the underlying ``select.select_option`` service call.
        """
        entity_id = self._entity_ids.get(role, "")
        if not entity_id:
            _LOGGER.debug(
                "inverter: select(%s) skip — no entity mapped (target=%s force=%s)",
                role, option, force,
            )
            return
        state = self._hass.states.get(entity_id)
        current = state.state if state is not None else None
        if not force and current == option:
            _LOGGER.debug(
                "inverter: select(%s) skip — current=%s entity=%s",
                role, current, entity_id,
            )
            return
        _LOGGER.debug(
            "inverter: select(%s) write — current=%s target=%s entity=%s force=%s",
            role, current, option, entity_id, force,
        )
        await self._hass.services.async_call(
            "select", "select_option",
            {"entity_id": entity_id, "option": option},
            blocking=True,
        )

    def _read_optional_float(
        self, key: str, normalize_pct: bool = False
    ) -> float | None:
        """Read a numeric HA state; return ``None`` when absent or unparseable.

        Args:
            key: Entity-ID map key.
            normalize_pct: When True, coerce a percentage reading to a 0.0–1.0
                fraction. A sensor carrying a ``%`` unit is divided by 100
                unconditionally; a unit-less sensor falls back to the magnitude
                heuristic (divide only when the value exceeds 1.0).

        Returns:
            Parsed float, or ``None``.
        """
        entity_id = self._entity_ids.get(key, "")
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        if normalize_pct:
            # A "%" sensor is always 0–100, so divide unconditionally: this is
            # the only way to read an exact 1 % correctly — the magnitude
            # heuristic below would mistake it for the fraction 1.0 = 100 %.
            # Unit-less SoC sensors keep the heuristic: a value above 1.0 must
            # be a percentage, anything ≤ 1.0 is taken as an already-normalised
            # fraction (genuinely ambiguous at exactly 1.0 without a unit).
            unit = str(state.attributes.get("unit_of_measurement") or "").strip()
            if unit == "%" or value > 1.0:
                return value / 100.0
        return value

    def _read_power_kw(self, key: str, fallback: float) -> float:
        """Read a HA power sensor and normalise to kW.

        Reads the sensor's ``unit_of_measurement`` attribute and rescales:
        W → /1000, MW → ×1000, kW (or missing/unknown) → as-is. Sensor
        vendors are inconsistent about whether grid/battery power is
        published in W or kW, so the caller cannot assume a unit.

        Args:
            key: Entity-ID map key (e.g. "grid_power", "battery_power").
            fallback: Value returned when the entity is absent or unparseable.

        Returns:
            Sensor value normalised to kW, or fallback.
        """
        entity_id = self._entity_ids.get(key, "")
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return fallback
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return fallback
        unit = str(state.attributes.get("unit_of_measurement") or "").strip()
        return normalize_power_to_kw(value, unit)

    def _read_power_kw_optional(self, key: str) -> float | None:
        """Read a power entity, returning None when unset / unavailable / stale.

        Mirrors ``_read_power_kw`` but distinguishes "no reading" (None) from
        "reading happens to be zero" (0.0). Used by callers that want to chain
        primary → fallback entities without a sentinel value getting in the way.

        Args:
            key: Entity-ID map key (e.g. "grid_power", "grid_power_fallback").

        Returns:
            Sensor value normalised to kW, or None when the entity isn't
            configured / its state isn't a number.
        """
        entity_id = self._entity_ids.get(key, "")
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        unit = str(state.attributes.get("unit_of_measurement") or "").strip()
        return normalize_power_to_kw(value, unit)
