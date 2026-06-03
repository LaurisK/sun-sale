"""Auto-resolve solis_modbus entity IDs from the HA entity registry.

Roles required for the StorageMode state machine (see ``docs/solis_control.md``):

  - Telemetry sensors: SoC, battery power, grid power.
  - Register 43110 readback sensor (Storage Control word).
  - Number entities for battery currents, export limits, RC active-power setpoint.
  - Bit-switches for register 43110 bits 0/1/5/6 + the master export gate
    and the grid feed-in limit switch (regs outside 43110).

TOU slot-time entities (regs 43143/43145/43147/43149) are no longer
discovered — the new state machine does not write per-cycle TOU slots.

Unique-ID matching is *defensive* because the upstream solis_modbus
``SolisSensorGroup`` has a bug: it calls ``unique_id_generator(controller,
entity)`` with the entity *dict* instead of ``entity.get("unique")``, so
regular (non-derived) sensors end up with garbled unique_ids of the form
``solis_modbus_{serial}_{'name': '…', …, 'unique': '<suffix>', …}``. The
matcher therefore tries three patterns in order — ``endswith`` for clean
UIDs (derived sensors), a substring search for the dict-repr UIDs, and
entity_id tail matching as a final safety net — so the resolver works
regardless of which side of the upstream bug an entity falls on.
"""
import logging
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

# Each entry maps a role key to (suffix, entity_id_tail). The suffix is the
# canonical ``solis_modbus_inverter_<slug>`` token written by the integration's
# sensor data files. The entity_id tail is the slugified default object_id used
# when the entity_id wasn't overridden (matches both ``..._<tail>`` and
# ``..._<tail>_2`` to absorb the dup-suffix on second-instance entities).
_SENSOR_SUFFIXES: dict[str, tuple[str, str]] = {
    # Telemetry — read every cycle.
    "battery_soc":                 ("solis_modbus_inverter_battery_soc", "battery_soc"),
    "battery_power":               ("solis_modbus_inverter_battery_power", "battery_power"),
    # Net grid flow — prefer the derived ``Grid Power Net`` sensor (clean UID,
    # already in sunSale's positive=import convention; no sign-flip needed).
    # The matcher handles its clean UID via the endswith pattern.
    "grid_power":                  ("solis_modbus_inverter_grid_power_net", "grid_power_net"),
    # Fallback for installs where the meter chain is empty / stale. AC port
    # power uses the opposite (positive=inverter→grid) sign convention, so
    # the boundary sign-flip is applied to this slot in the read path.
    "grid_power_fallback":         ("solis_modbus_inverter_ac_grid_port_power", "ac_grid_port_power"),
    # Daily-resetting energy counters — authoritative totals for the
    # ObservedGridSeries end-of-day correction (see inbound/grid.py).
    "grid_import_energy_today":    ("solis_modbus_inverter_today_energy_imported_from_grid", "today_energy_imported_from_grid"),
    "grid_export_energy_today":    ("solis_modbus_inverter_today_energy_fed_into_grid", "today_energy_fed_into_grid"),
    # Storage Control word readback (register 43110).
    "storage_control_readback":    ("solis_modbus_inverter_storage_control_switch_value", "storage_control_switch_value"),
    # Battery max charge / discharge currents (per-slot; configured via numbers).
    "battery_max_charge_current":    ("solis_modbus_inverter_battery_max_charge_current", "battery_max_charge_current"),
    "battery_max_discharge_current": ("solis_modbus_inverter_battery_max_discharge_current", "battery_max_discharge_current"),
    # Remote-Control AC active-power setpoint (register 43141).
    "rc_setpoint":                 ("solis_modbus_inverter_rc_inverter_ac_grid_active_power", "rc_inverter_ac_grid_active_power"),
    # Export-side numbers (regs 43073/43074 area).
    "backflow_power":              ("solis_modbus_inverter_backflow_power", "backflow_power"),
    "peak_max_usable_grid_power":  ("solis_modbus_inverter_peak_max_usable_grid_power", "peak_max_usable_grid_power"),
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
# ``(suffix, entity_id_tail)`` — same shape as ``_SENSOR_SUFFIXES``.
_SWITCH_SUFFIXES: dict[str, tuple[str, str]] = {
    "allow_export_under_self_use_switch":
        ("solis_modbus_inverter_allow_export_switch_under_self_generation_and_self_use",
         "allow_export_switch_under_self_generation_and_self_use"),
    "grid_feed_in_power_limit_switch":
        ("solis_modbus_inverter_grid_feed_in_power_limit_switch",
         "grid_feed_in_power_limit_switch"),
}


def _matches(uid: str, entity_id: str, suffix: str, entity_id_tail: str) -> bool:
    """Return True when an entry's identifiers match ``suffix``/``entity_id_tail``.

    Three independent patterns are tried — any one is enough — to absorb the
    upstream solis_modbus bug where regular-sensor unique_ids embed the full
    dict-repr instead of just the ``unique`` slug.

    Patterns:
      * ``uid.endswith(_<suffix>)`` (with optional ``_2``) — clean UIDs as
        emitted by derived sensors.
      * ``"'unique': '<suffix>'"`` substring of UID — the garbled dict-repr
        form for regular sensors. The single-quoted form is what
        ``str(dict)`` produces in CPython.
      * ``entity_id`` ends in ``_<entity_id_tail>`` (with optional ``_2``) —
        last-resort match against the integration's default object_id so the
        resolver still finds an entity if both UID forms ever drift.

    Args:
        uid: Entity registry unique_id (may be empty for orphaned entries).
        entity_id: Home Assistant entity_id (e.g. ``sensor.<slug>``).
        suffix: Canonical ``solis_modbus_inverter_<slug>`` token from the
                integration's sensor data files.
        entity_id_tail: Slugified default object_id suffix (e.g.
                ``meter_total_active_power``) used for the entity_id match.

    Returns:
        True when any pattern matches; False otherwise.
    """
    if uid:
        if uid.endswith(f"_{suffix}") or uid.endswith(f"_{suffix}_2"):
            return True
        if f"'unique': '{suffix}'" in uid:
            return True
    if entity_id and entity_id_tail:
        if entity_id.endswith(f"_{entity_id_tail}") or entity_id.endswith(f"_{entity_id_tail}_2"):
            return True
    return False


def resolve_solis_entities(hass: HomeAssistant, config_entry_id: str) -> dict[str, str]:
    """Resolve entity IDs for a solis_modbus config entry via the entity registry.

    Matches each required role to its entity_id by inspecting both the
    unique_id and the entity_id of every entry attached to the given
    config entry. See module docstring for the matching patterns.

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
        entity_id = entry.entity_id or ""
        for role, (suffix, tail) in _SENSOR_SUFFIXES.items():
            if role in result:
                continue
            if _matches(uid, entity_id, suffix, tail):
                result[role] = entity_id
        for role, (suffix, tail) in _SWITCH_SUFFIXES.items():
            if role in result:
                continue
            if _matches(uid, entity_id, suffix, tail):
                result[role] = entity_id
        for role, bit in _BIT_SWITCHES.items():
            if role in result:
                continue
            if uid.endswith(f"_{_REG_43110}_{bit}"):
                result[role] = entity_id

    all_roles = (
        set(_SENSOR_SUFFIXES) | set(_SWITCH_SUFFIXES) | set(_BIT_SWITCHES)
    )
    missing = sorted(all_roles - result.keys())
    if missing:
        _LOGGER.warning("solis_modbus auto-discovery: missing roles: %s", missing)

    return result
