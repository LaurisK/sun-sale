"""Auto-resolve solis_modbus entity IDs from the HA entity registry."""
import logging
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

# Unique-ID suffixes for sensors/numbers: solis_modbus_{serial}_{suffix}
_SENSOR_SUFFIXES: dict[str, str] = {
    "battery_soc": "solis_modbus_inverter_battery_soc",
    "battery_power": "solis_modbus_inverter_battery_power",
    "grid_power": "solis_modbus_inverter_ac_grid_port_power",
    "solis_charge_current": "solis_modbus_inverter_time_charging_charge_current",
    "solis_discharge_current": "solis_modbus_inverter_time_charging_discharge_current",
}

# Register numbers for time entities: solis_modbus_{serial}_{register}
_TIME_REGISTERS: dict[str, int] = {
    "solis_charge_start_time_1": 43143,
    "solis_charge_end_time_1": 43145,
    "solis_discharge_start_time_1": 43147,
    "solis_discharge_end_time_1": 43149,
}

# Switches: solis_modbus_{serial}_{register}_{bit_position}
_SWITCH_REGISTER = 43110
_SWITCH_BITS: dict[str, int] = {
    "solis_self_use_mode_switch": 0,
    "solis_tou_mode_switch": 1,
    "solis_allow_grid_charge_switch": 5,
}


def resolve_solis_entities(hass: HomeAssistant, config_entry_id: str) -> dict[str, str]:
    """Resolve entity IDs for a solis_modbus config entry via the entity registry.

    Matches each required role to its entity_id by inspecting the unique_id
    registered against the given config entry. Unique-ID patterns follow the
    solis_modbus convention:
      - sensors/numbers: solis_modbus_{serial}_{entity_suffix}
      - time entities:   solis_modbus_{serial}_{register}
      - switches:        solis_modbus_{serial}_{register}_{bit_position}

    Args:
        hass: Home Assistant instance.
        config_entry_id: The solis_modbus config entry ID whose entities to scan.

    Returns:
        Dict mapping role keys to resolved entity IDs. Roles not found are omitted.
    """
    registry = er.async_get(hass)
    result: dict[str, str] = {}

    for entry in er.async_entries_for_config_entry(registry, config_entry_id):
        uid = entry.unique_id or ""
        for role, suffix in _SENSOR_SUFFIXES.items():
            if uid.endswith(f"_{suffix}"):
                result[role] = entry.entity_id
        for role, register in _TIME_REGISTERS.items():
            if uid.endswith(f"_{register}"):
                result[role] = entry.entity_id
        for role, bit in _SWITCH_BITS.items():
            if uid.endswith(f"_{_SWITCH_REGISTER}_{bit}"):
                result[role] = entry.entity_id

    all_roles = set(_SENSOR_SUFFIXES) | set(_TIME_REGISTERS) | set(_SWITCH_BITS)
    missing = sorted(all_roles - result.keys())
    if missing:
        _LOGGER.warning("solis_modbus auto-discovery: missing roles: %s", missing)

    return result
