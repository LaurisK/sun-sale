"""Tests for switch.py — AutomationSwitch entity."""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.sun_sale.switch import AutomationSwitch


def make_switch(automation_enabled: bool = True):
    coord = MagicMock()
    coord.automation_enabled = automation_enabled
    entry = MagicMock()
    entry.entry_id = "test_entry"
    sw = AutomationSwitch(coord, entry)
    return sw, coord


def test_is_on_when_enabled():
    sw, _ = make_switch(automation_enabled=True)
    assert sw.is_on is True


def test_is_off_when_disabled():
    sw, _ = make_switch(automation_enabled=False)
    assert sw.is_on is False


async def test_turn_on_enables_automation():
    sw, coord = make_switch(automation_enabled=False)
    await sw.async_turn_on()
    assert coord.automation_enabled is True


async def test_turn_off_disables_automation():
    sw, coord = make_switch(automation_enabled=True)
    await sw.async_turn_off()
    assert coord.automation_enabled is False


def test_unique_id_uses_entry_id():
    sw, _ = make_switch()
    assert sw._attr_unique_id == "test_entry_enabled"


def test_device_info_contains_domain_identifier():
    sw, _ = make_switch()
    info = sw.device_info
    assert any("sun_sale" in str(ident) for ident in info["identifiers"])
