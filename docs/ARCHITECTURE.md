# sunSale Architecture

End-to-end description of the tiered observer DAG that drives every sunSale update cycle.

| | |
|---|---|
| **Status** | Implemented |
| **Pattern** | Translation layer + inbound normalisers + tiered observer DAG + event router |
| **HA boundary** | HA imports confined to root entry points (`__init__.py`, `config_flow.py`, `sensor.py`, `switch.py`), `orchestration/`, and `outbound/inverter.py` + `outbound/ev_charger.py`. `inbound/translators.py` does not import HA but receives `hass` at runtime to read `hass.states`. All other modules are pure Python. |

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
┌─────────────────────────────────────────────────────────┐
│  Translation Layer        inbound/translators.py        │
│                                                         │
│  NordpoolTranslator   →  NordpoolData                   │
│  SolarTranslator      →  SolarData                      │
│  BatteryTranslator    →  BatteryReading                 │
│  EVTranslator         →  EVChargerState (EV optional)   │
└─────────────────────────────────────────────────────────┘
      │  typed primary data (dict[type, Any])
      │  + YesterdayPrices, EstimatedCapacity (injected by coordinator)
      ▼
┌─────────────────────────────────────────────────────────┐
│  DAG Engine               pipeline/dag_engine.py        │
│                                                         │
│  T1  PricingNode      BatteryStateNode                  │
│  T2  GenerationNode   DegradationNode                   │
│      EVSchedulerNode  (EV optional)                     │
│  T3  LockoutNode      ChargingProfileNode               │
│  T4  OptimizerNode                                      │
│  T5  DashboardNode                                      │
│                                                         │
│  Pure-Python helpers used by nodes:                     │
│    inbound/pricing.py     (PriceSeries assembly)        │
│    inbound/forecast.py    (GenerationSeries assembly)   │
│    pipeline/tariff.py     (buy/sell formula)            │
│    pipeline/battery.py    (capacity + degradation)      │
│    pipeline/calculator.py        (lockout windows)      │
│    pipeline/charging_profile.py  (solar disposition)    │
│    pipeline/optimizer.py         (greedy pair-match)    │
│    pipeline/ev_scheduler.py                             │
└─────────────────────────────────────────────────────────┘
      │  ControlEvents
      ▼
┌─────────────────────────────────────────────────────────┐
│  Event Router             outbound/event_router.py      │
│  → InverterController     outbound/inverter.py          │
│  → EVChargerController    outbound/ev_charger.py        │
└─────────────────────────────────────────────────────────┘
```

The coordinator (`orchestration/coordinator.py`) owns the update schedule, feeds the three layers in order, manages the persistent yesterday/capacity stores, and collects results into `coordinator.data` for sensor entities.

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
│   ├── events.py                ControlEvent / InverterActionEvent / EVActionEvent
│   └── models.py                All dataclasses (configs, primary types, secondary types)
│
├── inbound/                     HA-read translators + pure-Python normalisers
│   ├── translators.py           Reads hass.states → primary types
│   ├── pricing.py               build_price_series / build_price_series_72h
│   ├── forecast.py              build_generation_series
│   └── battery.py               build_battery_status
│
├── pipeline/                    Pure-Python DAG engine + node logic
│   ├── dag_engine.py            DagNode, DagEngine, NodeContext, run_translators
│   ├── nodes.py                 All DAG node classes
│   ├── tariff.py                buy_price / sell_price
│   ├── battery.py               CapacityEstimator, degradation_cost_per_kwh
│   ├── calculator.py            Lockout windows, slot decisions
│   ├── charging_profile.py      Per-slot solar disposition (battery/sell/no-export)
│   ├── optimizer.py             Greedy pair-match schedule
│   └── ev_scheduler.py          EV charge plan
│
├── outbound/                    HA-write adapters + event routing
│   ├── event_router.py          Deduplicates and routes ControlEvents
│   ├── inverter.py              InverterController (Solis + abstract)
│   ├── ev_charger.py            EVChargerController
│   └── dashboard.py             Build future_slots / frozen forecast (pure)
│
└── orchestration/               Glue: schedule, persistence, sensor dict mapping
    ├── coordinator.py           SunSaleCoordinator (DataUpdateCoordinator subclass)
    └── debug_view.py            HTTP view exposing cycle inputs/outputs as JSON
```

Layering rule: `contract` imports nothing from the integration. `inbound`/`pipeline`/`outbound` import only `contract`. `orchestration` may import from all four. Violations surface as circular-import errors.

---

## 3. Layer 1 — Translation (inbound)

**File:** `inbound/translators.py`

Translators are the primary HA-state readers. The only other modules that touch `hass.states` are `outbound/inverter.py` and `outbound/ev_charger.py`, which read live device telemetry (SoC, plug state, etc.) on demand from their controller methods. All remaining modules are pure Python.

Translator modules themselves do **not** import the `homeassistant` package — they accept `hass: Any` and call methods on it at runtime, which keeps them unit-testable without an HA harness.

Each translator has a synchronous `.parse(hass, now)` method (testable without HA) and an asynchronous `.translate(hass, config, raw_config, now)` wrapper called by the coordinator. Each declares an `output_type` class attribute used by `run_translators` to key the primary dict.

| Translator | Output type | Source |
|---|---|---|
| `NordpoolTranslator` | `NordpoolData` | Nordpool sensor — prefers `raw_today`/`raw_tomorrow` (timestamped slots, 15-min or hourly), falls back to legacy flat `today`/`tomorrow` arrays. Resolution is auto-detected from slot stride. Tomorrow is zero-filled until the day-ahead market publishes. |
| `SolarTranslator` | `SolarData` | Open-Meteo `watts` dict preferred; Forecast.Solar / Solcast `forecast` attribute as fallback. Combines today + tomorrow entities and multi-panel sources by summing at matching timestamps. |
| `BatteryTranslator` | `BatteryReading` | `InverterController` for SoC/power/grid; HA state for household load sensor (with default fallback). |
| `EVTranslator` | `EVChargerState` | `EVChargerController` for plug-state and SoC; HA state for target SoC and departure time. Only registered when `CONF_EV_ENABLED=True`. |

All translators run in parallel via `asyncio.gather` (`run_translators` in `pipeline/dag_engine.py`) before the DAG starts.

### Inbound normalisers (pure Python, no HA)

Two more files in `inbound/` are not translators — they are pure assembly functions called by DAG nodes:

- **`inbound/pricing.py`** — `build_price_series(prices, config, now, resolution=None)` applies the tariff formula to a flat list of `PriceEntry`. `build_price_series_72h(nordpool, yesterday, config, now)` is the function `PricingNode` calls: it combines `YesterdayPrices.entries + NordpoolData.entries` and uses `NordpoolData.resolution` as the source of truth. See `docs/inbound_pricing.md` for the full reference.
- **`inbound/forecast.py`** — `build_generation_series(solar, price_series, now)` is the function `GenerationNode` calls: it resamples `SolarData.entries` onto the `PriceSeries` grid (1:1, zero-filled where solar is absent) to produce a continuous 72h `GenerationSeries`, and computes per-day totals plus `today_remaining_kwh`. Yesterday entries are prepended in the coordinator (same pattern as pricing) and are invisible to downstream consumers. See `docs/inbound_forecast.md` for the full reference.
- **`inbound/battery.py`** — `build_battery_status(reading, config)` is the function `BatteryStatusNode` calls: it combines configured nominal capacity and charge/discharge power limits with the observed SoC into an immutable `BatteryStatus`, and derives `remaining_capacity_kwh = soc * total_capacity_kwh`. See `docs/inbound_battery.md` for the full reference.

These live in `inbound/` because they normalise translator output into the shape downstream nodes consume.

---

## 4. Layer 2 — DAG engine and nodes (pipeline)

**Files:** `pipeline/dag_engine.py`, `pipeline/nodes.py`

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
    config:    SunSaleConfig      # tariff + battery + EV config
    now:       datetime

    def require(self, t: type) -> Any   # raises MissingDependencyError if absent
    def get(self, t: type) -> Any | None  # returns None if absent (optional deps)
```

---

## 5. Layer 3 — Event router and output adapters (outbound)

**Files:** `outbound/event_router.py`, `outbound/inverter.py`, `outbound/ev_charger.py`

Nodes emit `ControlEvent` objects (defined in `contract/events.py`) alongside their computed result. The coordinator collects all events from the DAG run and passes them to `EventRouter.handle()` only when automation is enabled.

```python
@dataclass(frozen=True)
class InverterActionEvent(ControlEvent):
    action: Action       # IDLE | CHARGE_FROM_GRID | DISCHARGE_TO_GRID | CHARGE_FROM_SOLAR
    power_kw: float

@dataclass(frozen=True)
class EVActionEvent(ControlEvent):
    charge_power_kw: float   # 0.0 = stop charging
```

Dedup happens at two layers:

- **Node-side** — `OptimizerNode` and `EVSchedulerNode` each hold a mutable `_LastActionRef` cell that tracks the last action key. They only emit an event when the current-slot action differs from the previous cycle's.
- **Router-side** — `EventRouter` additionally keeps `_last_inverter_key` and re-checks `f"{action}:{power_kw:.3f}"` before calling the inverter controller, so a duplicate inverter command would still be suppressed even if a node emitted one. EV events have only node-side dedup.

The output controllers (`InverterController`, `EVChargerController`) contain all platform-specific HA service-call logic and are the only outbound writers to HA.

`outbound/dashboard.py` is also in this layer but is **pure Python** — it builds presentation dicts for `DashboardNode` from already-typed data and writes nothing.

---

## 6. Orchestration and update cycle

**Files:** `orchestration/coordinator.py`, `orchestration/debug_view.py`

`SunSaleCoordinator` is a `DataUpdateCoordinator` subclass with `update_interval = UPDATE_INTERVAL_MINUTES`.

`async_setup()` (once at integration load):

1. Build `TariffConfig`, `BatteryConfig`, optional `EVChargerConfig` from the config entry.
2. Instantiate `InverterController` (Solis or abstract) and optional `EVChargerController`.
3. Build translator list, DAG node list, `DagEngine`, `EventRouter`.
4. Load `CapacityEstimator` state from `STORAGE_KEY_CAPACITY`.
5. Load yesterday entries (Nordpool + solar) from `STORAGE_KEY_YESTERDAY`.

`_async_update_data()` (every cycle):

1. **Translate** — `run_translators(translators, hass, config, raw_config, now)` runs all translators in parallel; collect `primary` dict.
2. **Inject yesterday pricing** — build `YesterdayPrices(entries=...)` from the persistent store (empty tuple if stored date is not exactly yesterday) and deposit into `primary`. This is what gives `inbound/pricing` ownership of the 72h yesterday→today→tomorrow span.
3. **Stitch yesterday solar** — currently still done by mutating `SolarData.entries` in place (legacy path; counterpart for solar of what pricing now owns explicitly).
4. **Persist today** — extract today's slots from `NordpoolData.entries` and `SolarData.entries` and write them to `STORAGE_KEY_YESTERDAY` so the next cycle can read them as yesterday.
5. **Capacity estimation** — derive a `CapacityObservation` from the SoC delta vs. `_last_battery_reading`; add it to the `CapacityEstimator` and persist. Inject `EstimatedCapacity` into `primary`.
6. **DAG run** — `DagEngine.run(primary, config, now)` executes tiers T1→T5; returns `secondary` dict + all emitted events.
7. **Route events** — pass each event to `EventRouter.handle()` (only when `automation_enabled`); update `last_dispatched_action` / `last_dispatched_at` for the UI.
8. **Build sensor dict** — map type-keyed `secondary` entries to the string-keyed dict sensors read (`"pricing"`, `"forecast"`, `"calculation"`, `"schedule"`, `"ev_schedule"`, `"battery_state"`, `"degradation_cost"`, `"estimated_capacity"`, `"prices"`, `"grid_power_kw"`, `"battery_power_kw"`, `"ev_state"`, `"dashboard_slots"`, `"solar_frozen_forecast"`).

The coordinator contains no domain computation — it owns the schedule, the persistent stores, and the string↔type bridge to sensors.

`debug_view.py` registers an HTTP view at `/api/sun_sale/debug` that exposes the most recent `primary` and `secondary` for inspection.

---

## 7. Node reference

| Node | Tier | Consumes | Produces | Events |
|---|---|---|---|---|
| `PricingNode` | 1 | `NordpoolData`, `YesterdayPrices` | `PriceSeries` (72h) | — |
| `BatteryStateNode` | 1 | `BatteryReading`, `EstimatedCapacity` | `BatteryState` | — |
| `BatteryStatusNode` | 1 | `BatteryReading` | `BatteryStatus` | — |
| `GenerationNode` | 2 | `SolarData`, `PriceSeries` | `GenerationSeries` | — |
| `DegradationNode` | 2 | `BatteryState` | `DegradationCost` | — |
| `EVSchedulerNode` | 2 | `PriceSeries`, `EVChargerState` | `EVSchedule` | `EVActionEvent` (on change) |
| `ChargingProfileNode` | 3 | `BatteryStatus`, `GenerationSeries`, `PriceSeries` | `ChargingProfile` | — |
| `LockoutNode` | 3 | `PriceSeries`, `GenerationSeries`, `BatteryState` (+ optional `EVChargerState` via `ctx.get`) | `CalculationResult` | — |
| `OptimizerNode` | 4 | `PriceSeries`, `CalculationResult`, `GenerationSeries`, `BatteryState`, `DegradationCost` | `Schedule` | `InverterActionEvent` (on change) |
| `DashboardNode` | 5 | `NordpoolData`, `SolarData`, `BatteryReading`, `PriceSeries`, `GenerationSeries`, `Schedule` | `DashboardData` | — |

`EVSchedulerNode` is only registered when `CONF_EV_ENABLED=True`. `EVChargerState` is then a primary input (from `EVTranslator`); when EV is disabled it is absent from `primary` and `LockoutNode` uses `ctx.get(EVChargerState)` (returns `None`).

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
| `EVChargerState` | `EVTranslator` (EV only) | `is_plugged_in`, `soc`, `target_soc`, `departure_time` |

### Secondary data (in `NodeContext.secondary`)

| Type | Produced by | Key fields |
|---|---|---|
| `PriceSeries` | `PricingNode` | `slots: tuple[PriceSlot, ...]` (72h), `resolution: timedelta`, `computed_at: datetime`; helpers `slot_at(t)`, `window(t1, t2)` |
| `BatteryState` | `BatteryStateNode` | `soc: float`, `estimated_capacity_kwh: float` |
| `BatteryStatus` | `BatteryStatusNode` | `total_capacity_kwh`, `max_charge_power_kw`, `max_discharge_power_kw`, `soc`, `remaining_capacity_kwh` |
| `GenerationSeries` | `GenerationNode` | `slots: tuple[GenerationSlot, ...]`, `primary: str`, `overlays: tuple[str, ...]`, `computed_at` |
| `DegradationCost` | `DegradationNode` | `value_kwh: float` |
| `EVSchedule` | `EVSchedulerNode` | `slots: list[EVChargeSlot]`, `total_cost_eur`, `total_energy_kwh`, `computed_at` |
| `CalculationResult` | `LockoutNode` | `slots: tuple[SlotDecision, ...]`, `feed_in_lockout_windows`, `total_negative_sale_kwh`, `computed_at` |
| `ChargingProfile` | `ChargingProfileNode` | `slots: tuple[ChargingProfileSlot, ...]` (today's remaining; `mode ∈ {solar_charge, sell, no_export, idle}`), `free_capacity_kwh`, `today_remaining_generation_kwh`, `solar_exceeds_capacity`, `allocated_solar_kwh`, `total_no_export_kwh`, `computed_at` |
| `Schedule` | `OptimizerNode` | `slots: list[ScheduleSlot]`, `total_expected_profit_eur`, `degradation_cost_per_kwh`, `computed_at` |
| `DashboardData` | `DashboardNode` | `future_slots: list[dict]`, `solar_frozen_forecast: list[dict]` |

`PriceSlot` carries `buy_eur_kwh`, `sell_eur_kwh` (can be negative), `spot_eur_kwh`, `sell_allowed` (strict `> 0`), and `sources: tuple[str, ...]` for diagnostics. See `docs/inbound_pricing.md`.

---

## 9. Key design decisions

**HA boundary.** HA imports are confined to the root entry points (`__init__.py`, `config_flow.py`, `sensor.py`, `switch.py`), `orchestration/coordinator.py`, `orchestration/debug_view.py`, and the two outbound controllers (`outbound/inverter.py`, `outbound/ev_charger.py`). `inbound/translators.py` reads `hass.states` but never imports the `homeassistant` package — it works on duck-typed `hass`. Every other module is pure Python and testable with plain `pytest` without an HA harness.

**Sub-package layering enforces direction.** `contract` depends on nothing. `inbound` / `pipeline` / `outbound` depend only on `contract`. `orchestration` is the only layer allowed to glue them. This is enforced by convention (and would surface as a circular import if violated).

**Inbound owns the 72h pricing assembly.** The Nordpool translator emits today + tomorrow only; the coordinator supplies yesterday from a persistent store as `YesterdayPrices`; `inbound/pricing.build_price_series_72h` combines them and applies the tariff. Downstream consumers can rely on `PriceSeries` covering yesterday→today→tomorrow when yesterday data is available. See `docs/inbound_pricing.md`.

**Tier constraint enforced at wire-time.** `add_observer()` raises `TierViolationError` if `observer.tier <= subject.tier`. This catches dependency graph mistakes during integration startup, not at runtime.

**Events vs. return values.** Nodes return their computed data *and* a list of `ControlEvent` objects. Events signal side-effects (send an inverter command); the return value is data that flows to downstream nodes. Keeping them separate means the engine can route them independently.

**Two-layer deduplication.** `OptimizerNode` and `EVSchedulerNode` suppress emitting an event when the current-slot action key matches the previous cycle (`_LastActionRef`). `EventRouter` additionally re-checks the inverter key (`_last_inverter_key`) before dispatching, giving belt-and-braces protection against duplicate inverter commands. EV events rely on node-side dedup only.

**Coordinator-injected primary data.** `YesterdayPrices` and `EstimatedCapacity` are not translator outputs — they are stateful values the coordinator owns across cycles (loaded from `Store`, updated each cycle) and deposited into `primary` before `DagEngine.run()`. Treating them as primary data keeps DAG nodes stateless and the engine free of cross-cycle state.

**Optional EV path is additive.** When EV is disabled, `EVTranslator` and `EVChargerController` are not constructed, `EVChargerState` is absent from `primary`, and `EVSchedulerNode` is not added to the engine. `LockoutNode` uses `ctx.get(EVChargerState)` (returns `None`); all other nodes ignore EV types entirely. No conditional EV branches scattered through the DAG.
