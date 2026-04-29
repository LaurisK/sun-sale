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

# Config entry keys — Solis-specific inverter entities
CONF_INVERTER_SOLIS_CHARGE_CURRENT = "inverter_solis_charge_current"
CONF_INVERTER_SOLIS_DISCHARGE_CURRENT = "inverter_solis_discharge_current"
CONF_INVERTER_SOLIS_CHARGE_START_TIME_1 = "inverter_solis_charge_start_time_1"
CONF_INVERTER_SOLIS_CHARGE_END_TIME_1 = "inverter_solis_charge_end_time_1"
CONF_INVERTER_SOLIS_DISCHARGE_START_TIME_1 = "inverter_solis_discharge_start_time_1"
CONF_INVERTER_SOLIS_DISCHARGE_END_TIME_1 = "inverter_solis_discharge_end_time_1"
CONF_INVERTER_SOLIS_TOU_MODE_SWITCH = "inverter_solis_tou_mode_switch"
CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH = "inverter_solis_allow_grid_charge_switch"
CONF_INVERTER_SOLIS_SELF_USE_MODE_SWITCH = "inverter_solis_self_use_mode_switch"

# Config entry keys — EV charger
CONF_EV_ENABLED = "ev_enabled"
CONF_EV_PLATFORM = "ev_platform"
CONF_EV_BATTERY_CAPACITY = "ev_battery_capacity_kwh"
CONF_EV_MAX_CHARGE_POWER = "ev_max_charge_power_kw"
CONF_EV_MIN_CHARGE_POWER = "ev_min_charge_power_kw"
CONF_EV_ENTITY_PLUG_STATE = "ev_entity_plug_state"
CONF_EV_ENTITY_SOC = "ev_entity_soc"
CONF_EV_ENTITY_TARGET_SOC = "ev_entity_target_soc"
CONF_EV_ENTITY_DEPARTURE_TIME = "ev_entity_departure_time"
CONF_EV_ENTITY_CHARGER_SWITCH = "ev_entity_charger_switch"

# Config entry keys — data sources
CONF_NORDPOOL_ENTITY = "nordpool_entity"
CONF_SOLAR_FORECAST_ENTITY = "solar_forecast_entity"
CONF_SOLAR_FORECAST_ENTITY_2 = "solar_forecast_entity_2"
CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD = "inverter_entity_household_load"

# Persistent storage
STORAGE_KEY_CAPACITY = f"{DOMAIN}_capacity"
STORAGE_VERSION = 1

# Update interval (minutes)
UPDATE_INTERVAL_MINUTES = 5

# Capacity estimator: discard observations with SoC delta below this threshold
CAPACITY_OBS_MIN_SOC_DELTA = 0.05

# Defaults
DEFAULT_BATTERY_MIN_SOC = 10
DEFAULT_BATTERY_MAX_SOC = 95
DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY = 90
DEFAULT_BATTERY_RATED_CYCLE_LIFE = 6000
DEFAULT_BATTERY_NOMINAL_VOLTAGE = 48.0
DEFAULT_EV_MIN_CHARGE_POWER_KW = 1.4
DEFAULT_EV_TARGET_SOC = 0.80

# Default Solis entity IDs (canonical names from the solis_modbus integration)
DEFAULT_SOLIS_CHARGE_CURRENT = "number.solis_time_charging_charge_current"
DEFAULT_SOLIS_DISCHARGE_CURRENT = "number.solis_time_charging_discharge_current"
DEFAULT_SOLIS_CHARGE_START_TIME_1 = "time.solis_time_charging_charge_start_slot_1"
DEFAULT_SOLIS_CHARGE_END_TIME_1 = "time.solis_time_charging_charge_end_slot_1"
DEFAULT_SOLIS_DISCHARGE_START_TIME_1 = "time.solis_time_charging_discharge_start_slot_1"
DEFAULT_SOLIS_DISCHARGE_END_TIME_1 = "time.solis_time_charging_discharge_end_slot_1"
DEFAULT_SOLIS_TOU_MODE_SWITCH = "switch.solis_time_of_use_mode"
DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH = "switch.solis_allow_grid_to_charge_the_battery"
DEFAULT_SOLIS_SELF_USE_MODE_SWITCH = "switch.solis_self_use_mode"
