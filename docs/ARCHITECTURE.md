# sunSale Architecture

End-to-end description of the tiered observer DAG that drives every sunSale update cycle.

| | |
|---|---|
| **Status** | Implemented |
| **Pattern** | Translation layer + inbound normalisers + tiered observer DAG + post-DAG inverter control module |
| **HA boundary** | HA imports confined to root entry points (`__init__.py`, `config_flow.py`, `sensor.py`, `switch.py`), `orchestration/`, and `outbound/inverter.py`. Inbound translators (`inbound/*.py`) do not import HA — each accepts a duck-typed `hass` at runtime to read `hass.states`. All other modules are pure Python. |

## Contents

1. [Overview](#1-overview)
2. [Package layout](#2-package-layout)
3. [Layer 1 — Translation (inbound)](#3-layer-1--translation-inbound)
4. [Layer 2 — DAG engine and nodes (pipeline)](#4-layer-2--dag-engine-and-nodes-pipeline)
5. [Layer 3 — Event router and output adapters (outbound)](#5-layer-3--event-router-and-output-adapters-outbound)
6. [Orchestration and update cycle](#6-orchestration-and-update-cycle)
7. [Node reference](#7-node-reference)
8. [Data contracts](#8-data-contracts)
9. [Key design decisions](#9-key-design-decisions)

---

## 1. Overview

```
HA state machine
      │  (read once per cycle, parallel)
      ▼
┌─────────────────────────────────────────────────────────────┐
│  Translation Layer        inbound/*.py                      │
│                                                             │
│  NordpoolTranslator      →  NordpoolData                    │
│  SolarTranslator         →  SolarData                       │
│  BatteryTranslator       →  BatteryReading                  │
│  GenerationTranslator    →  GenerationReading               │
│  PvPowerTranslator       →  PvPowerReading                  │
│  GridObserver / *Total   →  GridPowerReading / *TodayReading│
│  HouseholdLoad / *Cons.  →  HouseholdLoad/Consumption       │
│  InverterModeTranslator  →  InverterModeReading             │
└─────────────────────────────────────────────────────────────┘
      │  typed primary data (dict[type, Any])
      │  + YesterdayPrices, EstimatedCapacity, PriceHistory,
      │    *History primaries (injected by coordinator)
      ▼
┌─────────────────────────────────────────────────────────────┐
│  DAG Engine               pipeline/dag_engine.py            │
│  DAG Nodes                pipeline/nodes/tier{1..4}.py      │
│                                                             │
│  T1  PricingNode     BatteryStateNode    BatteryStatusNode  │
│      BaseLoadProfileNode                                    │
│  T2  GenerationNode  ObservedGenerationNode  ObservedGridNode│
│      DegradationNode BatteryRuntimeNode  ProfitabilityNode  │
│  T3  ChargingProfileNode  LockoutNode                       │
│      ForecastAccuracyNode  MonthlyBillNode                  │
│  T4  ScheduleNode                                           │
│                                                             │
│  Pure-Python helpers used by nodes:                         │
│    inbound/pricing.py        (PriceSeries assembly)         │
│    inbound/forecast.py       (GenerationSeries assembly)    │
│    inbound/grid.py           (ObservedGridSeries assembly)  │
│    pipeline/tariff.py        (buy/sell formula)             │
│    pipeline/battery.py       (capacity + degradation)       │
│    pipeline/calculation.py   (lockout windows)              │
│    pipeline/charging_profile.py (solar disposition)         │
│    pipeline/schedule.py      (greedy pair-match)            │
│    pipeline/storage_mode_specs.py (planner ↔ StorageMode)   │
│    pipeline/profitability.py (rolling daily-peak percentile)│
│    pipeline/monthly_bill.py  (running bill = carry + live)  │
│    pipeline/base_load.py     (P10 baseload profile)         │
│    pipeline/forecast_accuracy.py (MAE/bias/MAPE + EMAs)     │
└─────────────────────────────────────────────────────────────┘
      │  Schedule (with StorageMode per slot)
      ▼
┌─────────────────────────────────────────────────────────────┐
│  InverterControlModule    outbound/inverter_control_module  │
│  → InverterController     outbound/inverter.py              │
│  (observe → plan → act; act gated by automation switch)     │
└─────────────────────────────────────────────────────────────┘
```

The coordinator (`orchestration/coordinator.py`) owns the update schedule, feeds the three layers in order, manages all persistent stores via `orchestration/persistent_store.py`, and collects results into `coordinator.data` for sensor entities.

---

## 2. Package layout

```
custom_components/sun_sale/
├── __init__.py                  HA entry point (panel, debug view, service)
├── config_flow.py               Config + options flow
├── sensor.py                    HA sensor entities
├── switch.py                    Automation-enable switch
│
├── contract/                    Pure data types — no logic, no imports of other layers
│   ├── const.py                 Storage keys, conf keys, defaults
│   ├── events.py                ControlEvent / InverterActionEvent
│   └── models.py                All dataclasses (configs, primary types, secondary types)
│
├── inbound/                     HA-read translators + pure-Python normalisers
│   ├── pricing.py               NordpoolTranslator + 72h PriceSeries assembly
│   ├── forecast.py              SolarTranslator + GenerationSeries assembly
│   ├── generation.py            GenerationTranslator + PvPowerTranslator + ObservedGenerationSeries
│   ├── battery.py               BatteryTranslator + BatteryStatus assembly
│   ├── grid.py                  GridObserver + import/export totals + ObservedGridSeries
│   ├── household_load.py        HouseholdLoadTranslator (instantaneous kW)
│   ├── household_consumption.py HouseholdConsumptionTranslator (today-total kWh)
│   ├── inverter_mode.py         InverterModeTranslator (decoded StorageMode)
│   └── solis_entity_resolver.py Auto-resolve solis_modbus entity IDs from the registry
│
├── pipeline/                    Pure-Python DAG engine + node logic
│   ├── dag_engine.py            DagNode, DagEngine, NodeContext, run_translators
│   ├── nodes/                   DAG nodes split by execution tier
│   │   ├── tier1.py             Pricing / BatteryState / BatteryStatus / BaseLoadProfile
│   │   ├── tier2.py             Generation / ObservedGeneration / ObservedGrid /
│   │   │                        Degradation / BatteryRuntime / Profitability
│   │   ├── tier3.py             ChargingProfile / Lockout / ForecastAccuracy / MonthlyBill
│   │   └── tier4.py             ScheduleNode
│   ├── tariff.py                buy_price / sell_price
│   ├── battery.py               CapacityEstimator, degradation_cost_per_kwh
│   ├── calculation.py           Lockout windows, slot decisions
│   ├── charging_profile.py      Per-slot solar disposition (battery/sell/no-export)
│   ├── schedule.py              Greedy pair-match schedule → StorageMode per slot
│   ├── storage_mode_specs.py    PlannerDecision / build_specs / decode_mode / select_mode
│   ├── profitability.py         Rolling 30d daily-peak percentile (day-class normalised)
│   ├── monthly_bill.py          Running monthly bill = persistent carry + live slot costs
│   ├── base_load.py             24h P10 baseload profile + battery runtime estimate
│   └── forecast_accuracy.py     Forecast vs observed deltas + EMA quality buckets
│
├── outbound/                    HA-write adapters + post-DAG actuation
│   ├── inverter.py              InverterController (Solis V2 + generic), normalize_power_to_kw
│   └── inverter_control_module.py  Observer + dispatcher: observe → plan → act per cycle
│
└── orchestration/               Glue: schedule, persistence, sensor dict mapping
    ├── coordinator.py           SunSaleCoordinator (DataUpdateCoordinator subclass)
    ├── persistent_store.py      PersistentStore[T] — typed wrapper around HA Store
    └── debug_view.py            HTTP view exposing cycle inputs/outputs as JSON
```

Layering rule: `contract` imports nothing from the integration. `inbound`/`pipeline`/`outbound` import only `contract`. `orchestration` may import from all four. Violations surface as circular-import errors.

---

## 3. Layer 1 — Translation (inbound)

**Files:** `inbound/{pricing,forecast,generation,battery,grid,household_load,household_consumption,inverter_mode,solis_entity_resolver}.py`

Translators are the primary HA-state readers. The only other module that touches `hass.states` is `outbound/inverter.py`, which reads live device telemetry (SoC, register state, etc.) on demand from its controller methods. All remaining modules are pure Python.

Each translator lives in its own module — there is no monolithic `translators.py`. Translator modules themselves do **not** import the `homeassistant` package — they accept `hass: Any` and call methods on it at runtime, which keeps them unit-testable without an HA harness.

Each translator has a synchronous `.parse(hass, now)` method (testable without HA) and an asynchronous `.translate(hass, config, raw_config, now)` wrapper called by the coordinator. Each declares an `output_type` class attribute used by `run_translators` to key the primary dict.

| Translator | Output type | Source |
|---|---|---|
| `NordpoolTranslator` | `NordpoolData` | Nordpool sensor — prefers `raw_today`/`raw_tomorrow` (timestamped slots, 15-min or hourly), falls back to legacy flat `today`/`tomorrow` arrays. Resolution is auto-detected from slot stride. Tomorrow is zero-filled until the day-ahead market publishes. |
| `SolarTranslator` | `SolarData` | Open-Meteo `watts` dict preferred; Forecast.Solar / Solcast `forecast` attribute as fallback. Combines today + tomorrow entities and multi-panel sources by summing at matching timestamps. |
| `BatteryTranslator` | `BatteryReading` | `InverterController` for SoC/power/grid; HA state for household load sensor (with default fallback). |

All translators run in parallel via `asyncio.gather` (`run_translators` in `pipeline/dag_engine.py`) before the DAG starts.

### Inbound normalisers (pure Python, no HA)

Two more files in `inbound/` are not translators — they are pure assembly functions called by DAG nodes:

- **`inbound/pricing.py`** — `build_price_series(prices, config, now, resolution=None)` applies the tariff formula to a flat list of `PriceEntry`. `build_price_series_72h(nordpool, yesterday, config, now)` is the function `PricingNode` calls: it combines `YesterdayPrices.entries + NordpoolData.entries` and uses `NordpoolData.resolution` as the source of truth.
- **`inbound/forecast.py`** — `build_generation_series(solar, price_series, now)` is the function `GenerationNode` calls: it resamples `SolarData.entries` onto the `PriceSeries` grid (1:1, zero-filled where solar is absent) to produce a continuous 72h `GenerationSeries`, and computes per-day totals plus `today_remaining_kwh`. Yesterday entries are prepended in the coordinator (same pattern as pricing) and are invisible to downstream consumers.
- **`inbound/battery.py`** — `build_battery_status(reading, config)` is the function `BatteryStatusNode` calls: it combines configured nominal capacity and charge/discharge power limits with the observed SoC into an immutable `BatteryStatus`, and derives `remaining_capacity_kwh = soc * total_capacity_kwh`.

See `docs/MODULES.md` for per-module reference (description, exposed type, dependencies, test coverage).

These live in `inbound/` because they normalise translator output into the shape downstream nodes consume.

---

## 4. Layer 2 — DAG engine and nodes (pipeline)

**Files:** `pipeline/dag_engine.py`, `pipeline/nodes/tier{1,2,3,4}.py` (re-exported through `pipeline/nodes/__init__.py`)

### DagNode contract

Every node declares three class attributes:

```python
class SomeNode(DagNode):
    tier: int              # 1–5; controls execution order and observer wiring
    output_type: type      # the type this node deposits into NodeContext.secondary
    consumes: list[type]   # primary or secondary types this node needs
```

The engine calls `_wire()` once at construction. For each `(consumer, dependency_type)` pair where the dependency is produced by another node, it calls `producer.add_observer(consumer)`. `add_observer` raises `TierViolationError` if `consumer.tier <= producer.tier`, enforcing the DAG's acyclicity at startup time.

### Observer notification

After `_compute()` returns, `run()` deposits the result in `ctx.secondary[output_type]` and calls `_notify_observers()`, which records the satisfied dependency on each observer. A node is "ready" for a tier when every type in `consumes` is either already in `ctx.primary` or has been marked satisfied via observer notification.

### Tier execution

```python
for tier_num in sorted(nodes_by_tier):
    ready = [n for n in nodes_by_tier[tier_num] if n.all_secondary_deps_satisfied(ctx)]
    results = await asyncio.gather(*[n.run(ctx) for n in ready])
```

All ready nodes in a tier run concurrently. Primary data (translator outputs plus coordinator-injected `YesterdayPrices` and `EstimatedCapacity`) is always pre-populated and never blocks readiness checks.

### NodeContext

```python
@dataclass
class NodeContext:
    primary:   dict[type, Any]    # translator + coordinator-injected primary data
    secondary: dict[type, Any]    # node outputs — accumulated tier by tier
    config:    SunSaleConfig      # tariff + battery config
    now:       datetime

    def require(self, t: type) -> Any   # raises MissingDependencyError if absent
    def get(self, t: type) -> Any | None  # returns None if absent (optional deps)
```

---

## 5. Layer 3 — Inverter control module and output adapters (outbound)

**Files:** `outbound/inverter_control_module.py`, `outbound/inverter.py`

Dispatch is **not** event-driven. Pipeline nodes currently return empty `ControlEvent` lists — the `Schedule` they produce already encodes the target `StorageMode` per slot. After the DAG run, the coordinator calls `InverterControlModule.tick(...)` once per cycle. It does three things in fixed order:

1. **Observe.** Compare this cycle's `InverterModeReading` against the last entry of the rolling `InverterModeHistory`. If the decoded mode changed, append a new `InverterModeChange` and prune samples older than start-of-yesterday (local time).
2. **Plan.** Look up the current `Schedule` slot, resolve its target mode into a concrete `StorageModeSpec` via `pipeline.storage_mode_specs.build_specs`.
3. **Act (conditional).** When `automation_enabled` is True, call `InverterController.apply_mode(target, spec)`. With the switch off (default), the module is observer-only — history grows and the plan is exposed, but no Modbus writes happen.

The `InverterController` contains all platform-specific HA service-call logic and is the only outbound writer to HA.

> Historical note: the older `outbound/event_router.py` and the `_LastActionRef`/`_last_inverter_key` two-layer dedup it implemented are gone. The `ControlEvent` / `InverterActionEvent` types in `contract/events.py` are retained for forward use; today they flow through the pipeline as empty lists.

---

## 6. Orchestration and update cycle

**Files:** `orchestration/coordinator.py`, `orchestration/persistent_store.py`, `orchestration/debug_view.py`

`SunSaleCoordinator` is a `DataUpdateCoordinator` subclass with `update_interval = UPDATE_INTERVAL_MINUTES`.

`async_setup()` (once at integration load):

1. Build `TariffConfig`, `BatteryConfig`, `SunSaleConfig` from the config entry.
2. Instantiate `InverterController` (Solis V2 or generic).
3. Build translator list, DAG node list, `DagEngine`, `InverterControlModule`.
4. Load all `PersistentStore[T]` instances (capacity, yesterday prices, generation/pv-power history, household-load history, grid power & today-totals, price-peak history, inverter-mode history, monthly-bill state, forecast-quality EMAs).

`_async_update_data()` (every cycle):

1. **Translate** — `run_translators(translators, hass, config, raw_config, now)` runs all translators in parallel; collect `primary` dict.
2. **Inject coordinator primaries** — append per-cycle observations (battery, household load, grid power, generation, pv power, inverter mode) into their respective `PersistentStore[T]`s with trim, then deposit history primaries (`HouseholdLoadHistory`, `GridPowerHistory`, `GridImport/ExportTodayHistory`, `GenerationHistory`, `PvPowerHistory`, `PriceHistory`, …) plus `YesterdayPrices` and `EstimatedCapacity` into `primary`. This is what gives the pipeline a full picture without the nodes touching persistence.
3. **Stitch yesterday solar** — still done by mutating `SolarData.entries` in place (legacy path; counterpart for solar of what pricing owns explicitly via `YesterdayPrices`).
4. **Persist today** — extract today's slots from `NordpoolData.entries` / `SolarData.entries` and write them to the yesterday store so the next cycle can read them as yesterday. End-of-day, append today's `DailyPeak` to the price-history store.
5. **Capacity estimation** — derive a `CapacityObservation` from the SoC delta vs `_last_battery_reading`; add it to the `CapacityEstimator` and persist. Inject `EstimatedCapacity` into `primary`.
6. **DAG run** — `DagEngine.run(primary, config, now)` executes tiers T1→T4; returns `secondary` dict (event lists are currently always empty).
7. **Inverter control tick** — call `InverterControlModule.tick(...)`. Observes the current `InverterModeReading` against history, looks up the current `Schedule` slot, and (only when `automation_enabled`) applies the resolved `StorageModeSpec` via `InverterController.apply_mode`.
8. **Build sensor dict** — map type-keyed `secondary` entries to the string-keyed dict sensors read (`"pricing"`, `"forecast"`, `"calculation"`, `"schedule"`, `"battery_state"`, `"battery_status"`, `"battery_runtime"`, `"degradation_cost"`, `"estimated_capacity"`, `"profitability"`, `"observed_generation"`, `"observed_grid"`, `"forecast_accuracy"`, `"monthly_bill"`, `"prices"`, `"grid_power_kw"`, `"battery_power_kw"`, `"household_load_kw"`, …).

The coordinator contains no domain computation — it owns the schedule, the persistent stores, and the string↔type bridge to sensors.

`debug_view.py` registers an HTTP view at `/api/sun_sale/debug` exposing the most recent `primary` / `secondary` / inputs as JSON for the panel UI and `tools/integration_check.py`.

---

## 7. Node reference

| Node | Tier | Consumes | Produces |
|---|---|---|---|
| `PricingNode` | 1 | `NordpoolData`, `YesterdayPrices` | `PriceSeries` (72h) |
| `BatteryStateNode` | 1 | `BatteryReading`, `EstimatedCapacity` | `BatteryState` |
| `BatteryStatusNode` | 1 | `BatteryReading` | `BatteryStatus` |
| `BaseLoadProfileNode` | 1 | `HouseholdLoadHistory` | `BaseLoadProfile` |
| `GenerationNode` | 2 | `SolarData`, `PriceSeries` | `GenerationSeries` |
| `ObservedGenerationNode` | 2 | `PvPowerHistory`, `GenerationHistory`, `PriceSeries` | `ObservedGenerationSeries` |
| `DegradationNode` | 2 | `BatteryState` | `DegradationCost` |
| `BatteryRuntimeNode` | 2 | `BatteryStatus`, `BaseLoadProfile` | `BatteryRuntimeEstimate` |
| `ObservedGridNode` | 2 | `GridPowerHistory`, `GridImportTodayHistory`, `GridExportTodayHistory`, `PriceSeries` | `ObservedGridSeries` |
| `ProfitabilityNode` | 2 | `PriceSeries`, `PriceHistory` | `ProfitabilityScore` |
| `ChargingProfileNode` | 3 | `BatteryStatus`, `GenerationSeries`, `PriceSeries` | `ChargingProfile` |
| `ForecastAccuracyNode` | 3 | `GenerationSeries`, `ObservedGenerationSeries` | `ForecastAccuracyResult` |
| `MonthlyBillNode` | 3 | `PriceSeries`, `ObservedGridSeries` | `MonthlyBillResult` |
| `LockoutNode` | 3 | `PriceSeries`, `GenerationSeries`, `BatteryState` | `CalculationResult` |
| `ScheduleNode` | 4 | `PriceSeries`, `CalculationResult`, `GenerationSeries`, `BatteryState`, `DegradationCost`, `ChargingProfile` | `Schedule` |

All nodes currently return empty event lists — dispatch is handled by `InverterControlModule.tick()` after the DAG run, using the `Schedule`'s per-slot `StorageMode`.

---

## 8. Data contracts

All types are dataclasses in `contract/models.py`. Frozen unless they're mutated in place by the coordinator.

### Primary data (in `NodeContext.primary`)

| Type | Origin | Key fields |
|---|---|---|
| `NordpoolData` | `NordpoolTranslator` | `entries: list[PriceEntry]` (today + tomorrow, sorted), `resolution: timedelta` |
| `YesterdayPrices` | Coordinator (from `STORAGE_KEY_YESTERDAY`) | `entries: tuple[PriceEntry, ...]` (empty when stored date ≠ yesterday) |
| `SolarData` | `SolarTranslator` | `entries: list[SolarEntry]`, `total_today_kwh: float`, `today_remaining_kwh: float`, `primary_source: str` |
| `BatteryReading` | `BatteryTranslator` | `soc`, `power_kw`, `grid_power_kw`, `household_load_kw` |
| `EstimatedCapacity` | Coordinator (from `CapacityEstimator`) | `value_kwh: float` |

### Secondary data (in `NodeContext.secondary`)

| Type | Produced by | Key fields |
|---|---|---|
| `PriceSeries` | `PricingNode` | `slots: tuple[PriceSlot, ...]` (72h), `resolution: timedelta`, `computed_at: datetime`; helpers `slot_at(t)`, `window(t1, t2)` |
| `BatteryState` | `BatteryStateNode` | `soc: float`, `estimated_capacity_kwh: float` |
| `BatteryStatus` | `BatteryStatusNode` | `total_capacity_kwh`, `max_charge_power_kw`, `max_discharge_power_kw`, `soc`, `remaining_capacity_kwh` |
| `GenerationSeries` | `GenerationNode` | `slots: tuple[GenerationSlot, ...]`, `primary: str`, `overlays: tuple[str, ...]`, `computed_at` |
| `DegradationCost` | `DegradationNode` | `value_kwh: float` |
| `CalculationResult` | `LockoutNode` | `slots: tuple[SlotDecision, ...]`, `feed_in_lockout_windows`, `total_negative_sale_kwh`, `computed_at` |
| `ChargingProfile` | `ChargingProfileNode` | `slots: tuple[ChargingProfileSlot, ...]` (today's remaining; `mode ∈ {solar_charge, sell, no_export, idle}`), `free_capacity_kwh`, `today_remaining_generation_kwh`, `solar_exceeds_capacity`, `allocated_solar_kwh`, `total_no_export_kwh`, `computed_at` |
| `Schedule` | `ScheduleNode` | `slots: list[ScheduleSlot]` (each carries target `StorageMode`), `total_expected_profit_eur`, `degradation_cost_per_kwh`, `computed_at` |
| `ObservedGenerationSeries` | `ObservedGenerationNode` | per-slot kWh from differenced today-total + averaged PV power; end-of-day counter correction |
| `ObservedGridSeries` | `ObservedGridNode` | per-slot gross `import_kwh` / `export_kwh` (split per sample), `today_import_total`, `today_export_total` |
| `BatteryRuntimeEstimate` | `BatteryRuntimeNode` | hours of household runtime from current SoC against the P10 baseload profile |
| `BaseLoadProfile` | `BaseLoadProfileNode` | 24 hourly P10 kW buckets derived from the household-load history |
| `ProfitabilityScore` | `ProfitabilityNode` | `score: float ∈ [0, 1]` (None until ≥ MIN_HISTORY_DAYS samples), `today_peak_eur_kwh`, `day_class`, window |
| `ForecastAccuracyResult` | `ForecastAccuracyNode` | `errors: ForecastErrorSeries`, EMA buckets (intensity / position / time-of-day) |
| `MonthlyBillResult` | `MonthlyBillNode` | `live_slots`, `carry_eur`, `running_total_eur`, `previous_month_total_eur` |

`PriceSlot` carries `buy_eur_kwh`, `sell_eur_kwh` (can be negative or zero), `spot_eur_kwh`, and `sources: tuple[str, ...]` for diagnostics. Sellability (the strict `> 0` check) lives downstream in the charging-profile stage.

---

## 9. Key design decisions

**HA boundary.** HA imports are confined to the root entry points (`__init__.py`, `config_flow.py`, `sensor.py`, `switch.py`), `orchestration/coordinator.py`, `orchestration/persistent_store.py`, `orchestration/debug_view.py`, and the outbound controller (`outbound/inverter.py`). Inbound translators read `hass.states` but never import the `homeassistant` package — they work on duck-typed `hass`. Every other module is pure Python and testable with plain `pytest` without an HA harness.

**Sub-package layering enforces direction.** `contract` depends on nothing. `inbound` / `pipeline` / `outbound` depend only on `contract`. `orchestration` is the only layer allowed to glue them. This is enforced by convention (and would surface as a circular import if violated).

**Inbound owns the 72h pricing assembly.** The Nordpool translator emits today + tomorrow only; the coordinator supplies yesterday from a persistent store as `YesterdayPrices`; `inbound/pricing.build_price_series_72h` combines them and applies the tariff. Downstream consumers can rely on `PriceSeries` covering yesterday→today→tomorrow when yesterday data is available.

**Tier constraint enforced at wire-time.** `add_observer()` raises `TierViolationError` if `observer.tier <= subject.tier`. This catches dependency graph mistakes during integration startup, not at runtime.

**Events vs. return values (legacy contract).** `DagNode._compute` still returns `(result, list[ControlEvent])` so the type signature accommodates side-effect emission. In the current code every node returns an empty event list — actuation moved into the post-DAG `InverterControlModule`, which reads the `Schedule`'s per-slot `StorageMode` directly. The event-tuple shape is kept for forward use without forcing a contract change if a future node needs to emit something orthogonal to the schedule.

**Post-DAG dispatch with idempotent application.** `InverterControlModule.tick()` is called once per cycle after the DAG. The `InverterController.apply_mode(mode, spec)` implementation is idempotent — applying the same target `StorageMode` twice is a no-op at the inverter level (Modbus writes are skipped when the readback already matches). This replaces the older `OptimizerNode` / `EventRouter` two-layer dedup with a simpler "apply the plan every cycle, let the controller short-circuit" pattern.

**Coordinator-injected primary data.** `YesterdayPrices`, `EstimatedCapacity`, and every `*History` primary are not translator outputs — they are stateful values the coordinator owns across cycles (loaded from `PersistentStore[T]`, appended-and-trimmed each cycle) and deposited into `primary` before `DagEngine.run()`. Treating them as primary data keeps DAG nodes stateless and the engine free of cross-cycle state.
