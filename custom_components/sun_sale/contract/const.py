"""Constants for the sunSale integration."""

DOMAIN = "sun_sale"

# Config entry keys — tariff
CONF_TARIFF_DISTRIBUTION_FEE = "distribution_fee"
CONF_TARIFF_TAX_RATE = "tax_rate"
CONF_TARIFF_MARKUP = "markup"
CONF_TARIFF_SELL_DISTRIBUTION_FEE = "sell_distribution_fee"
CONF_TARIFF_SELL_TAX_RATE = "sell_tax_rate"
CONF_TARIFF_SELL_MARKUP = "sell_markup"

# Config entry keys — battery
CONF_BATTERY_NOMINAL_CAPACITY = "nominal_capacity_kwh"
CONF_BATTERY_PURCHASE_PRICE = "purchase_price_eur"
CONF_BATTERY_RATED_CYCLE_LIFE = "rated_cycle_life"
CONF_BATTERY_MAX_CHARGE_POWER = "max_charge_power_kw"
CONF_BATTERY_MAX_DISCHARGE_POWER = "max_discharge_power_kw"
CONF_BATTERY_MIN_SOC = "min_soc"
CONF_BATTERY_MAX_SOC = "max_soc"
CONF_BATTERY_ROUND_TRIP_EFFICIENCY = "round_trip_efficiency"
CONF_BATTERY_NOMINAL_VOLTAGE = "nominal_voltage_v"

# Config entry keys — inverter
CONF_INVERTER_PLATFORM = "inverter_platform"
CONF_INVERTER_ENTITY_BATTERY_SOC = "inverter_entity_battery_soc"
CONF_INVERTER_ENTITY_GRID_POWER = "inverter_entity_grid_power"
CONF_INVERTER_ENTITY_BATTERY_POWER = "inverter_entity_battery_power"
CONF_INVERTER_ENTITY_CHARGE_CONTROL = "inverter_entity_charge_control"

# Config entry keys — Solis-specific inverter entities (state-machine model).
# Auto-detection via inbound/solis_entity_resolver.py is the preferred path;
# these CONF_* keys back the manual-mapping fallback form in config_flow.py.
CONF_SOLIS_CONFIG_ENTRY_ID = "solis_config_entry_id"
CONF_INVERTER_SOLIS_STORAGE_CONTROL_READBACK = "inverter_solis_storage_control_readback"
CONF_INVERTER_SOLIS_BATTERY_MAX_CHARGE_CURRENT = "inverter_solis_battery_max_charge_current"
CONF_INVERTER_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT = "inverter_solis_battery_max_discharge_current"
CONF_INVERTER_SOLIS_RC_SETPOINT = "inverter_solis_rc_setpoint"
CONF_INVERTER_SOLIS_BACKFLOW_POWER = "inverter_solis_backflow_power"
CONF_INVERTER_SOLIS_PEAK_MAX_USABLE_GRID_POWER = "inverter_solis_peak_max_usable_grid_power"
CONF_INVERTER_SOLIS_SELF_USE_SWITCH = "inverter_solis_self_use_switch"
CONF_INVERTER_SOLIS_TOU_MODE_SWITCH = "inverter_solis_tou_mode_switch"
CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH = "inverter_solis_allow_grid_charge_switch"
CONF_INVERTER_SOLIS_FEED_IN_PRIORITY_SWITCH = "inverter_solis_feed_in_priority_switch"
CONF_INVERTER_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH = (
    "inverter_solis_allow_export_under_self_use_switch"
)
CONF_INVERTER_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH = (
    "inverter_solis_grid_feed_in_power_limit_switch"
)

# Config entry keys — data sources
CONF_NORDPOOL_ENTITY = "nordpool_entity"
CONF_NORDPOOL_RESOLUTION = "nordpool_resolution"
CONF_SOLAR_FORECAST_ENTITY = "solar_forecast_entity"
CONF_SOLAR_FORECAST_ENTITY_2 = "solar_forecast_entity_2"
CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD = "inverter_entity_household_load"
CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY = (
    "inverter_entity_household_consumption_energy"
)
CONF_INVERTER_ENTITY_SOLAR_ENERGY = "inverter_entity_solar_energy"
CONF_INVERTER_ENTITY_PV_POWER = "inverter_entity_pv_power"
CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY = "inverter_entity_grid_import_energy"
CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY = "inverter_entity_grid_export_energy"

# Persistent storage
STORAGE_KEY_CAPACITY = f"{DOMAIN}_capacity"
STORAGE_KEY_YESTERDAY = f"{DOMAIN}_yesterday"
STORAGE_KEY_GENERATION = f"{DOMAIN}_generation"
STORAGE_KEY_PV_POWER = f"{DOMAIN}_pv_power"
STORAGE_KEY_HOUSEHOLD_LOAD = f"{DOMAIN}_household_load"
STORAGE_KEY_PRICE_HISTORY = f"{DOMAIN}_price_history"
STORAGE_KEY_FORECAST_QUALITY = f"{DOMAIN}_forecast_quality"
STORAGE_KEY_GRID_POWER = f"{DOMAIN}_grid_power"
STORAGE_KEY_GRID_IMPORT_TOTAL = f"{DOMAIN}_grid_import_total"
STORAGE_KEY_GRID_EXPORT_TOTAL = f"{DOMAIN}_grid_export_total"
STORAGE_KEY_MONTHLY_BILL = f"{DOMAIN}_monthly_bill"
STORAGE_KEY_MODE_HISTORY = f"{DOMAIN}_mode_history"
STORAGE_VERSION = 1

# Rolling generation-sample retention (days). Anything older than this is
# trimmed before persistence each cycle.
GENERATION_HISTORY_RETENTION_DAYS = 2

# Rolling PV-power-sample retention (days). Covers yesterday + today slots.
PV_POWER_HISTORY_RETENTION_DAYS = 2

# Rolling grid-power-sample retention (days). Covers yesterday + today for billing.
GRID_POWER_HISTORY_RETENTION_DAYS = 2

# Rolling import/export today-total counter retention (days). Same window as
# grid power so end-of-day correction always has yesterday's final value
# available alongside today's samples.
GRID_IMPORT_TOTAL_HISTORY_RETENTION_DAYS = 2
GRID_EXPORT_TOTAL_HISTORY_RETENTION_DAYS = 2

# Rolling household-load retention (days). Sized at ~1.5× the baseload
# profile window (30d) so a few stale samples at the tail don't strand
# entries that just left the window.
HOUSEHOLD_LOAD_HISTORY_RETENTION_DAYS = 45

# Rolling price-history retention (days) for profitability scoring.
PRICE_HISTORY_RETENTION_DAYS = 90

# Update interval (minutes)
UPDATE_INTERVAL_MINUTES = 5

# Capacity estimator: discard observations with SoC delta below this threshold
CAPACITY_OBS_MIN_SOC_DELTA = 0.05

# Defaults
DEFAULT_NORDPOOL_RESOLUTION = "15min"

DEFAULT_BATTERY_MIN_SOC = 10
DEFAULT_BATTERY_MAX_SOC = 95
DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY = 90
DEFAULT_BATTERY_RATED_CYCLE_LIFE = 6000
DEFAULT_BATTERY_NOMINAL_VOLTAGE = 48.0

# Default Solis entity IDs (canonical names from the solis_modbus integration).
# Only used as placeholders in the manual-mapping config-flow form when
# auto-detection via the entity registry fails.
DEFAULT_SOLIS_STORAGE_CONTROL_READBACK = "sensor.solis_storage_control_switch_value"
DEFAULT_SOLIS_BATTERY_MAX_CHARGE_CURRENT = "number.solis_battery_max_charge_current"
DEFAULT_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT = "number.solis_battery_max_discharge_current"
DEFAULT_SOLIS_RC_SETPOINT = "number.solis_rc_inverter_ac_grid_active_power"
DEFAULT_SOLIS_BACKFLOW_POWER = "number.solis_backflow_power"
DEFAULT_SOLIS_PEAK_MAX_USABLE_GRID_POWER = "number.solis_peak_max_usable_grid_power"
DEFAULT_SOLIS_SELF_USE_SWITCH = "switch.solis_self_use_mode"
DEFAULT_SOLIS_TOU_MODE_SWITCH = "switch.solis_time_of_use_mode"
DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH = "switch.solis_allow_grid_to_charge_the_battery"
DEFAULT_SOLIS_FEED_IN_PRIORITY_SWITCH = "switch.solis_feed_in_priority_mode"
DEFAULT_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH = (
    "switch.solis_allow_export_switch_under_self_generation_and_self_use"
)
DEFAULT_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH = "switch.solis_grid_feed_in_power_limit_switch"

# Default inverter / export limits used by storage_mode_specs.build_specs()
# until a per-deployment value is wired through the config flow.
DEFAULT_INVERTER_MAX_POWER_W = 10_000
DEFAULT_EXPORT_LIMIT_W = 10_000

# Mode-history retention: prune samples older than the start of yesterday
# (computed in local time by the control module).
MODE_HISTORY_RETENTION_DAYS = 2
