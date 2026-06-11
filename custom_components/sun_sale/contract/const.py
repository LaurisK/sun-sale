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
CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY = (
    "inverter_entity_household_consumption_energy"
)
CONF_INVERTER_ENTITY_SOLAR_ENERGY = "inverter_entity_solar_energy"
CONF_INVERTER_ENTITY_PV_POWER = "inverter_entity_pv_power"
CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY = "inverter_entity_grid_import_energy"
CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY = "inverter_entity_grid_export_energy"
# Per-direction instantaneous grid-power entities (preferred). When both are
# configured, the coordinator uses these in place of the legacy signed
# ``CONF_INVERTER_ENTITY_GRID_POWER``. Each entity must report a
# non-negative magnitude in W or kW for its direction only — the other
# direction reads ~0 at any moment because grid flow is one-way.
CONF_INVERTER_ENTITY_GRID_IMPORT_POWER = "inverter_entity_grid_import_power"
CONF_INVERTER_ENTITY_GRID_EXPORT_POWER = "inverter_entity_grid_export_power"
# AC grid-port power (signed; convention: positive = inverter→grid) feeding the
# derived consumption + losses observers. The Solis auto-detect path resolves
# this to ``ac_grid_port_power``; non-Solis installs map it manually.
CONF_INVERTER_ENTITY_AC_PORT_POWER = "inverter_entity_ac_port_power"
# Backup-port output power (magnitude, ≥ 0). Non-zero only when the inverter
# is bridging backup-protected loads with grid down; otherwise ~0. Solis
# resolves to ``backup_load_power``.
CONF_INVERTER_ENTITY_BACKUP_POWER = "inverter_entity_backup_power"
# Optional: HA entity exposing the inverter's own clock (local-time
# datetime). When set, the inverter_time module tracks HA↔inverter skew so
# the pre-rollover snapshot fires relative to the inverter's idea of midnight
# instead of HA's. Leaving this empty disables skew correction.
CONF_INVERTER_ENTITY_INVERTER_CLOCK = "inverter_entity_inverter_clock"

# Persistent storage
STORAGE_KEY_CAPACITY = f"{DOMAIN}_capacity"
STORAGE_KEY_YESTERDAY = f"{DOMAIN}_yesterday"
STORAGE_KEY_GENERATION = f"{DOMAIN}_generation"
STORAGE_KEY_PV_POWER = f"{DOMAIN}_pv_power"
STORAGE_KEY_CONSUMPTION_DAILY = f"{DOMAIN}_consumption_daily"
STORAGE_KEY_PRICE_HISTORY = f"{DOMAIN}_price_history"
STORAGE_KEY_FORECAST_QUALITY = f"{DOMAIN}_forecast_quality"
STORAGE_KEY_GRID_IMPORT_POWER = f"{DOMAIN}_grid_import_power"
STORAGE_KEY_GRID_EXPORT_POWER = f"{DOMAIN}_grid_export_power"
STORAGE_KEY_GRID_IMPORT_TOTAL = f"{DOMAIN}_grid_import_total"
STORAGE_KEY_GRID_EXPORT_TOTAL = f"{DOMAIN}_grid_export_total"
STORAGE_KEY_DERIVED_POWER = f"{DOMAIN}_derived_power"
STORAGE_KEY_MONTHLY_BILL = f"{DOMAIN}_monthly_bill"
STORAGE_KEY_MODE_HISTORY = f"{DOMAIN}_mode_history"
STORAGE_VERSION = 1

# Debounce window (seconds) for PersistentStore writes. A single coordinator
# tick fans out ~10–14 logical saves (the rolling sample histories plus the
# forecast-quality / monthly-bill / price-history stores), and off-cycle
# refreshes (mode-override presses, force_recalculate, startup) burst these
# further — each previously a full-JSON Store.async_save rewrite, ~3–4k
# writes/day. Routing them through Store.async_delay_save coalesces the
# per-tick fan-out and any burst into one debounced background write per store,
# off the synchronous update path — meaningful flash wear relief on SD-card
# installs. Kept below UPDATE_INTERVAL_MINUTES so steady-cadence writes still
# land each cycle; HA flushes pending writes on clean shutdown via its
# final-write listener, so only an unclean crash within the window can drop the
# most recent (reconstructible) sample.
STORE_SAVE_DELAY_SECONDS = 30

# Rolling generation-sample retention (days). Anything older than this is
# trimmed before persistence each cycle.
GENERATION_HISTORY_RETENTION_DAYS = 2

# Rolling PV-power-sample retention (days). Covers yesterday + today slots.
PV_POWER_HISTORY_RETENTION_DAYS = 2

# Rolling per-direction grid-power-sample retention (days). One value applies
# to both the import and export history stores. Covers yesterday + today for
# billing.
GRID_POWER_HISTORY_RETENTION_DAYS = 2

# Rolling import/export today-total counter retention (days). Same window as
# grid power so end-of-day correction always has yesterday's final value
# available alongside today's samples.
GRID_IMPORT_TOTAL_HISTORY_RETENTION_DAYS = 2
GRID_EXPORT_TOTAL_HISTORY_RETENTION_DAYS = 2

# Rolling derived-power-sample retention (days). Mirrors GRID_POWER history;
# enough to cover yesterday + today for the bake-in window.
DERIVED_POWER_HISTORY_RETENTION_DAYS = 2

# Baked observed history retention (days). Keeps enough history for the
# integration check's rollup window (per-side faults over the last month).
BAKED_OBSERVED_HISTORY_RETENTION_DAYS = 35

# Pre-rollover counter snapshot retention (days). Snapshots only need to
# survive long enough for the next-day bake-in to consume them.
COUNTER_SNAPSHOT_HISTORY_RETENTION_DAYS = 2

# Persistent storage keys for the new observed-series stores.
STORAGE_KEY_BAKED_OBSERVED = f"{DOMAIN}_baked_observed"
STORAGE_KEY_COUNTER_SNAPSHOT = f"{DOMAIN}_counter_snapshot"

# Bake-in source-kind discriminator values stored in BakedDayRecord.source_kind.
SOURCE_KIND_DEDICATED_SENSOR = "dedicated_sensor"
SOURCE_KIND_SNAPSHOT = "snapshot"
SOURCE_KIND_FAILED_NO_SOURCE = "failed_no_source"

# Bake-in hard cutoff — local time after which a bake-in attempt records
# failed_no_source if no source value has materialised. Expressed as
# (hour, minute) tuple in the local timezone.
BAKE_IN_HARD_CUTOFF_LOCAL = (6, 0)

# Pre-rollover snapshot window — local time range during which the snapshot
# module captures the current today-total counter values. Expressed as
# ((start_hour, start_minute), (end_hour, end_minute)) in local time.
SNAPSHOT_WINDOW_LOCAL = ((23, 30), (23, 59))

# Inverter clock-skew tracker: minimum samples in the rolling window before
# ``current_skew_seconds`` returns a value. Until then, the snapshot module
# uses HA local time directly (no shift).
INVERTER_TIME_MIN_SAMPLES = 5

# Inverter clock-skew tracker: maximum samples retained in the rolling
# window. Older samples are trimmed each cycle. Sized so a 5 min coordinator
# cadence yields roughly four hours of history — enough for the median to
# track gradual drift but small enough that a one-shot bad reading washes
# out quickly.
INVERTER_TIME_MAX_SAMPLES = 50

# Rolling per-day consumption-bucket retention (days). One ConsumptionDayRecord
# per local date, each holding 24 hour-bucket sums in kWh. Sized to give a
# full 30 finalised days of input to the per-hour P15 baseload profile.
CONSUMPTION_DAILY_WINDOW_DAYS = 30

# Per-day per-hour completeness gate. A day's hour bucket only feeds the P15
# profile when at least this fraction of the price-grid slots in that hour
# had a derived sample — drops days where the inverter was offline for part
# of the hour and the sum would otherwise underestimate the floor.
CONSUMPTION_DAILY_MIN_HOUR_COMPLETENESS = 0.8

# Rolling price-history retention (days) for profitability scoring.
PRICE_HISTORY_RETENTION_DAYS = 90

# Update interval (minutes)
UPDATE_INTERVAL_MINUTES = 5

# Capacity estimator: discard observations with SoC delta below this threshold
CAPACITY_OBS_MIN_SOC_DELTA = 0.05

# Capacity estimator: reject observations whose inter-reading interval falls
# outside this band around the nominal update interval. Refreshes are not always
# UPDATE_INTERVAL_MINUTES apart — mode-override changes, force_recalculate, and
# startup all fire an off-cycle async_request_refresh that can land seconds after
# the previous cycle, while a stalled coordinator can skip cycles entirely. Either
# breaks the two-endpoint average-power energy estimate, so only accept intervals
# close to nominal.
CAPACITY_OBS_MIN_INTERVAL_S = UPDATE_INTERVAL_MINUTES * 60 * 0.5
CAPACITY_OBS_MAX_INTERVAL_S = UPDATE_INTERVAL_MINUTES * 60 * 2.0

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

# Schedule policy switches — user-toggleable flags that constrain the DP
# scheduler's action set. Defaults preserve the historical "all modes
# available" behaviour so existing installs see no change after upgrade.
DEFAULT_SCHEDULE_USE_STANDBY = True
DEFAULT_SCHEDULE_ALLOW_GRID_CHARGING = True
DEFAULT_SCHEDULE_ALLOW_FEED_IN = True
DEFAULT_SCHEDULE_ALLOW_DISCHARGE_TO_GRID = True

# Numeric schedule-policy knobs. Values mirror the in-module DEFAULT_* used by
# pipeline/schedule.py so that the user-facing entities start at the same
# operating point the planner used before they were exposed.
DEFAULT_SCHEDULE_MODE_CHANGE_PENALTY_EUR_PER_KWH = 0.005
DEFAULT_SCHEDULE_PROFITABILITY_TILT_ALPHA = 0.5
DEFAULT_SCHEDULE_TERMINAL_VALUE_DISCOUNT = 0.5
# None means "use hardware max from BatteryConfig"; set a lower value to
# limit the DP's peak grid-export rate in Discharge-to-grid slots.
DEFAULT_SCHEDULE_MAX_DISCHARGE_TO_GRID_KW: float | None = None

# Bounds enforced by the Number entities and clamped by the coordinator before
# the policy reaches the DP. Mode-change penalty is bounded above by 0.10
# EUR/kWh — much higher and the DP would never change modes; profitability
# tilt and terminal discount are dimensionless and live in [0, 1].
SCHEDULE_MODE_CHANGE_PENALTY_MIN = 0.0
SCHEDULE_MODE_CHANGE_PENALTY_MAX = 0.10
SCHEDULE_PROFITABILITY_TILT_ALPHA_MIN = 0.0
SCHEDULE_PROFITABILITY_TILT_ALPHA_MAX = 1.0
SCHEDULE_TERMINAL_VALUE_DISCOUNT_MIN = 0.0
SCHEDULE_TERMINAL_VALUE_DISCOUNT_MAX = 1.0
SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MIN = 0.5
SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MAX = 30.0
