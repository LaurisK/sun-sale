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

from enum import Enum
from typing import Iterable

from homeassistant.core import HomeAssistant

from ..contract.models import (
    BatteryConfig,
    StorageMode,
    StorageModeSpec,
)


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
        ``rc_setpoint``                    (RC active-power setpoint, W)
        ``backflow_power``                 (export cap, W)

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

    def get_battery_soc(self) -> float:
        """Return battery SoC as 0.0–1.0. Falls back to 0.5 when unavailable."""
        return self._read_float("battery_soc", fallback=0.5, normalize_pct=True)

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

    # ------------------------------------------------------------------ #
    # Write side — apply_mode(StorageMode)                                 #
    # ------------------------------------------------------------------ #

    async def apply_mode(
        self,
        mode: StorageMode,
        spec: StorageModeSpec,
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
        """
        if self._platform != InverterPlatform.SOLIS:
            import logging
            logging.getLogger(__name__).debug(
                "apply_mode(%s) — platform %s has no register-level implementation",
                mode.value, self._platform.value,
            )
            return

        await self._apply_43110_bits(spec.reg_43110_value)
        if spec.charge_a is not None:
            await self._set_number(
                "battery_max_charge_current",
                spec.charge_a,
                tolerance_a=_NUMBER_WRITE_EPSILON,
            )
        if spec.discharge_a is not None:
            await self._set_number(
                "battery_max_discharge_current",
                spec.discharge_a,
                tolerance_a=_NUMBER_WRITE_EPSILON,
            )
        if spec.export_limit_w is not None:
            await self._set_number(
                "backflow_power",
                float(spec.export_limit_w),
                tolerance_a=_NUMBER_WRITE_EPSILON,
            )
        # RC setpoint always written — 0 W is the explicit "no override" value.
        await self._set_number(
            "rc_setpoint",
            float(spec.rc_setpoint_w),
            tolerance_a=_NUMBER_WRITE_EPSILON,
        )

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    async def _apply_43110_bits(self, target_value: int) -> None:
        """Toggle only the bit switches whose desired state differs from the readback.

        Reads the current value of register 43110 via the ``storage_control_readback``
        sensor; for each known bit (0/1/5/6) compares the target's bit to the
        observed bit and turns the corresponding switch on / off when they differ.
        Bits whose role switch is not mapped in ``entity_ids`` are silently skipped.

        Args:
            target_value: Desired bitmask value for register 43110.
        """
        current = self.get_storage_control_word()
        for bit, role in _REG_43110_BIT_ROLES.items():
            target_bit = (target_value >> bit) & 1
            current_bit = ((current >> bit) & 1) if current is not None else None
            if current_bit == target_bit:
                continue
            entity_id = self._entity_ids.get(role, "")
            if not entity_id:
                continue
            service = "turn_on" if target_bit else "turn_off"
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
    ) -> None:
        """Write a number entity only when its readback differs by more than tolerance.

        Args:
            role: Entity-ID map key (e.g. ``battery_max_charge_current``).
            target_value: Desired value to set.
            tolerance_a: Absolute tolerance under which the write is skipped.
        """
        entity_id = self._entity_ids.get(role, "")
        if not entity_id:
            return
        current = self._read_optional_float(role)
        if current is not None and abs(current - target_value) <= tolerance_a:
            return
        await self._hass.services.async_call(
            "number", "set_value",
            {"entity_id": entity_id, "value": target_value},
            blocking=True,
        )

    def _read_float(
        self, key: str, fallback: float, normalize_pct: bool = False
    ) -> float:
        """Read a numeric HA sensor state; return fallback on any failure.

        Args:
            key: Entity-ID map key (e.g. "battery_soc").
            fallback: Value returned when the entity is absent or unparseable.
            normalize_pct: When True, divide values > 1.0 by 100 (% → fraction).

        Returns:
            Float sensor value, or fallback.
        """
        value = self._read_optional_float(key, normalize_pct=normalize_pct)
        return value if value is not None else fallback

    def _read_optional_float(
        self, key: str, normalize_pct: bool = False
    ) -> float | None:
        """Read a numeric HA state; return ``None`` when absent or unparseable.

        Args:
            key: Entity-ID map key.
            normalize_pct: When True, divide values > 1.0 by 100.

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
        if normalize_pct and value > 1.0:
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
