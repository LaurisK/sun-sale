# Pipeline — Charging profile module

Reference for `custom_components/sun_sale/pipeline/charging_profile.py`.

The charging-profile module decides, slot by slot for the rest of today, what to do with predicted solar generation: store it in the battery, export it to the grid, or curtail it. It is pure Python with no Home Assistant imports.

## Contents

1. [Responsibilities](#1-responsibilities)
2. [Public API](#2-public-api)
3. [Inputs](#3-inputs)
4. [Output: `ChargingProfile`](#4-output-chargingprofile)
5. [Algorithm](#5-algorithm)
6. [Integration in the DAG](#6-integration-in-the-dag)
7. [Tests](#7-tests)

---

## 1. Responsibilities

- Compute the battery's available free space — `(max_soc - soc) * total_capacity_kwh` — from `BatteryStatus` plus `BatteryConfig.max_soc`.
- Compare it against today's remaining predicted solar generation.
- Emit a `ChargeMode` per slot for every today-and-future generation slot:
  - `SOLAR_CHARGE` — store this slot's solar in the battery.
  - `SELL` — export this slot's solar (sell price strictly positive).
  - `NO_EXPORT` — excess solar but sell price ≤ 0; curtail rather than pay to export.
  - `IDLE` — no generation expected this slot.

What this module deliberately does **not** do:

- It does not schedule grid charging. The optimizer owns grid charge/discharge decisions; this module reasons only about *solar disposition*.
- It does not consider future days. Only slots with `start.date() == now.date()` and `start >= now` are emitted, matching the `today_remaining_kwh` convention used by `GenerationSeries`.
- It does not split the marginal slot. When cumulative allocation crosses `free_capacity_kwh` mid-slot, the slot is kept whole as `SOLAR_CHARGE` — slight overfill is acceptable and far simpler than per-slot fractional accounting.

---

## 2. Public API

```python
def build_charging_profile(
    battery_status: BatteryStatus,
    generation: GenerationSeries,
    prices: PriceSeries,
    battery_config: BatteryConfig,
    now: datetime,
) -> ChargingProfile
```

Pure function — no I/O, no globals, no clock reads. `now` is the cycle timestamp recorded on the result and used to filter "today's remaining" slots.

---

## 3. Inputs

**`BatteryStatus`** — produced by `BatteryStatusNode` (T1). Provides `soc` and `total_capacity_kwh` (the configured nominal capacity).

**`GenerationSeries`** — produced by `GenerationNode` (T2). The 72h grid; the module filters to today's remaining slots before reasoning.

**`PriceSeries`** — produced by `PricingNode` (T1). Each slot carries `sell_eur_kwh`, the effective price after fees and tax. Looked up by `slot.start` to rank generating slots and to classify residual `SELL` vs `NO_EXPORT`.

**`BatteryConfig`** — read from `ctx.config.battery`. Only `max_soc` is consumed. Other fields are owned by the optimizer.

---

## 4. Output: `ChargingProfile`

```python
@dataclass(frozen=True)
class ChargingProfile:
    slots: tuple[ChargingProfileSlot, ...]   # today's remaining slots, in order
    free_capacity_kwh: float                  # (max_soc - soc) * total_capacity_kwh
    today_remaining_generation_kwh: float
    solar_exceeds_capacity: bool              # False = case 1, True = case 2
    allocated_solar_kwh: float                # sum of SOLAR_CHARGE expected_kwh
    total_no_export_kwh: float                # sum of NO_EXPORT expected_kwh
    computed_at: datetime


@dataclass(frozen=True)
class ChargingProfileSlot:
    start: datetime
    end: datetime
    mode: ChargeMode             # SOLAR_CHARGE | SELL | NO_EXPORT | IDLE
    expected_kwh: float
    sell_eur_kwh: float          # the price used to classify this slot
```

`free_capacity_kwh` is clamped at `0.0` — if `soc > max_soc` (telemetry drift), the module reports zero free space rather than a negative number.

---

## 5. Algorithm

```
free_capacity = max(0, (max_soc - soc) * total_capacity_kwh)
today_remaining = [g for g in generation.slots if g.start.date() == now.date() and g.start >= now]
today_remaining_kwh = sum(g.expected_kwh for g in today_remaining)

if today_remaining_kwh <= free_capacity:
    # Case 1: all solar fits → mark every generating slot SOLAR_CHARGE
    allocated = {g.start for g in today_remaining if g.expected_kwh > 0}
else:
    # Case 2: rank generating slots by sell_eur_kwh ascending, allocate
    # cumulatively until free_capacity is reached. Marginal slot kept whole.
    allocated = set()
    cumulative = 0.0
    for g in sorted(generating, key=sell_eur_kwh_then_start):
        if cumulative >= free_capacity:
            break
        allocated.add(g.start)
        cumulative += g.expected_kwh

for g in today_remaining:
    if g.expected_kwh <= 0:                 → IDLE
    elif g.start in allocated:              → SOLAR_CHARGE
    elif sell_eur_kwh > 0:                  → SELL
    else:                                   → NO_EXPORT
```

**Why "lowest sell price first" in case 2.** When solar overflows the battery, every "non-allocated" generating slot is destined for the grid. Allocating the cheapest-to-sell slots into the battery preserves the most valuable solar slots for export — equivalently, we sell only the most expensive solar and keep the rest.

**Negative sell prices.** A negative `sell_eur_kwh` means the grid pays *you* to consume rather than the other way around — exporting actively costs money. Such slots rank first in the case-2 sort (so they're preferred for battery charging) and, if not allocated, fall through to `NO_EXPORT` rather than `SELL`.

**Tie-breaking.** Slots with identical `sell_eur_kwh` are ordered by `start` ascending — earlier slots win. Deterministic and matches solar's natural arrival order.

---

## 6. Integration in the DAG

`ChargingProfileNode` (`pipeline/nodes.py`):

```python
tier = 3
output_type = ChargingProfile
consumes = [BatteryStatus, GenerationSeries, PriceSeries]
```

Tier 3 because it consumes the T1 secondary outputs `BatteryStatus` and `PriceSeries`, plus the T2 secondary `GenerationSeries`. It runs in parallel with `LockoutNode` (also T3); neither depends on the other.

Coordinator flow (`orchestration/coordinator.py:_async_update_data`):

1. Run translators → `primary[BatteryReading, SolarData, NordpoolData, …]`.
2. `DagEngine.run(...)` → T1 produces `BatteryStatus` + `PriceSeries`; T2 produces `GenerationSeries`; T3 `ChargingProfileNode` calls `build_charging_profile(...)` → `secondary[ChargingProfile]`.
3. `_build_sensor_dict` exposes the value under the `"charging_profile"` key.

No downstream DAG node currently consumes `ChargingProfile` — it is a sink-shaped output destined for sensors and dashboards.

---

## 7. Tests

All charging-profile tests live in `tests/test_charging_profile.py` and run under pure-Python pytest (no HA harness needed). Shared fixtures come from `tests/conftest.py` — `default_battery_config()` provides the canonical config.

Run them with:

```bash
./venv/bin/python -m pytest tests/test_charging_profile.py -v
```

### Coverage

| Group | Test | What it checks |
|---|---|---|
| Case 1 (fits) | `test_case1_all_solar_charge_when_generation_fits` | gen < free → all generating slots SOLAR_CHARGE, zero-kWh slots IDLE |
| | `test_case1_free_capacity_uses_max_soc_not_one` | `free = (max_soc - soc) * total`, not `(1 - soc) * total` |
| Case 2 (overflow) | `test_case2_lowest_sell_price_slots_fill_battery` | cheapest sell-price slot allocated to battery; others SELL |
| | `test_case2_marginal_slot_kept_whole_overfills` | marginal slot fully allocated even when its kWh exceeds remaining free capacity |
| | `test_case2_multiple_slots_summed_to_reach_free_capacity` | cumulative allocation across multiple cheap slots |
| NO_EXPORT | `test_no_export_when_sell_price_negative` | negative-price excess slot → NO_EXPORT (not SELL) |
| | `test_no_export_when_battery_full_and_sell_negative` | `free_capacity == 0` and negative price → NO_EXPORT |
| Edge cases | `test_zero_generation_all_idle` | every slot IDLE when no solar |
| | `test_past_slots_excluded` | slots with `start < now` filtered out |
| | `test_tomorrow_slots_excluded` | slots on a later date filtered out |
| | `test_today_remaining_generation_kwh_matches_sum` | totals field equals sum of slot kWh |
| | `test_profile_is_immutable` | `ChargingProfileSlot` is frozen |
| **Node wiring** | `test_charging_profile_node_produces_profile_from_inputs` | `ChargingProfileNode.run(ctx)` reads inputs from `ctx.secondary` and deposits `ChargingProfile` |

### What is intentionally **not** covered here

- **End-to-end DAG wiring with the full engine** — covered in `tests/test_coordinator.py`; the node-wiring test above runs `ChargingProfileNode` directly against a hand-built `NodeContext`.
- **Optimizer interaction with `ChargingProfile`** — the optimizer does not consume `ChargingProfile` today; if a future tier-4 node starts to, its tests will exercise the integration.
- **Tariff-formula correctness** — covered in `tests/test_tariff.py`; this module trusts whatever `sell_eur_kwh` the price slot reports.
