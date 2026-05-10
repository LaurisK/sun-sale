# sunSale Architecture

End-to-end description of the tiered observer DAG that drives every sunSale update cycle.

| | |
|---|---|
| **Status** | Implemented (v0.1.2+) |
| **Pattern** | Translation layer + tiered observer DAG + event router |
| **HA boundary** | `translators.py` only — all other modules are pure Python |

## Contents

1. [Overview](#1-overview)
2. [Layer 1 — Translation layer](#2-layer-1--translation-layer)
3. [Layer 2 — DAG engine and nodes](#3-layer-2--dag-engine-and-nodes)
4. [Layer 3 — Event router and output adapters](#4-layer-3--event-router-and-output-adapters)
5. [Update cycle walk-through](#5-update-cycle-walk-through)
6. [Node reference](#6-node-reference)
7. [Data contracts](#7-data-contracts)
8. [Key design decisions](#8-key-design-decisions)

---

## 1. Overview

```
HA state machine
      │  (read once per cycle, parallel)
      ▼
┌─────────────────────────────────────────────┐
│  Translation Layer       translators.py     │
│                                             │
│  NordpoolTranslator  →  NordpoolPrices      │
│  SolarTranslator     →  RawSolarData        │
│  BatteryTranslator   →  BatteryReading      │
│  EVTranslator        →  EVChargerState      │
└─────────────────────────────────────────────┘
      │  typed primary data (dict[type, Any])
      ▼
┌─────────────────────────────────────────────┐
│  DAG Engine              dag_engine.py      │
│                                             │
│  T1  PricingNode      BatteryStateNode      │
│  T2  GenerationNode   DegradationNode       │
│      EVSchedulerNode  (EV optional)         │
│  T3  LockoutNode                            │
│  T4  OptimizerNode                          │
│  T5  DashboardNode                          │
└─────────────────────────────────────────────┘
      │  ControlEvents
      ▼
┌─────────────────────────────────────────────┐
│  Event Router            event_router.py    │
│  → InverterController                       │
│  → EVChargerController (EV optional)        │
└─────────────────────────────────────────────┘
```

The coordinator (`coordinator.py`) owns the update schedule (every 5 minutes), feeds the three layers in order, and collects results into `coordinator.data` for sensor entities.

---

## 2. Layer 1 — Translation layer

**File:** `translators.py`

This is the **only** file in the integration that reads from the HA state machine. All other modules are pure Python.

Each translator has a synchronous `.parse(hass)` method (testable without HA) and an asynchronous `.translate(hass)` wrapper called by the coordinator.

| Translator | Output type | Source |
|---|---|---|
| `NordpoolTranslator` | `NordpoolPrices` | Nordpool sensor — prefers `raw_today`/`raw_tomorrow` (15-min timestamped slots), falls back to legacy hourly `today`/`tomorrow` arrays |
| `SolarTranslator` | `RawSolarData` | Open Meteo watts dict + Forecast.Solar/Solcast `forecast` attribute (fallback). Combines today + tomorrow entities automatically. |
| `BatteryTranslator` | `BatteryReading` | InverterController for SoC/power/grid; HA state for household load sensor |
| `EVTranslator` | `EVChargerState` | Plug-state binary sensor, EV SoC, target SoC, departure time. Only registered when EV is enabled. |

All translators run in parallel via `asyncio.gather` before the DAG starts.

---

## 3. Layer 2 — DAG engine and nodes

**Files:** `dag_engine.py`, `nodes.py`

### DagNode contract

Every node declares three class attributes:

```python
class SomeNode(DagNode):
    tier: int              # 1–5; controls execution order and observer wiring
    output_type: type      # the type this node deposits into NodeContext.secondary
    consumes: list[type]   # primary or secondary types this node needs
```

The engine calls `_wire()` once at setup. For each `(consumer, dependency_type)` pair it finds the producer node for that type and calls `producer.add_observer(consumer)`. `add_observer` raises `TierViolationError` if `consumer.tier <= producer.tier`, enforcing the DAG's acyclicity guarantee.

### Observer notification

After `_compute()` returns, `run()` deposits the result in `ctx.secondary[output_type]` and calls `_notify_observers()`, which sets a "satisfied" flag on each observer for that type. A node is "ready" for a tier when all its secondary dependencies are either already in `ctx.secondary` or have been marked satisfied.

### Tier execution

```python
for tier_num in sorted(nodes_by_tier):
    ready = [n for n in nodes_by_tier[tier_num] if n.all_secondary_deps_satisfied(ctx)]
    results = await asyncio.gather(*[n.run(ctx) for n in ready])
```

All nodes in a tier run concurrently. Primary data (from translators) is always pre-populated and never blocks readiness checks.

### NodeContext

```python
@dataclass
class NodeContext:
    primary:   dict[type, Any]    # translator outputs — never modified by nodes
    secondary: dict[type, Any]    # node outputs — accumulated tier by tier
    config:    SunSaleConfig      # tariff + battery + EV config
    now:       datetime

    def require(self, t: type) -> Any: ...   # raises MissingDependencyError if absent
    def get(self, t: type) -> Any | None: ... # returns None if absent (optional deps)
```

---

## 4. Layer 3 — Event router and output adapters

**Files:** `event_router.py`, `inverter.py`, `ev_charger.py`

Nodes emit `ControlEvent` objects (defined in `events.py`) alongside their computed result. The coordinator collects all events from all nodes and passes them to `EventRouter.handle()`.

```python
@dataclass(frozen=True)
class InverterActionEvent(ControlEvent):
    action: Action       # CHARGE_FROM_GRID | DISCHARGE_TO_GRID | IDLE
    power_kw: float

@dataclass(frozen=True)
class EVActionEvent(ControlEvent):
    charge_power_kw: float   # 0.0 = stop charging
```

`EventRouter` deduplicates inverter commands by keying on `f"{action}:{power_kw:.3f}"`. Repeated identical commands within or across cycles are suppressed. Only a genuine change triggers a service call to the inverter/EV-charger adapter.

The output adapters (`InverterController`, `EVChargerController`) contain all platform-specific HA service call logic and are the only files that write to HA.

---

## 5. Update cycle walk-through

`SunSaleCoordinator._async_update_data()`:

1. **Translate** — run all translators in parallel; collect `primary` dict.
2. **Capacity estimation** — update `CapacityEstimator` with the new `BatteryReading`; inject `EstimatedCapacity` into `primary`.
3. **DAG run** — `DagEngine.run(primary, config, now)` executes tiers T1→T5; returns `secondary` dict + all emitted events.
4. **Route events** — pass each event to `EventRouter.handle()`; suppressed if identical to last cycle.
5. **Build sensor dict** — map type-keyed `secondary` entries to string keys (`"pricing"`, `"forecast"`, `"calculation"`, `"schedule"`, `"dashboard"`) for sensor entities.

The coordinator itself contains no computation — it only owns the update schedule, the CapacityEstimator state between cycles, and the `_last_battery_reading` needed for SoC-delta capacity observations.

---

## 6. Node reference

| Node | Tier | Consumes | Produces | Events |
|---|---|---|---|---|
| `PricingNode` | 1 | `NordpoolPrices` | `PriceSeries` | — |
| `BatteryStateNode` | 1 | `BatteryReading`, `EstimatedCapacity` | `BatteryState` | — |
| `GenerationNode` | 2 | `RawSolarData`, `PriceSeries` | `GenerationSeries` | — |
| `DegradationNode` | 2 | `BatteryState` | `DegradationCost` | — |
| `EVSchedulerNode` | 2 | `PriceSeries`, `EVChargerState` | `EVSchedule` | `EVActionEvent` (on change) |
| `LockoutNode` | 3 | `PriceSeries`, `GenerationSeries`, `BatteryState` | `CalculationResult` | — |
| `OptimizerNode` | 4 | `PriceSeries`, `CalculationResult`, `GenerationSeries`, `BatteryState`, `DegradationCost` | `Schedule` | `InverterActionEvent` (on change) |
| `DashboardNode` | 5 | `NordpoolPrices`, `RawSolarData`, `BatteryReading`, `PriceSeries`, `GenerationSeries`, `Schedule` | `DashboardData` | — |

`EVSchedulerNode` is only registered when `CONF_EV_ENABLED=True`. `EVChargerState` is absent from primary otherwise; nodes use `ctx.get(EVChargerState)` and handle `None` gracefully.

---

## 7. Data contracts

All types are frozen dataclasses in `models.py` (no HA imports).

### Primary data (translator outputs)

| Type | Key fields |
|---|---|
| `NordpoolPrices` | `slots: list[HourlyPrice]`, `raw_15min: dict[datetime, float]` |
| `RawSolarData` | `watts: dict[datetime, float]`, `forecast_slots: list[dict]` |
| `BatteryReading` | `soc: float`, `power_kw: float`, `grid_power_kw: float`, `household_load_kw: float` |
| `EstimatedCapacity` | `value_kwh: float` (injected by coordinator from CapacityEstimator) |
| `EVChargerState` | `plugged_in: bool`, `soc: float`, `target_soc: float`, `departure: datetime | None`, `max_charge_power_kw: float` |

### Secondary data (node outputs)

| Type | Produced by | Key fields |
|---|---|---|
| `PriceSeries` | PricingNode | `slots: tuple[PriceSlot, ...]`, `resolution: timedelta`, `computed_at: datetime` |
| `BatteryState` | BatteryStateNode | `soc: float`, `estimated_capacity_kwh: float` |
| `GenerationSeries` | GenerationNode | `slots: tuple[GenerationSlot, ...]`, `primary: str`, `overlays: tuple[str, ...]` |
| `DegradationCost` | DegradationNode | `value_kwh: float` |
| `EVSchedule` | EVSchedulerNode | `slots: tuple[EVSlot, ...]` |
| `CalculationResult` | LockoutNode | `slots: tuple[SlotDecision, ...]`, `feed_in_lockout_windows`, `computed_at` |
| `Schedule` | OptimizerNode | `slots: tuple[ScheduleSlot, ...]` |
| `DashboardData` | DashboardNode | `future_slots: list[dict]`, `solar_frozen_forecast: list[dict]` |

---

## 8. Key design decisions

**Hard HA boundary.** Only `translators.py` reads from HA. Every other module is pure Python and testable with plain `pytest` without an HA harness. Violations surface immediately as import errors.

**Tier constraint enforced at wire-time.** `add_observer()` raises `TierViolationError` if `observer.tier <= subject.tier`. This catches dependency graph mistakes during integration startup, not at runtime.

**Events vs. return values.** Nodes return their computed data *and* a list of `ControlEvent` objects. Events signal side-effects (send an inverter command); the return value is data that flows to downstream nodes. Keeping them separate means the DAG engine can route them independently and the event router can deduplicate without coupling to node internals.

**Deduplication at the router, not the node.** `EventRouter` holds `_last_inverter_key` and suppresses repeated identical commands. Nodes emit events freely every cycle; the router decides whether to act. This makes nodes stateless (easier to test) and centralises the suppress-repeated-commands logic.

**CapacityEstimator outside the DAG.** It is stateful across cycles (it accumulates observations over hours/days) and has no downstream consumers in the same cycle — its output is injected as `EstimatedCapacity` primary data before `DagEngine.run()`. Making it a DAG node would require cross-cycle state in the engine, which would complicate reset and testing.

**Optional EV path is additive.** When EV is disabled, `EVTranslator` is not registered, `EVChargerState` is absent from primary, and `EVSchedulerNode` is not added to the engine. `LockoutNode` uses `ctx.get(EVChargerState)` (returns `None`); all other nodes ignore EV types entirely. No conditionals scattered through the DAG.
