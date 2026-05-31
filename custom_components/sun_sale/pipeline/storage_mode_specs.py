"""Storage-mode register specs and observed-state decoder.

The Solis hybrid inverter exposes a small set of register groups (see
``docs/solis_control.md``). Each high-level StorageMode is a *composition* of
target values across (43110 bitmask, export limit, charge current, discharge
current, RC active-power setpoint). This module owns:

  - ``build_specs(...)``  — concrete StorageModeSpec for each StorageMode given
    the deployment's battery and inverter limits.
  - ``decode_mode(...)``  — best-effort decode of an observed register state to
    a StorageMode, used by the inbound translator.

The pipeline scheduler (``pipeline/schedule.py``) picks StorageMode values
directly via slot physics — there is no separate planner-side decision
type.

Pure Python: no Home Assistant imports.
"""
from __future__ import annotations

from ..contract.models import (
    BatteryConfig,
    StorageMode,
    StorageModeSpec,
)


_CURRENT_EPSILON_A = 1e-3


def _amps_from_kw(power_kw: float, voltage_v: float) -> float:
    """Convert a DC-side power limit to amps at the configured bus voltage.

    Args:
        power_kw: Power in kilowatts (≥0).
        voltage_v: DC bus voltage; falls back to 48 V when ≤0 for safety.

    Returns:
        Equivalent current in amps.
    """
    v = voltage_v if voltage_v > 0 else 48.0
    return (power_kw * 1000.0) / v


def build_specs(
    battery_config: BatteryConfig,
    export_max_w: int,
    inverter_max_power_w: int,
) -> dict[StorageMode, StorageModeSpec]:
    """Return the concrete StorageModeSpec table for this deployment.

    ``UNKNOWN`` is intentionally omitted — it is only ever an observed-state
    label and is never applied to the hardware.

    Args:
        battery_config: Battery limits (used to derive max charge/discharge amps).
        export_max_w: Backflow / export power cap for FeedIn and SelfUse modes.
        inverter_max_power_w: Rated AC output, used as the RC setpoint magnitude
            for Discharge (force discharge to grid).

    Returns:
        Dict mapping each StorageMode to its concrete spec.
    """
    i_charge_max_a = _amps_from_kw(
        battery_config.max_charge_power_kw, battery_config.nominal_voltage_v
    )
    i_discharge_max_a = _amps_from_kw(
        battery_config.max_discharge_power_kw, battery_config.nominal_voltage_v
    )
    p_charge_max_w = int(battery_config.max_charge_power_kw * 1000)

    return {
        StorageMode.FeedIn: StorageModeSpec(
            reg_43110_value=64,
            export_limit_w=export_max_w,
            charge_a=i_charge_max_a,
            discharge_a=0.0,
            rc_setpoint_w=0,
        ),
        StorageMode.SelfUse: StorageModeSpec(
            reg_43110_value=1,
            export_limit_w=export_max_w,
            charge_a=i_charge_max_a,
            discharge_a=0.0,
            rc_setpoint_w=0,
        ),
        StorageMode.NoExport: StorageModeSpec(
            reg_43110_value=1,
            export_limit_w=0,
            charge_a=i_charge_max_a,
            discharge_a=0.0,
            rc_setpoint_w=0,
        ),
        StorageMode.Discharge: StorageModeSpec(
            reg_43110_value=64,
            export_limit_w=None,    # uncapped while Discharging
            charge_a=0.0,
            discharge_a=i_discharge_max_a,
            rc_setpoint_w=+inverter_max_power_w,
        ),
        StorageMode.GridCharge: StorageModeSpec(
            reg_43110_value=33,
            export_limit_w=0,
            charge_a=i_charge_max_a,
            discharge_a=0.0,
            rc_setpoint_w=-p_charge_max_w,
        ),
        StorageMode.StandBy: StorageModeSpec(
            reg_43110_value=1,
            export_limit_w=0,
            charge_a=0.0,
            discharge_a=0.0,
            rc_setpoint_w=0,
        ),
        StorageMode.AUTO: StorageModeSpec(
            reg_43110_value=1,
            export_limit_w=None,    # leave whatever the user / hardware set
            charge_a=None,
            discharge_a=None,
            rc_setpoint_w=0,
        ),
        StorageMode.TRACK: StorageModeSpec(
            reg_43110_value=1,
            export_limit_w=None,
            charge_a=i_charge_max_a,
            discharge_a=i_discharge_max_a,
            rc_setpoint_w=0,        # caller overrides per-tick
        ),
    }


def decode_mode(
    reg_43110_value: int | None,
    charge_a: float | None,
    discharge_a: float | None,
    rc_setpoint_w: int | None,
) -> StorageMode:
    """Best-effort decode of an observed inverter state to a StorageMode.

    The register-bitmask alone is ambiguous in two cases:
      * 43110=1 (SelfUse) covers AUTO, StandBy, SelfUse, and NoExport.
      * 43110=64 (FeedIn) covers FeedIn and Discharge.
    Ancillary signals (discharge current, charge current) disambiguate where
    possible. NoExport vs SelfUse and AUTO vs the hardware-default cases
    collapse to SelfUse / StandBy respectively because the differentiating
    fields (export-limit, allow-export-switch) are not consulted here. The
    control module's *target* mode is the authoritative answer; this decoder
    produces the *observed* label that drives the chart history.

    Args:
        reg_43110_value: Raw readback of the Storage Control word.
        charge_a: Configured charge current; treated as ``0`` when ``None``.
        discharge_a: Configured discharge current; treated as ``0`` when ``None``.
        rc_setpoint_w: Signed RC active-power setpoint (currently unused but
            reserved for TRACK detection).

    Returns:
        Best-fit StorageMode; ``UNKNOWN`` when ``reg_43110_value`` is ``None``
        or the bitmask is not one of the recognised states.
    """
    del rc_setpoint_w  # reserved — TRACK detection deferred to chunk 3

    if reg_43110_value is None:
        return StorageMode.UNKNOWN

    c = charge_a or 0.0
    d = discharge_a or 0.0

    if reg_43110_value == 33:
        return StorageMode.GridCharge
    if reg_43110_value == 64:
        return StorageMode.Discharge if d > _CURRENT_EPSILON_A else StorageMode.FeedIn
    if reg_43110_value == 1:
        if c < _CURRENT_EPSILON_A and d < _CURRENT_EPSILON_A:
            return StorageMode.StandBy
        return StorageMode.SelfUse
    return StorageMode.UNKNOWN


