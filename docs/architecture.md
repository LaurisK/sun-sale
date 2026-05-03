# sunSale Architecture

## Project Overview

**sunSale** is a Home Assistant custom integration that automates electricity buying/selling and EV charging decisions for households with solar panels and battery storage. It optimizes around Nordpool spot prices, solar generation forecasts, battery state, tariff costs, and EV charging demand.

---

## Layer Structure

| Layer | Modules | HA Dependency |
|---|---|---|
| **Contracts** | `models.py`, `const.py` | None |
| **Pure logic** | `tariff.py`, `battery.py`, `optimizer.py`, `ev_scheduler.py` | None — unit-testable in isolation |
| **Platform adapters** | `inverter.py`, `ev_charger.py` | Yes — HA service calls + state reads |
| **Orchestration** | `coordinator.py`, `dashboard.py` | Yes — DataUpdateCoordinator |
| **HA integration** | `__init__.py`, `config_flow.py`, `sensor.py`, `switch.py`, `debug_view.py` | Yes |

---

## ASCII Structure Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         HOME ASSISTANT                                  │
│                                                                         │
│  ┌──────────────┐   ┌───────────────┐   ┌────────────────────────────┐ │
│  │ config_flow  │   │  __init__.py  │   │     debug_view.py          │ │
│  │              │──▶│               │   │  GET /api/sun_sale/debug   │ │
│  │ 5-step setup │   │ async_setup_  │   └────────────────────────────┘ │
│  │ + options    │   │ entry()       │                                   │
│  └──────────────┘   └──────┬────────┘                                  │
│                            │ creates                                    │
│                            ▼                                            │
│              ┌─────────────────────────────┐                           │
│              │       coordinator.py        │◀─── HA state machine      │
│              │   SunSaleCoordinator        │     (Nordpool, solar,     │
│              │   5-min DataUpdateCoord.    │      inverter, EV)        │
│              └──────────────┬──────────────┘                           │
│                             │ returns coordinator.data dict             │
│           ┌─────────────────┼──────────────────┐                      │
│           ▼                 ▼                  ▼                       │
│     ┌───────────┐    ┌────────────┐    ┌─────────────┐                │
│     │ sensor.py │    │ switch.py  │    │ dashboard   │                 │
│     │ 13 sensors│    │ Automation │    │ (consumed   │                 │
│     │           │    │ on/off     │    │  via sensor)│                 │
│     └───────────┘    └────────────┘    └─────────────┘                │
└─────────────────────────────────────────────────────────────────────────┘

┌──────────── Pure Python (no HA dependencies) ──────────────────────────┐
│                                                                         │
│   models.py          tariff.py         battery.py                      │
│   ─────────          ─────────         ──────────                      │
│   HourlyPrice    ┌──▶buy_price()   ┌──▶degradation_cost_per_kwh()     │
│   TariffResult   │   sell_price()  │   trade_profit_per_kwh()          │
│   SolarForecast  │   compute_      │   CapacityEstimator               │
│   ScheduleSlot   │   tariffs()     │                                   │
│   Schedule       │                 │                                   │
│   BatteryConfig  │   optimizer.py  │   ev_scheduler.py                 │
│   EVChargerState │   ───────────   │   ──────────────                  │
│   EVSchedule     │   optimize_     │   schedule_ev_charge()            │
│   (frozen        │   schedule() ───┘                                   │
│    dataclasses)  │                                                      │
└──────────────────┼──────────────────────────────────────────────────────┘
                   │
┌──────────── HA Platform Abstractions ──────────────────────────────────┐
│                  │                                                      │
│   inverter.py        ev_charger.py                                     │
│   ──────────         ─────────────                                     │
│   InverterController  EVChargerController                               │
│   .async_charge_from_grid()   .async_start_charging()                 │
│   .async_discharge_to_grid()  .async_stop_charging()                  │
│   .async_idle()               .is_plugged_in()                        │
│   .get_battery_soc()          .get_ev_soc()                           │
│   .get_battery_power()                                                  │
│   .get_grid_power()                                                     │
│                                                                         │
│   Platforms: Solis, Huawei, SolarEdge, GoodWe, Generic                │
│   Platforms: OpenEVSE, Easee, Wallbox, Generic                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Module Reference

### `models.py` — Shared data contracts

All data structures as frozen dataclasses. No HA imports. Everything else imports from here.

```python
# Inputs
HourlyPrice(start, end, price_eur_kwh)
SolarForecast(start, end, generation_kwh)

# Configs (frozen — built from config_entry, immutable per run)
TariffConfig(distribution_fee, tax_rate, markup, sell_distribution_fee, sell_tax_rate, sell_markup)
BatteryConfig(nominal_capacity_kwh, purchase_price_eur, rated_cycle_life,
              max_charge_power_kw, max_discharge_power_kw, min_soc, max_soc,
              round_trip_efficiency, nominal_voltage_v)
EVChargerConfig(max_charge_power_kw, min_charge_power_kw, battery_capacity_kwh)

# Computed outputs
TariffResult(hour, spot_price, buy_price, sell_price)
ScheduleSlot(start, end, action, power_kw, expected_soc_after, expected_profit_eur, reason)
Schedule(slots[], total_expected_profit_eur, degradation_cost_per_kwh, computed_at)
EVChargeSlot(start, end, charge_power_kw, cost_eur)
EVSchedule(slots[], total_cost_eur, total_energy_kwh, computed_at)

# Runtime state (mutable)
BatteryState(soc, estimated_capacity_kwh)
EVChargerState(is_plugged_in, soc, target_soc, departure_time)
CapacityObservation(timestamp, soc_start, soc_end, energy_kwh, direction)
Action(Enum): IDLE | CHARGE_FROM_GRID | DISCHARGE_TO_GRID | CHARGE_FROM_SOLAR
```

---

### `tariff.py` — Spot price → effective buy/sell price

Pure functions. Called by coordinator before optimizer.

```python
buy_price(spot, config)  →  (spot + distribution_fee + markup) * (1 + tax_rate)
sell_price(spot, config) →  (spot - sell_distribution_fee - sell_markup) * (1 - sell_tax_rate)
compute_tariffs(prices: list[HourlyPrice], config: TariffConfig) → list[TariffResult]
```

---

### `battery.py` — Degradation model & capacity learning

Pure functions + stateful estimator. The estimator persists to HA Store.

```python
degradation_cost_per_kwh(config, state) → float
  # purchase_price / (rated_cycle_life * estimated_capacity * 2)

trade_profit_per_kwh(buy_tariff, sell_tariff, deg_cost, efficiency) → float
  # sell * efficiency - buy - deg_cost * 2

class CapacityEstimator:
  .add_observation(CapacityObservation) → None  # exponentially-weighted avg (decay=0.9)
  .estimated_capacity_kwh → float
  .to_dict() / from_dict()                      # HA Store serialize/deserialize
```

---

### `optimizer.py` — Battery charge/discharge schedule

Pure function. Greedy pair-matching algorithm over the Nordpool price window.

```python
optimize_schedule(
    tariffs: list[TariffResult],
    solar_forecast: list[SolarForecast],
    battery_config: BatteryConfig,
    battery_state: BatteryState,
    degradation_cost: float,
    now: datetime,
) → Schedule
```

**Algorithm:** Rank all `(buy_hour, sell_hour)` pairs by `profit_per_kwh`. Greedily assign feasible pairs while forward-simulating SoC bounds. Retry at half-power if bounds violated. Unassigned hours → IDLE or solar pass-through.

---

### `ev_scheduler.py` — EV charge scheduling

Pure function. Cheapest-hours selection within departure window.

```python
schedule_ev_charge(
    tariffs: list[TariffResult],
    ev_config: EVChargerConfig,
    ev_state: EVChargerState,
    now: datetime,
) → EVSchedule
```

Selects cheapest N hours before departure. Last slot gets fractional energy to hit exactly `target_soc`.

---

### `inverter.py` — Inverter platform abstraction

HA-dependent. Dispatches generic commands to platform-specific HA service calls.

```python
class InverterController(hass, platform: InverterPlatform, entity_ids, battery_config):
    # Commands (async — HA service calls):
    async_charge_from_grid(power_kw) → None
    async_discharge_to_grid(power_kw) → None
    async_idle() → None

    # State reads (sync — HA state machine):
    get_battery_soc() → float       # 0.0–1.0
    get_battery_power() → float     # kW, positive = charging
    get_grid_power() → float        # kW, positive = importing

# Supported platforms:
InverterPlatform: SOLIS | HUAWEI_SOLAR | SOLAREDGE | GOODWE | GENERIC
```

Solis uses TOU time-slot registers + current setpoints. Others use a charge-mode entity.

---

### `ev_charger.py` — EV charger platform abstraction

HA-dependent. Same pattern as `inverter.py`.

```python
class EVChargerController(hass, platform: EVChargerPlatform, entity_ids):
    async_start_charging(power_kw) → None
    async_stop_charging() → None
    is_plugged_in() → bool
    get_ev_soc() → float | None

# Supported platforms:
EVChargerPlatform: OPENEVSE | EASEE | WALLBOX | GENERIC
```

---

### `coordinator.py` — Central orchestrator (5-min cycle)

```
_async_update_data() flow:

  1. READ HA state
       _read_nordpool_prices()   → list[HourlyPrice]    (new/legacy/15-min formats)
       _read_solar_forecast()    → list[SolarForecast]   (Open Meteo watts or Forecast.Solar)
       inverter.get_battery_*()  → BatteryState
       ev_charger.is_plugged_in/get_ev_soc()

  2. COMPUTE (pure Python)
       tariff.compute_tariffs()              → list[TariffResult]
       battery.degradation_cost_per_kwh()   → float
       optimizer.optimize_schedule()         → Schedule
       ev_scheduler.schedule_ev_charge()    → EVSchedule  (if EV enabled + plugged)

  3. EXECUTE (if automation_enabled, deduped by action_key)
       _execute_current_action()    → inverter commands
       _execute_current_ev_action() → ev_charger commands

  4. LEARN
       _build_capacity_observation()
       capacity_estimator.add_observation()

  5. VISUALIZE
       dashboard.build_future_slots()
       dashboard.build_solar_frozen_forecast()

  Returns: coordinator.data dict consumed by all sensor/switch entities
```

**Key properties:**
- `automation_enabled: bool` — master on/off, written by `switch.py`
- `battery_config: BatteryConfig | None`
- `tariff_config: TariffConfig | None`
- `last_dispatched_action: str | None`
- `last_dispatched_at: datetime | None`

---

### `sensor.py` — 13 HA sensor entities

All inherit `_BaseSensor → CoordinatorEntity`. All read from `coordinator.data`.

| Sensor | State | Key Attribute |
|---|---|---|
| `CurrentActionSensor` | `idle` / `charge_from_grid` / … | — |
| `NextActionSensor` | next action string | — |
| `NextActionTimeSensor` | ISO datetime | — |
| `ExpectedProfitSensor` | EUR float | — |
| `DegradationCostSensor` | EUR/kWh | — |
| `EstimatedCapacitySensor` | kWh | — |
| `CurrentBuyPriceSensor` | EUR/kWh | — |
| `CurrentSellPriceSensor` | EUR/kWh | — |
| `EVChargingSensor` | on/off | — |
| `EVChargeCostSensor` | EUR | — |
| `ScheduleSensor` | current action | full `Schedule` in attrs |
| `InverterModeSensor` | mode label | — |
| `DashboardSensor` | slot count | 15-min slots + frozen solar |

---

### `switch.py` — Master automation toggle

Single `AutomationSwitch`. Persists state via `RestoreEntity`. Defaults OFF on first install. Writes `coordinator.automation_enabled`.

---

### `dashboard.py` — 15-min visualization builder

Reads raw Nordpool 15-min prices + Open Meteo watts. Projects SoC forward slot-by-slot. Derives `inverter_mode` (`self_use`, `sell_discharge`, `charge_from_grid`, …) and `grid_operation` (`+import` / `-export`).

```python
build_future_slots(hass, config, coordinator_data, battery_config, tariff_config) → list[dict]
  # [{t_ms, buy_price, sell_price, solar_forecast_w, battery_soc_pct, inverter_mode, grid_operation}]
  # 15-min slots from now to end of tomorrow

build_solar_frozen_forecast(hass, config) → list[dict]
  # [{t_ms, forecast_w}] for today only (mismatch overlay)
```

---

### `config_flow.py` — 5-step UI configuration

Steps: **tariff → battery → inverter platform → inverter entities (Solis or generic path) → EV → sources**

Options flow re-exposes the same steps for post-install reconfiguration. Schema builder helpers (`_tariff_schema`, `_battery_schema`, …) are shared between both flows.

---

### `debug_view.py` — HTTP diagnostic endpoint

`GET /api/sun_sale/debug` → JSON dump of all coordinator states including config, last inputs, computed schedule, and last dispatch. Registered once in `__init__.py`.

---

### `__init__.py` — Integration entry point

```python
async_setup_entry(hass, entry):
    1. Create SunSaleCoordinator
    2. async_config_entry_first_refresh()
    3. Forward setup to ["sensor", "switch"] platforms
    4. Register SunSaleDebugView (once)
    5. Register static www/ paths (once)
    6. Register custom panel (once)
    7. Register "sun_sale.force_recalculate" service

async_unload_entry(hass, entry)
async_reload_entry(hass, entry)   # handles options changes
```

---

## Coordinator Data Dict (`coordinator.data`)

```python
{
    "schedule":               Schedule,
    "ev_schedule":            EVSchedule | None,
    "tariffs":                list[TariffResult],
    "battery_state":          BatteryState,
    "degradation_cost":       float,
    "estimated_capacity":     float,
    "prices":                 list[HourlyPrice],
    "solar_forecast":         list[SolarForecast],
    "grid_power_kw":          float,
    "battery_power_kw":       float,
    "ev_state":               EVChargerState | None,
    "dashboard_slots":        list[dict],
    "solar_frozen_forecast":  list[dict],
}
```

---

## Import Dependency Graph

```
config_flow ──────────────────────────────────────▶ const, models
__init__   ──────┬──────────────────────────────▶ coordinator, debug_view
                 │
coordinator ─────┼──▶ tariff, battery, optimizer, ev_scheduler  (pure)
                 ├──▶ inverter, ev_charger                        (HA)
                 ├──▶ dashboard                                    (HA)
                 └──▶ models, const

sensor     ─────────▶ coordinator (CoordinatorEntity)
switch     ─────────▶ coordinator

tariff     ─────────▶ models
battery    ─────────▶ models
optimizer  ─────────▶ models, tariff, battery
ev_scheduler ───────▶ models
inverter   ─────────▶ models, const
ev_charger ─────────▶ models, const
dashboard  ─────────▶ models, const, tariff
```

---

## Test Coverage

| Test file | Covers |
|---|---|
| `test_battery.py` | `degradation_cost_per_kwh`, `trade_profit_per_kwh`, `CapacityEstimator` |
| `test_coordinator.py` | `_read_nordpool_prices`, `_read_solar_forecast` |
| `test_config_flow.py` | All config flow steps |
| `test_debug_view.py` | Debug HTTP view serialization |
| `test_ev_charger.py` | EVChargerController |
| `test_ev_scheduler.py` | EV charge scheduling algorithm |
| `test_init.py` | Integration setup/unload |

`conftest.py` mocks all HA imports and provides shared fixtures: `make_price()`, `make_solar()`, `default_tariff_config()`, `default_battery_config()`, `default_battery_state()`, `default_ev_config()`.
