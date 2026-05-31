"""Tests for pipeline/storage_mode_specs.py — pure Python, no HA required."""
from __future__ import annotations

import pytest

from custom_components.sun_sale.contract.models import StorageMode
from custom_components.sun_sale.pipeline.storage_mode_specs import (
    build_specs,
    decode_mode,
)
from tests.conftest import default_battery_config


# ---------------------------------------------------------------------------
# build_specs — concrete register targets
# ---------------------------------------------------------------------------


def _specs():
    """Return the canonical spec table for default test fixtures."""
    return build_specs(
        battery_config=default_battery_config(),
        export_max_w=10_000,
        inverter_max_power_w=10_000,
    )


def test_build_specs_covers_every_applied_mode():
    # UNKNOWN is observed-only and intentionally excluded.
    specs = _specs()
    applied = {m for m in StorageMode if m != StorageMode.UNKNOWN}
    assert set(specs.keys()) == applied


@pytest.mark.parametrize(
    "mode, expected_reg",
    [
        (StorageMode.SELL, 64),
        (StorageMode.STORE, 1),
        (StorageMode.HOARD, 1),
        (StorageMode.DUMP, 64),
        (StorageMode.GULP, 33),
        (StorageMode.STBY, 1),
        (StorageMode.AUTO, 1),
        (StorageMode.TRACK, 1),
    ],
)
def test_build_specs_register_bitmasks_match_doc(mode, expected_reg):
    # Bitmasks come straight from docs/solis_control.md §3.
    assert _specs()[mode].reg_43110_value == expected_reg


def test_gulp_pushes_grid_charge_setpoint_negative():
    # GULP forces grid → battery, so RC active-power setpoint is negative.
    bc = default_battery_config()
    spec = build_specs(bc, export_max_w=10_000, inverter_max_power_w=10_000)[StorageMode.GULP]
    assert spec.rc_setpoint_w == -int(bc.max_charge_power_kw * 1000)
    assert spec.discharge_a == 0.0
    assert spec.charge_a is not None and spec.charge_a > 0


def test_dump_pushes_export_setpoint_positive_and_uncaps_export():
    spec = _specs()[StorageMode.DUMP]
    assert spec.rc_setpoint_w == 10_000
    assert spec.export_limit_w is None
    assert spec.charge_a == 0.0
    assert spec.discharge_a is not None and spec.discharge_a > 0


def test_hoard_zeros_export_limit():
    spec = _specs()[StorageMode.HOARD]
    assert spec.export_limit_w == 0


def test_stby_zeros_both_currents_and_export():
    spec = _specs()[StorageMode.STBY]
    assert spec.charge_a == 0.0
    assert spec.discharge_a == 0.0
    assert spec.export_limit_w == 0
    assert spec.rc_setpoint_w == 0


def test_auto_leaves_hardware_defaults_intact():
    spec = _specs()[StorageMode.AUTO]
    assert spec.charge_a is None
    assert spec.discharge_a is None
    assert spec.export_limit_w is None


def test_amps_derived_from_battery_voltage():
    bc = default_battery_config()
    expected_a = (bc.max_charge_power_kw * 1000) / bc.nominal_voltage_v
    assert _specs()[StorageMode.SELL].charge_a == pytest.approx(expected_a)


def test_build_specs_safe_with_zero_voltage_fallback():
    bc = default_battery_config()
    # Use object.__setattr__ because BatteryConfig is frozen.
    object.__setattr__(bc, "nominal_voltage_v", 0.0)
    specs = build_specs(bc, export_max_w=10_000, inverter_max_power_w=10_000)
    # Falls back to 48 V — should not divide-by-zero.
    assert specs[StorageMode.SELL].charge_a == pytest.approx(
        (bc.max_charge_power_kw * 1000) / 48.0
    )


# ---------------------------------------------------------------------------
# decode_mode — observed register + currents → StorageMode
# ---------------------------------------------------------------------------


def test_decode_none_register_is_unknown():
    assert decode_mode(None, 10.0, 0.0, 0) == StorageMode.UNKNOWN


def test_decode_unrecognised_bitmask_is_unknown():
    # bit 2 (Off-Grid) on its own is not in the planner's vocabulary.
    assert decode_mode(4, 0.0, 0.0, 0) == StorageMode.UNKNOWN


def test_decode_self_use_idle_is_stby():
    assert decode_mode(1, 0.0, 0.0, 0) == StorageMode.STBY


def test_decode_self_use_with_charge_current_is_store():
    assert decode_mode(1, 50.0, 0.0, 0) == StorageMode.STORE


def test_decode_grid_charge_bitmask_is_gulp():
    assert decode_mode(33, 50.0, 0.0, -3000) == StorageMode.GULP


def test_decode_feed_in_no_discharge_is_sell():
    assert decode_mode(64, 50.0, 0.0, 0) == StorageMode.SELL


def test_decode_feed_in_with_discharge_is_dump():
    assert decode_mode(64, 0.0, 50.0, 5000) == StorageMode.DUMP


def test_decode_handles_none_currents_as_zero():
    # Translator may pass None when the number entity is unavailable.
    assert decode_mode(1, None, None, None) == StorageMode.STBY


def test_decode_round_trip_applied_modes_match_build_specs():
    """For every applied mode whose decode is unambiguous, applying its spec to
    the inverter and reading the register back should reproduce the same mode."""
    specs = _specs()
    # AUTO and TRACK collapse to STBY/STORE in the decoder by design — they
    # are not part of the round-trip set (see decode_mode docstring).
    unambiguous = {
        StorageMode.SELL,
        StorageMode.STORE,
        StorageMode.HOARD,    # will round-trip to STORE; tested separately
        StorageMode.DUMP,
        StorageMode.GULP,
        StorageMode.STBY,
    }
    for mode in unambiguous:
        spec = specs[mode]
        decoded = decode_mode(
            spec.reg_43110_value,
            spec.charge_a,
            spec.discharge_a,
            spec.rc_setpoint_w,
        )
        # HOARD is observationally indistinguishable from STORE (both SelfUse
        # with charge current); the decoder collapses them to STORE.
        if mode == StorageMode.HOARD:
            assert decoded == StorageMode.STORE
        else:
            assert decoded == mode, f"{mode} round-trip failed → {decoded}"


