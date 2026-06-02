"""Auto-resolve solis_modbus entity IDs from the HA entity registry.

Roles required for the StorageMode state machine (see ``docs/solis_control.md``):

  - Telemetry sensors: SoC, battery power, grid power.
  - Register 43110 readback sensor (Storage Control word).
  - Number entities for battery currents, export limits, RC active-power setpoint.
  - Bit-switches for register 43110 bits 0/1/5/6 + the master export gate
    and the grid feed-in limit switch (regs outside 43110).

TOU slot-time entities (regs 43143/43145/43147/43149) are no longer
discovered — the new state machine does not write per-cycle TOU slots.
"""
import logging
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

# Unique-ID suffixes for sensors / numbers: ``solis_modbus_{serial}_{suffix}``.
_SENSOR_SUFFIXES: dict[str, str] = {
    # Telemetry — read every cycle.
    "battery_soc":                "solis_modbus_inverter_battery_soc",
    "battery_power":              "solis_modbus_inverter_battery_power",
    # Net grid flow measured at the external CT meter — represents what
    # actually crosses the grid connection (positive = inverter→grid in the
    # solis_modbus convention; sunSale flips this to its positive=import
    # contract inside GridObserver / InverterController). The AC port power
    # would mix in self-consumed energy and is not what we want.
    "grid_power":                 "solis_modbus_inverter_meter_total_active_power",
    # Fallback for CT-only installs where the Modbus-meter register is empty
    # — the inverter AC port reading is still populated and shares the same
    # sign convention, so the boundary sign-flip applies unchanged.
    "grid_power_fallback":        "solis_modbus_inverter_ac_grid_port_power",
    # Daily-resetting energy counters used as authoritative totals for the
    # ObservedGridSeries end-of-day correction (see inbound/grid.py).
    "grid_import_energy_today":   "solis_modbus_inverter_energy_imported_today",
    "grid_export_energy_today":   "solis_modbus_inverter_energy_exported_today",
    # Storage Control word readback (register 43110).
    "storage_control_readback":   "solis_modbus_inverter_storage_control_switch_value",
    # Battery max charge / discharge currents (per-slot; configured via numbers).
    "battery_max_charge_current":    "solis_modbus_inverter_battery_max_charge_current",
    "battery_max_discharge_current": "solis_modbus_inverter_battery_max_discharge_current",
    # Remote-Control AC active-power setpoint (register 43141).
    "rc_setpoint":                "solis_modbus_inverter_rc_inverter_ac_grid_active_power",
    # Export-side numbers (regs 43073/43074 area).
    "backflow_power":             "solis_modbus_inverter_backflow_power",
    "peak_max_usable_grid_power": "solis_modbus_inverter_peak_max_usable_grid_power",
}

# Bit switches at register 43110: ``solis_modbus_{serial}_43110_{bit}``.
# Mapping table — see ``docs/solis_control.md`` §2 for the bit assignments.
_REG_43110 = 43110
_BIT_SWITCHES: dict[str, int] = {
    "self_use_switch":         0,
    "tou_mode_switch":         1,
    "allow_grid_charge_switch": 5,
    "feed_in_priority_switch": 6,
}

# Switches that participate in dispatch but are not bits of 43110.
# Their unique-id suffix follows the same ``solis_modbus_{serial}_{suffix}``
# pattern as sensors / numbers — only the slug differs.
_SWITCH_SUFFIXES: dict[str, str] = {
    "allow_export_under_self_use_switch":
        "solis_modbus_inverter_allow_export_switch_under_self_generation_and_self_use",
    "grid_feed_in_power_limit_switch":
        "solis_modbus_inverter_grid_feed_in_power_limit_switch",
}


def resolve_solis_entities(hass: HomeAssistant, config_entry_id: str) -> dict[str, str]:
    """Resolve entity IDs for a solis_modbus config entry via the entity registry.

    Matches each required role to its entity_id by inspecting the unique_id
    registered against the given config entry. Unique-ID patterns follow the
    solis_modbus convention:
      - sensors / numbers / non-43110 switches: ``solis_modbus_{serial}_{slug}``
      - register-43110 bit switches:            ``solis_modbus_{serial}_43110_{bit}``

    Args:
        hass: Home Assistant instance.
        config_entry_id: The solis_modbus config entry ID whose entities to scan.

    Returns:
        Dict mapping role keys to resolved entity IDs. Roles not found are
        omitted; the caller falls back to the manual-mapping config-flow form.
    """
    registry = er.async_get(hass)
    result: dict[str, str] = {}

    for entry in er.async_entries_for_config_entry(registry, config_entry_id):
        uid = entry.unique_id or ""
        for role, suffix in _SENSOR_SUFFIXES.items():
            if uid.endswith(f"_{suffix}") or uid.endswith(f"_{suffix}_2"):
                result[role] = entry.entity_id
        for role, suffix in _SWITCH_SUFFIXES.items():
            if uid.endswith(f"_{suffix}") or uid.endswith(f"_{suffix}_2"):
                result[role] = entry.entity_id
        for role, bit in _BIT_SWITCHES.items():
            if uid.endswith(f"_{_REG_43110}_{bit}"):
                result[role] = entry.entity_id

    all_roles = (
        set(_SENSOR_SUFFIXES) | set(_SWITCH_SUFFIXES) | set(_BIT_SWITCHES)
    )
    missing = sorted(all_roles - result.keys())
    if missing:
        _LOGGER.warning("solis_modbus auto-discovery: missing roles: %s", missing)

    return result
