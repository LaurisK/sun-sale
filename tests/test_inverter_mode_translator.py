"""Tests for the InverterModeTranslator."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.sun_sale.contract.models import InverterModeReading, StorageMode
from custom_components.sun_sale.inbound.inverter_mode import InverterModeTranslator


NOW = datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc)


def _make_inverter(
    *,
    reg: int | None,
    charge_a: float | None,
    discharge_a: float | None,
    rc_w: int | None,
) -> MagicMock:
    """Build a stub InverterController exposing only the read-side helpers."""
    inv = MagicMock()
    inv.get_storage_control_word.return_value = reg
    inv.get_charge_current_a.return_value = charge_a
    inv.get_discharge_current_a.return_value = discharge_a
    inv.get_rc_setpoint_w.return_value = rc_w
    return inv


def test_parse_decodes_gulp_when_register_is_33():
    inv = _make_inverter(reg=33, charge_a=50.0, discharge_a=0.0, rc_w=-3000)
    reading = InverterModeTranslator(inv).parse(None, NOW)
    assert reading == InverterModeReading(
        timestamp=NOW,
        reg_43110_value=33,
        mode=StorageMode.GULP,
        charge_a=50.0,
        discharge_a=0.0,
        rc_setpoint_w=-3000,
    )


def test_parse_decodes_dump_when_discharging():
    inv = _make_inverter(reg=64, charge_a=0.0, discharge_a=50.0, rc_w=5000)
    reading = InverterModeTranslator(inv).parse(None, NOW)
    assert reading.mode == StorageMode.DUMP


def test_parse_decodes_sell_when_feed_in_with_no_discharge():
    inv = _make_inverter(reg=64, charge_a=0.0, discharge_a=0.0, rc_w=0)
    reading = InverterModeTranslator(inv).parse(None, NOW)
    assert reading.mode == StorageMode.SELL


def test_parse_decodes_stby_when_self_use_with_zero_currents():
    inv = _make_inverter(reg=1, charge_a=0.0, discharge_a=0.0, rc_w=0)
    reading = InverterModeTranslator(inv).parse(None, NOW)
    assert reading.mode == StorageMode.STBY


def test_parse_returns_unknown_when_register_unavailable():
    inv = _make_inverter(reg=None, charge_a=None, discharge_a=None, rc_w=None)
    reading = InverterModeTranslator(inv).parse(None, NOW)
    assert reading.mode == StorageMode.UNKNOWN
    assert reading.reg_43110_value is None


def test_parse_preserves_timestamp():
    inv = _make_inverter(reg=1, charge_a=0.0, discharge_a=0.0, rc_w=0)
    other = datetime(2026, 5, 30, 12, 34, tzinfo=timezone.utc)
    reading = InverterModeTranslator(inv).parse(None, other)
    assert reading.timestamp == other


@pytest.mark.asyncio
async def test_translate_async_wraps_parse():
    inv = _make_inverter(reg=33, charge_a=10.0, discharge_a=0.0, rc_w=-1000)
    result = await InverterModeTranslator(inv).translate(
        hass=None, config=None, raw_config={}, now=NOW,
    )
    assert result.mode == StorageMode.GULP
    assert result.timestamp == NOW
