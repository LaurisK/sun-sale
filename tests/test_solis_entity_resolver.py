"""Tests for inbound.solis_entity_resolver — RC-role entity matching."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.sun_sale.inbound import solis_entity_resolver as r


class _Entry:
    """Minimal entity-registry entry stub."""

    def __init__(self, unique_id: str, entity_id: str) -> None:
        self.unique_id = unique_id
        self.entity_id = entity_id


@pytest.fixture
def registry_scan(monkeypatch):
    """Patch the registry helpers; returns a setter for the scanned entries."""
    entries: list[_Entry] = []
    monkeypatch.setattr(r.er, "async_get", lambda hass: MagicMock())
    monkeypatch.setattr(
        r.er, "async_entries_for_config_entry", lambda reg, eid: list(entries),
    )
    return entries


def test_rc_select_resolved_by_register_unique_id(registry_scan):
    registry_scan.append(
        _Entry("solis_modbus_SN123_43132_select", "select.rc_grid_adjustment"),
    )
    result = r.resolve_solis_entities(MagicMock(), "entry")
    assert result["rc_grid_adjustment_select"] == "select.rc_grid_adjustment"


def test_rc_select_not_stolen_by_hidden_readback_sensor(registry_scan):
    # The 43132 readback sensor shares the entity_id tail — the domain guard
    # must keep it from claiming the select role.
    registry_scan.extend([
        _Entry(
            "solis_modbus_SN123_solis_modbus_inverter_rc_grid_adjustment",
            "sensor.namai_inv_rc_grid_adjustment",
        ),
        _Entry("solis_modbus_SN123_43132_select", "select.rc_grid_adjustment"),
    ])
    result = r.resolve_solis_entities(MagicMock(), "entry")
    assert result["rc_grid_adjustment_select"] == "select.rc_grid_adjustment"


def test_rc_timeout_prefers_number_over_sensor(registry_scan):
    # Both domains exist for the editable 43282 register; the writable role
    # must resolve to the number entity (second pass override).
    registry_scan.extend([
        _Entry(
            "solis_modbus_SN123_solis_modbus_inverter_rc_timeout",
            "sensor.namai_inv_rc_timeout_2",
        ),
        _Entry(
            "solis_modbus_SN123_solis_modbus_inverter_rc_timeout_number",
            "number.namai_inv_rc_timeout_2",
        ),
    ])
    result = r.resolve_solis_entities(MagicMock(), "entry")
    assert result["rc_timeout"] == "number.namai_inv_rc_timeout_2"


def test_missing_rc_roles_are_optional(registry_scan, caplog):
    # Old solis_modbus versions don't expose the RC entities at all — their
    # absence must not be reported as missing *required* roles.
    result = r.resolve_solis_entities(MagicMock(), "entry")
    assert "rc_grid_adjustment_select" not in result
    assert "rc_timeout" not in result
    for record in caplog.records:
        if record.levelname == "WARNING":
            assert "rc_grid_adjustment_select" not in record.getMessage()
            assert "rc_timeout" not in record.getMessage()
