"""Storage-mode register specs and planner → StorageMode mapping.

The Solis hybrid inverter exposes a small set of register groups (see
``docs/solis_control.md``). Each high-level StorageMode is a *composition* of
target values across (43110 bitmask, export limit, charge current, discharge
current, RC active-power setpoint). This module owns:

  - ``PlannerDecision``   — internal planner-side per-slot decision enum.
    Not part of the public contract; lives in the pipeline because it
    represents the optimizer's view of a slot, not the inverter's state.
  - ``build_specs(...)``  — concrete StorageModeSpec for each StorageMode given
    the deployment's battery and inverter limits.
  - ``decode_mode(...)``  — best-effort decode of an observed register state to
    a StorageMode, used by the inbound translator.
  - ``select_mode(...)``  — map a planner-side (PlannerDecision,
    ChargingProfile, sell-price) tuple to the StorageMode the inverter
    should enter.

Pure Python: no Home Assistant imports.
"""
from __future__ import annotations

from enum import Enum

from ..contract.models import (
    BatteryConfig,
    ChargeMode,
    StorageMode,
    StorageModeSpec,
)


class PlannerDecision(Enum):
    """Internal planner-side decision for a single scheduling slot.

    Produced by ``pipeline/schedule.py``'s greedy pair-match optimizer and
    bridged to a Solis StorageMode by ``select_mode()``. Intentionally not
    exposed via ``contract/models.py`` — callers outside the pipeline should
    consume ``ScheduleSlot.mode`` (a StorageMode) instead.
    """
    IDLE              = "idle"
    CHARGE_FROM_GRID  = "charge_from_grid"
    DISCHARGE_TO_GRID = "discharge_to_grid"
    CHARGE_FROM_SOLAR = "charge_from_solar"


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
        export_max_w: Backflow / export power cap for SELL and STORE modes.
        inverter_max_power_w: Rated AC output, used as the RC setpoint magnitude
            for DUMP (force discharge to grid).

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
        StorageMode.SELL: StorageModeSpec(
            reg_43110_value=64,
            export_limit_w=export_max_w,
            charge_a=i_charge_max_a,
            discharge_a=0.0,
            rc_setpoint_w=0,
        ),
        StorageMode.STORE: StorageModeSpec(
            reg_43110_value=1,
            export_limit_w=export_max_w,
            charge_a=i_charge_max_a,
            discharge_a=0.0,
            rc_setpoint_w=0,
        ),
        StorageMode.HOARD: StorageModeSpec(
            reg_43110_value=1,
            export_limit_w=0,
            charge_a=i_charge_max_a,
            discharge_a=0.0,
            rc_setpoint_w=0,
        ),
        StorageMode.DUMP: StorageModeSpec(
            reg_43110_value=64,
            export_limit_w=None,    # uncapped while DUMPing
            charge_a=0.0,
            discharge_a=i_discharge_max_a,
            rc_setpoint_w=+inverter_max_power_w,
        ),
        StorageMode.GULP: StorageModeSpec(
            reg_43110_value=33,
            export_limit_w=0,
            charge_a=i_charge_max_a,
            discharge_a=0.0,
            rc_setpoint_w=-p_charge_max_w,
        ),
        StorageMode.STBY: StorageModeSpec(
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
      * 43110=1 (SelfUse) covers AUTO, STBY, STORE, and HOARD.
      * 43110=64 (FeedIn) covers SELL and DUMP.
    Ancillary signals (discharge current, charge current) disambiguate where
    possible. HOARD vs STORE and AUTO vs the hardware-default cases collapse
    to STORE / STBY respectively because the differentiating fields
    (export-limit, allow-export-switch) are not consulted here. The control
    module's *target* mode is the authoritative answer; this decoder produces
    the *observed* label that drives the chart history.

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
        return StorageMode.GULP
    if reg_43110_value == 64:
        return StorageMode.DUMP if d > _CURRENT_EPSILON_A else StorageMode.SELL
    if reg_43110_value == 1:
        if c < _CURRENT_EPSILON_A and d < _CURRENT_EPSILON_A:
            return StorageMode.STBY
        return StorageMode.STORE
    return StorageMode.UNKNOWN


def select_mode(
    decision: PlannerDecision,
    charging_profile_mode: ChargeMode | None = None,
    sell_eur_kwh: float | None = None,
) -> StorageMode:
    """Map a per-slot PlannerDecision (plus charging-profile context) to a StorageMode.

    Mapping table:

      ============================  ====================  ============
      PlannerDecision               ChargingProfile mode  StorageMode
      ============================  ====================  ============
      CHARGE_FROM_GRID              (any)                 GULP
      DISCHARGE_TO_GRID             (any)                 DUMP
      CHARGE_FROM_SOLAR             NO_EXPORT             HOARD
      CHARGE_FROM_SOLAR             SELL / SOLAR_CHARGE / IDLE / None  STORE
      IDLE                          sell_eur_kwh < 0      STBY
      IDLE                          otherwise             AUTO
      ============================  ====================  ============

    Args:
        decision: Planner's per-slot decision.
        charging_profile_mode: Per-slot disposition from ChargingProfileNode;
            only consulted for ``CHARGE_FROM_SOLAR`` to choose STORE vs HOARD.
        sell_eur_kwh: Effective sell price for the slot; used only when
            ``decision == IDLE`` to decide between AUTO and STBY (the latter
            avoids running the inverter while the grid would penalise export).

    Returns:
        StorageMode the inverter should enter for this slot.
    """
    if decision == PlannerDecision.CHARGE_FROM_GRID:
        return StorageMode.GULP
    if decision == PlannerDecision.DISCHARGE_TO_GRID:
        return StorageMode.DUMP
    if decision == PlannerDecision.CHARGE_FROM_SOLAR:
        if charging_profile_mode == ChargeMode.NO_EXPORT:
            return StorageMode.HOARD
        return StorageMode.STORE
    if sell_eur_kwh is not None and sell_eur_kwh < 0:
        return StorageMode.STBY
    return StorageMode.AUTO
