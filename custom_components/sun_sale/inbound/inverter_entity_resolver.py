"""Resolve the full inverter entity-ID mapping for coordinator setup.

This is the platform-aware front door to entity resolution. For Solis it
delegates the per-role discovery to :func:`resolve_solis_entities` (auto-detect
path) or falls back to the manually mapped config entries; for the generic
platform it reads the manual mapping directly. On top of the telemetry/control
``inverter_entity_ids`` dict it also pins the observer-pipeline entity IDs
(grid-power directions, today-total counters, PV/solar energy, AC-port/backup
power) using the same *manual-config-first, Solis-auto-second* merge, and maps
auto-detected yesterday-total entities into ``raw_config`` so
``yesterday_total_resolver`` can find them.

Keeping this maze out of the coordinator leaves ``async_setup`` reading as the
thin wiring step it is meant to be, and puts entity resolution next to its
sibling :mod:`solis_entity_resolver`.
"""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.core import HomeAssistant

from ..contract.const import (
    CONF_INVERTER_ENTITY_AC_PORT_POWER,
    CONF_INVERTER_ENTITY_BACKUP_POWER,
    CONF_INVERTER_ENTITY_BATTERY_POWER,
    CONF_INVERTER_ENTITY_BATTERY_SOC,
    CONF_INVERTER_ENTITY_CHARGE_CONTROL,
    CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY,
    CONF_INVERTER_ENTITY_GRID_EXPORT_POWER,
    CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY,
    CONF_INVERTER_ENTITY_GRID_IMPORT_POWER,
    CONF_INVERTER_ENTITY_GRID_POWER,
    CONF_INVERTER_ENTITY_PV_POWER,
    CONF_INVERTER_ENTITY_SOLAR_ENERGY,
    CONF_INVERTER_PLATFORM,
    CONF_INVERTER_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH,
    CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH,
    CONF_INVERTER_SOLIS_BACKFLOW_POWER,
    CONF_INVERTER_SOLIS_BATTERY_MAX_CHARGE_CURRENT,
    CONF_INVERTER_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT,
    CONF_INVERTER_SOLIS_FEED_IN_PRIORITY_SWITCH,
    CONF_INVERTER_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH,
    CONF_INVERTER_SOLIS_PEAK_MAX_USABLE_GRID_POWER,
    CONF_INVERTER_SOLIS_RC_SETPOINT,
    CONF_INVERTER_SOLIS_SELF_USE_SWITCH,
    CONF_INVERTER_SOLIS_STORAGE_CONTROL_READBACK,
    CONF_INVERTER_SOLIS_TOU_MODE_SWITCH,
    CONF_SOLIS_CONFIG_ENTRY_ID,
    DEFAULT_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH,
    DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH,
    DEFAULT_SOLIS_BACKFLOW_POWER,
    DEFAULT_SOLIS_BATTERY_MAX_CHARGE_CURRENT,
    DEFAULT_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT,
    DEFAULT_SOLIS_FEED_IN_PRIORITY_SWITCH,
    DEFAULT_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH,
    DEFAULT_SOLIS_PEAK_MAX_USABLE_GRID_POWER,
    DEFAULT_SOLIS_RC_SETPOINT,
    DEFAULT_SOLIS_SELF_USE_SWITCH,
    DEFAULT_SOLIS_STORAGE_CONTROL_READBACK,
    DEFAULT_SOLIS_TOU_MODE_SWITCH,
)
from ..outbound.inverter import InverterPlatform
from .solis_entity_resolver import resolve_solis_entities


@dataclass(frozen=True)
class ResolvedInverterEntities:
    """Fully resolved inverter entity IDs for coordinator wiring.

    Attributes:
        platform: The configured inverter platform.
        inverter_entity_ids: Telemetry + control role → entity-ID mapping
            passed to ``InverterController`` (and mirrored for the debug view).
        grid_import_power / grid_export_power: Per-direction grid-power sensor
            entity IDs; empty when only the signed net sensor is available.
        signed_grid_power: The signed net grid-power sensor entity ID.
        grid_import_total / grid_export_total: Daily-resetting grid-energy
            counter entity IDs.
        pv_power: Instantaneous PV-power sensor entity ID.
        solar_energy_today: Daily-resetting solar-energy counter entity ID.
        ac_port_power / backup_power: Derived-observer input entity IDs.
    """

    platform: InverterPlatform
    inverter_entity_ids: dict[str, str]
    grid_import_power: str
    grid_export_power: str
    signed_grid_power: str
    grid_import_total: str
    grid_export_total: str
    pv_power: str
    solar_energy_today: str
    ac_port_power: str
    backup_power: str


def _resolve_role_ids(hass: HomeAssistant, data: dict) -> dict[str, str]:
    """Build the telemetry + control role → entity-ID mapping for the platform.

    For Solis, prefers the auto-detect path (an explicit
    ``CONF_SOLIS_CONFIG_ENTRY_ID``, or a sole ``solis_modbus`` config entry
    recovered for legacy installs) and runs :func:`resolve_solis_entities`
    against it; otherwise it reads the manual 12-field mapping. The generic
    platform always uses its manual mapping.

    Args:
        hass: Home Assistant instance, used for the solis_modbus registry scan.
        data: Merged config-entry data + options.

    Returns:
        Role-keyed entity-ID dict ready for ``InverterController``.
    """
    inverter_platform = InverterPlatform(data[CONF_INVERTER_PLATFORM])
    if inverter_platform != InverterPlatform.SOLIS:
        return {
            "battery_soc": data[CONF_INVERTER_ENTITY_BATTERY_SOC],
            "battery_power": data[CONF_INVERTER_ENTITY_BATTERY_POWER],
            "grid_power": data[CONF_INVERTER_ENTITY_GRID_POWER],
            "charge_control": data[CONF_INVERTER_ENTITY_CHARGE_CONTROL],
            "grid_import_energy_today":
                data.get(CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY, ""),
            "grid_export_energy_today":
                data.get(CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY, ""),
        }

    solis_entry_id = data.get(CONF_SOLIS_CONFIG_ENTRY_ID)
    # Legacy configs created before solis auto-detect existed don't have
    # CONF_SOLIS_CONFIG_ENTRY_ID stored. Recover by scanning the registry
    # for solis_modbus config entries — when exactly one is present the
    # choice is unambiguous, so we run the resolver against it and let
    # its findings (grid_power_net + today-counter slugs) override the
    # legacy manual values. Avoids forcing every existing user through
    # a reconfigure cycle.
    if not solis_entry_id:
        solis_entries = hass.config_entries.async_entries("solis_modbus")
        if len(solis_entries) == 1:
            solis_entry_id = solis_entries[0].entry_id
    if solis_entry_id:
        # Auto-detected path: resolve all entity IDs from the entity registry.
        inverter_entity_ids = resolve_solis_entities(hass, solis_entry_id)
        # Telemetry roles share keys with the manual mapping, so ensure
        # battery_soc / battery_power / grid_power survive auto-detect
        # even if the resolver missed one (its defaults still work).
        inverter_entity_ids.setdefault(
            "battery_soc", data.get(CONF_INVERTER_ENTITY_BATTERY_SOC, ""),
        )
        inverter_entity_ids.setdefault(
            "battery_power", data.get(CONF_INVERTER_ENTITY_BATTERY_POWER, ""),
        )
        inverter_entity_ids.setdefault(
            "grid_power", data.get(CONF_INVERTER_ENTITY_GRID_POWER, ""),
        )
        inverter_entity_ids.setdefault(
            "grid_import_energy_today",
            data.get(CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY, ""),
        )
        inverter_entity_ids.setdefault(
            "grid_export_energy_today",
            data.get(CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY, ""),
        )
        return inverter_entity_ids

    # Manual-mapping fallback: entity IDs stored directly in config entry data.
    return {
        "battery_soc": data[CONF_INVERTER_ENTITY_BATTERY_SOC],
        "battery_power": data[CONF_INVERTER_ENTITY_BATTERY_POWER],
        "grid_power": data[CONF_INVERTER_ENTITY_GRID_POWER],
        "grid_import_energy_today":
            data.get(CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY, ""),
        "grid_export_energy_today":
            data.get(CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY, ""),
        "storage_control_readback":      data.get(CONF_INVERTER_SOLIS_STORAGE_CONTROL_READBACK, DEFAULT_SOLIS_STORAGE_CONTROL_READBACK),
        "battery_max_charge_current":    data.get(CONF_INVERTER_SOLIS_BATTERY_MAX_CHARGE_CURRENT, DEFAULT_SOLIS_BATTERY_MAX_CHARGE_CURRENT),
        "battery_max_discharge_current": data.get(CONF_INVERTER_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT, DEFAULT_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT),
        "rc_setpoint":                   data.get(CONF_INVERTER_SOLIS_RC_SETPOINT, DEFAULT_SOLIS_RC_SETPOINT),
        "backflow_power":                data.get(CONF_INVERTER_SOLIS_BACKFLOW_POWER, DEFAULT_SOLIS_BACKFLOW_POWER),
        "peak_max_usable_grid_power":    data.get(CONF_INVERTER_SOLIS_PEAK_MAX_USABLE_GRID_POWER, DEFAULT_SOLIS_PEAK_MAX_USABLE_GRID_POWER),
        "self_use_switch":               data.get(CONF_INVERTER_SOLIS_SELF_USE_SWITCH, DEFAULT_SOLIS_SELF_USE_SWITCH),
        "tou_mode_switch":               data.get(CONF_INVERTER_SOLIS_TOU_MODE_SWITCH, DEFAULT_SOLIS_TOU_MODE_SWITCH),
        "allow_grid_charge_switch":      data.get(CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH, DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH),
        "feed_in_priority_switch":       data.get(CONF_INVERTER_SOLIS_FEED_IN_PRIORITY_SWITCH, DEFAULT_SOLIS_FEED_IN_PRIORITY_SWITCH),
        "allow_export_under_self_use_switch": data.get(CONF_INVERTER_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH, DEFAULT_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH),
        "grid_feed_in_power_limit_switch": data.get(CONF_INVERTER_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH, DEFAULT_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH),
    }


def resolve_inverter_entities(
    hass: HomeAssistant, data: dict,
) -> ResolvedInverterEntities:
    """Resolve every inverter entity ID the coordinator needs from config.

    Builds the role mapping for ``InverterController`` and pins the
    observer-pipeline entity IDs, merging manual config first and Solis
    auto-detect second. Auto-detected yesterday-total entities are written
    back into ``data`` under the keys ``yesterday_total_resolver`` expects.

    Args:
        hass: Home Assistant instance.
        data: Merged config-entry data + options. **Mutated in place** to add
            any auto-detected yesterday-total entity IDs.

    Returns:
        A :class:`ResolvedInverterEntities` with the role mapping and every
        observer-pipeline entity ID.
    """
    inverter_platform = InverterPlatform(data[CONF_INVERTER_PLATFORM])
    inverter_entity_ids = _resolve_role_ids(hass, data)

    # Per-direction grid-power observers — prefer the per-direction
    # entity when configured. When absent (typical for Solis auto-detect,
    # where solis_modbus only publishes signed ``grid_power_net``), the
    # observers fall back to the signed sensor and project onto their
    # side via ``_signed_polarity``. The signed ``grid_power`` entity
    # also remains in ``inverter_entity_ids`` for
    # ``InverterController.get_grid_power`` (used by BatteryReading /
    # capacity estimator), independent of the observer pipeline.
    grid_import_power = data.get(CONF_INVERTER_ENTITY_GRID_IMPORT_POWER, "")
    grid_export_power = data.get(CONF_INVERTER_ENTITY_GRID_EXPORT_POWER, "")
    signed_grid_power = inverter_entity_ids.get("grid_power", "")
    grid_import_total = inverter_entity_ids.get("grid_import_energy_today", "")
    grid_export_total = inverter_entity_ids.get("grid_export_energy_today", "")
    # PV power + today-solar-energy: prefer the manual-config value when
    # set; fall back to the Solis resolver's auto-detected entity. The
    # resolver omits the role entirely when not present, so dict.get
    # with empty-string default is the right merge.
    pv_power = (
        data.get(CONF_INVERTER_ENTITY_PV_POWER, "")
        or inverter_entity_ids.get("pv_power", "")
    )
    solar_energy_today = (
        data.get(CONF_INVERTER_ENTITY_SOLAR_ENERGY, "")
        or inverter_entity_ids.get("solar_energy_today", "")
    )
    # Derived-observer inputs — same manual-first / Solis-auto-second merge.
    # Both are optional; absence simply disables the consumption + losses
    # observed series. The grid-net and battery/PV signals already arrive
    # via the existing translators, so only AC port + backup are new
    # entities to wire through.
    ac_port_power = (
        data.get(CONF_INVERTER_ENTITY_AC_PORT_POWER, "")
        or inverter_entity_ids.get("ac_port_power", "")
    )
    backup_power = (
        data.get(CONF_INVERTER_ENTITY_BACKUP_POWER, "")
        or inverter_entity_ids.get("backup_power", "")
    )
    # Yesterday-total entities: map auto-detected Solis entries into the
    # raw-config dict so ``yesterday_total_resolver`` finds them via its
    # ``DEDICATED_ENTITY_CONFIG_KEY`` lookup without further plumbing.
    for cfg_key, role_key in (
        ("inverter_entity_generation_yesterday", "solar_energy_yesterday"),
        ("inverter_entity_grid_import_yesterday", "grid_import_energy_yesterday"),
        ("inverter_entity_grid_export_yesterday", "grid_export_energy_yesterday"),
    ):
        if not data.get(cfg_key) and inverter_entity_ids.get(role_key):
            data[cfg_key] = inverter_entity_ids[role_key]

    return ResolvedInverterEntities(
        platform=inverter_platform,
        inverter_entity_ids=inverter_entity_ids,
        grid_import_power=grid_import_power,
        grid_export_power=grid_export_power,
        signed_grid_power=signed_grid_power,
        grid_import_total=grid_import_total,
        grid_export_total=grid_export_total,
        pv_power=pv_power,
        solar_energy_today=solar_energy_today,
        ac_port_power=ac_port_power,
        backup_power=backup_power,
    )
