# sunSale — System Architecture Assessment

> Point-in-time architectural review dated 2026-05-29.
> Scope: `custom_components/sun_sale/` (~7.3k LOC), companion to `ARCHITECTURE.md` and `MODULES.md`.
> Audience: maintainer planning the next refactor pass.

## Contents

1. [Current shape](#1-current-shape)
2. [What is working very well](#2-what-is-working-very-well)
3. [Issues and tensions](#3-issues-and-tensions)
4. [Proposed improvements, ranked by ROI](#4-proposed-improvements-ranked-by-roi)
5. [What not to change](#5-what-not-to-change)
6. [Recommended first step](#6-recommended-first-step)

---

## 1. Current shape

A Home Assistant integration that orchestrates battery + solar arbitrage on Nordpool prices. The architecture is a five-layer stack with strict directional dependencies:

```
┌──────────────────────────────────────────────────────────────────┐
│ Layer            Role                                Imports HA? │
├──────────────────────────────────────────────────────────────────┤
│ contract/        Pure dataclasses + enums + events   No          │
│ inbound/         HA-state translators + normalisers  Runtime-only│
│                                                      (duck-typed)│
│ pipeline/        Tiered observer DAG (~13 nodes)     No          │
│ outbound/        Inverter writer + event router      Yes (write) │
│ orchestration/   Coordinator + debug HTTP view       Yes         │
│ (root)           __init__, sensor, switch,           Yes         │
│                  config_flow                                     │
└──────────────────────────────────────────────────────────────────┘
```

The DAG engine wires 13 nodes across 4 tiers by matching `output_type → consumes`, enforces the tier constraint at startup (not runtime), and fans out ready nodes per tier with `asyncio.gather`. The coordinator owns translators, persistent stores, and the string-keyed bridge that feeds HA sensor entities.

---

## 2. What is working very well

1. **Strict layering with a one-line discipline.** `contract` depends on nothing; `inbound/pipeline/outbound` depend only on `contract`; `orchestration` is the only allowed gluer. The rule is simple enough for a future maintainer to hold in their head.
2. **HA boundary is well-fenced.** Only six files import the `homeassistant` package. Everything testable runs without an HA harness — rare for a custom component and the right call.
3. **DAG engine is small (~290 LOC), explicit, and correct.** Observer wiring by type, tier enforcement at startup, async fan-out per tier. The `NodeContext.require()`/`get()` split is clean.
4. **Type-keyed primary/secondary dicts** make the dependency graph self-documenting; new nodes are nearly drop-in.
5. **Coordinator-injected primaries** (`YesterdayPrices`, `EstimatedCapacity`, `PriceHistory`, `HouseholdLoadHistory`, `GenerationHistory`, `ForecastQualityStore`, `SunTimes`) keep nodes stateless without funneling stateful values through translators.
6. **Two-layer command deduplication** (node-side `_LastActionRef` + router-side `_last_inverter_key`) is belt-and-braces protection against duplicate inverter dispatch.
7. **Pre-refactored modules are visibly cleaner** (`inbound/{pricing,forecast,generation,battery}`, `pipeline/{charging_profile,base_load}`). There is a clear, repeatable refactor pattern to apply to the rest.

---

## 3. Issues and tensions

### 3.1 The coordinator is becoming a god object (743 LOC)

`SunSaleCoordinator` now owns:

- Six translator instantiations
- Thirteen node instantiations
- **Seven persistent stores** (capacity, yesterday-prices/solar, generation, household-load, price-history, forecast-quality)
- Their per-cycle append/trim/serialize logic inline
- Local-tz day-rollover rotation for yesterday-prices
- Sun-times reading from `sun.sun`
- Capacity-observation derivation
- String-keyed sensor dict assembly
- Event dispatch gating

`docs/MODULES.md §7` already records the smell: *"Every 'missing' follow-up tends to touch three places here: store load in `async_setup`, primary injection in `_async_update_data`, an entry in `_build_sensor_dict`."* That is a structural symptom, not a docstring note.

### 3.2 Persistence is a cross-cutting concern with no abstraction

Each store appears four times in `coordinator.py`: declaration, `Store(...)` instantiation in `async_setup`, load+parse, append+trim+serialize+save. Adding a new store means touching four places, copy-pasting the trim/serialize pattern, and risking the LOCAL-tz bug already discovered (see the comment at `coordinator.py:415–423`).

### 3.3 `nodes.py` mixes 12 small node classes with import-time fan-out

`pipeline/nodes.py` imports `base_load`, `battery`, `calculation`, `charging_profile`, `forecast_accuracy`, `schedule`, `profitability`, `inbound.battery`, `inbound.forecast`, `inbound.generation`, `inbound.pricing`. It is a monolithic dispatch table. Each node is ~10 lines of "extract from ctx, call helper, return"; the file is 400 LOC because imports + boilerplate dominate.

### 3.4 The tier system uses `int` constants — fragile and lossy

`tier = 1/2/3/4` is set at the class level. Move `ChargingProfileNode` to depend on a new T3 output and tier ordering silently breaks. The wire-time check catches violations *given the declared tiers*, but tiers are asserted by the developer, not derived from `consumes`. Tier could be derived as the longest path from primary inputs, eliminating the manual maintenance.

### 3.5 `ForecastQualityStore` and `SunTimes` are not in `consumes`

`ForecastAccuracyNode.consumes = [GenerationSeries, ObservedGenerationSeries]` but the node also reads `ForecastQualityStore` and `SunTimes` via `ctx.get(...)`. The docstring explains *"to avoid self-referential DAG wiring"* — but the dependency declaration is incomplete. A future refactor that re-derives dependencies from `consumes` (see 3.4) will miss these.

Fix shape: a separate `reads_primary: list[type]` slot on `DagNode`, declared explicitly and excluded from observer wiring.

### 3.6 Sensor dict is a string-keyed shadow contract

`_build_sensor_dict` maps ~20 typed entries to a string-keyed dict that `sensor.py` reads. Renaming a type or adding a field requires updating two places (and the debug view may form a third). This layer earns its keep in v0 and starts costing by v2.

### 3.7 Profitability integration check coverage is unclear

Per `CLAUDE.md`: every pipeline module must have a corresponding deep-check in `tools/integration_check.py`. `ProfitabilityNode` is now registered in the coordinator (lines 304–306) and `STORAGE_KEY_PRICE_HISTORY` is loaded — but `MODULES.md §9` still describes it as "not wired." Verify that `tools/integration_check.py` has a `check_profitability()` and a `ProfitabilityCheckWidget`. If not, the convention has already drifted.

### 3.8 Tests cover units; coordinator integration is one large fixture

`tests/test_coordinator.py` is the only place the full DAG runs end-to-end. With 13 nodes and 7 stores, the surface is now too big for one file to catch regressions. Three small additions would each pay back:

- `test_dag_topology.py` — derives tier from `consumes`, asserts it matches declarations, asserts every type in `consumes ∪ reads_primary` is either produced by another node or a known primary key.
- `test_persistence_contract.py` — round-trip every store.
- `test_sensor_dict_completeness.py` — every key consumed by sensor entities is produced by `_build_sensor_dict`.

### 3.9 `models.py` is 662 LOC of every dataclass

The flat catalogue is an explicit choice and has value. At 662 LOC across 30+ types, it has crossed the threshold from "read" to "Ctrl-F." Splitting into `models/primary.py`, `models/secondary.py`, `models/config.py` (re-exported from `models/__init__.py`) preserves the flat import surface while scoping changes. Defer until item #5 below.

### 3.10 No structured architectural decision log

`ARCHITECTURE.md` describes the *current* shape excellently. It does not record *why* tiers are explicit ints, why `nodes.py` is monolithic, why the coordinator owns persistence. Cold readers re-litigate. `base_load_missing.md` already follows the right pattern — extend it to a `docs/adr/` directory.

---

## 4. Proposed improvements, ranked by ROI

### Tier A — high ROI, low risk

1. ~~**Extract persistence into a `PersistentStore[T]` helper.**~~
   **Done (2026-05-29).** `orchestration/persistent_store.py` introduces `PersistentStore[T]` with `load()`, `save()`, `value`, and `append_and_trim()`. All six coordinator stores migrate to it. The yesterday two-bucket state is consolidated into `_YesterdayBuckets` (private dataclass) with `_rotate_yesterday_buckets()` extracted as a pure helper. Serialisation logic lifted to module-level functions. Net: coordinator shrinks from 743 → 787 lines (added 183 lines of serde helpers / dataclass) but removes the five in-memory list fields, two `_append_*` methods (~40 LOC), and all inline `async_save` call-sites with their embedded serialisers. New stores each take 4–5 lines to register. The `persistent_store.py` helper is 87 LOC.

2. **Split `nodes.py` per-tier or per-domain.** Either `pipeline/nodes/tier1.py`, `tier2.py`, … or `pipeline/nodes/{pricing,battery,forecast,schedule}.py`. Re-export from `pipeline/nodes/__init__.py` so the coordinator's import line is unchanged. Imports get scoped, the file becomes findable.

3. **Add `reads_primary: list[type]` to `DagNode`.** Make `ForecastAccuracyNode` (and any future node reading primaries that bypass `consumes` for wiring reasons) declare them. Update `_wire()` to use both lists. Eliminates the implicit-dependency hazard.

4. **Add `tests/test_dag_topology.py`** that:
   - instantiates every real node;
   - asserts every type in `consumes ∪ reads_primary` is either produced by another node or a known primary key;
   - re-derives each node's tier as the longest path from primaries and asserts it matches the declared tier.

### Tier B — medium ROI, moderate risk

5. **Replace `_build_sensor_dict` with a declarative `SensorBindings` table.**
   ```python
   BINDINGS = [
       Binding("pricing",           PriceSeries,      from_secondary),
       Binding("forecast",          GenerationSeries, from_secondary),
       Binding("degradation_cost",  DegradationCost,  from_secondary,
               project=lambda d: d.value_kwh, default=0.0),
       ...
   ]
   ```
   Sensors look up by typed key; the string layer becomes a thin presentation adapter. Eliminates the dual-write problem.

6. **Derive tier from `consumes`.** Replace the `tier` class attribute with a topological sort at `DagEngine.__init__`. Keep `TierViolationError` (rename to `CycleError`). Unblocks node re-ordering without manual tier bumps.

7. **Add a per-cycle structured log + cycle ID.** A 5-min DAG run with 13 nodes is observable in the debug view, but there is no time-series of "what changed when." A single JSON line per cycle (`cycle_id, run_ms, schedule_changed, dispatched_action, n_events`) survives restarts and is searchable from HA logs.

8. **Move `integration_check.py` deep-checks next to their modules.** E.g. `pipeline/profitability_check.py`; the tool discovers them. Stops `tools/integration_check.py` from becoming the next 1000-LOC monolith.

### Tier C — strategic, plan before doing

9. **Replace greedy optimizer with DP or MILP.**
   Greedy pair-match is fine for v1 but misses opportunities when SoC bounds intersect non-trivially with multi-pair sequences. With `PriceHistory` + `ProfitabilityScore` already plumbed, the inputs exist for day-class-aware DP over 72h slots. ~150 LOC, significant modelling gain. Worth a design doc first.

10. **EV charging.** The original implementation plan called for `ev_scheduler.py` + `ev_charger.py`; they are absent. If still on the roadmap, the current architecture is a great fit — add T1 `EVStateTranslator` + T3 `EVScheduleNode` + outbound adapter. If not on the roadmap, prune the references from `MEMORY.md` and the docs to reduce confusion.

11. **ADR directory.** Start `docs/adr/` with one ADR per surviving decision: "Coordinator-injected primaries," "Tiers as explicit integers," "Flat `models.py`." Each 10–20 lines. Cheap insurance against re-litigation.

---

## 5. What not to change

- **The layer split.** Do not merge `inbound`/`pipeline`/`outbound`.
- **The duck-typed `hass` in translators.** This is the right testability hack.
- **The two-layer event dedup.** Belt-and-braces is correct here.
- **The flat `models.py`** until item #5 above lands — otherwise the move is churn.
- **`contract/` as a separate package.** Some will call it overkill for one repo; it pays for itself every time the rule prevents an import cycle.

---

## 6. Recommended first step

**Tier A item #1: extract `PersistentStore[T]`** — **done 2026-05-29.** Next recommended step: **Tier A item #2 (split `nodes.py` per-tier or per-domain)**, which is now the largest single-file smell, followed by items #3 and #4.

### Verification before acting

- **Memory staleness:** project memory was last updated 41 days ago. `MODULES.md §9` lists `profitability.py` as "not wired," but `coordinator.py` clearly registers `ProfitabilityNode`. Update both before relying on them for planning.
- **Integration-check coverage:** confirm `tools/integration_check.py` has the `ProfitabilityCheckWidget` required by `CLAUDE.md`. If not, that gap is independent of any refactor and should be closed first.
