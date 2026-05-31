# Inbound — Generation module

Reference for `custom_components/sun_sale/inbound/generation.py`.

The generation module produces an **`ObservedGenerationSeries`** — per-slot kWh actually produced by the PV system, aligned to the same grid as `PriceSeries` and covering **yesterday 00:00 → now**. It is pure Python with no Home Assistant imports.

It is the *observed* counterpart to `inbound/forecast.py`: forecast predicts future generation; this module measures past generation.

## Summary

Two translators feed the helper:

- **Primary path** — `PvPowerTranslator` snapshots the inverter's instantaneous PV power (W) each cycle. The helper averages those samples within each price-grid slot and converts to kWh: `slot_kwh = mean(power_W) × slot_duration_h / 1000`.
- **Fallback path** — `GenerationTranslator` snapshots the inverter's daily-resetting today-total kWh counter. When no PV-power samples exist, the helper differences successive counter samples (with daily-reset handling) and resamples onto the price grid.

After slots are built, an **end-of-day proportional correction** scales today's slots to match the most recent today-total counter reading when one is present and the correction factor falls within `[0.5, 2.0]`. The output, `ObservedGenerationSeries`, covers past slots only.

- **Exposes:** `PvPowerTranslator`, `GenerationTranslator` (translators), `build_observed_generation_series` (helper).
- **Depends on:** `contract.models`.
- **Tests:** `tests/test_generation_inbound.py` — full per-test coverage table in §9.

## Contents

1. [Responsibilities](#1-responsibilities)
2. [Public API](#2-public-api)
3. [Inputs](#3-inputs)
4. [Output: `ObservedGenerationSeries`](#4-output-observedgenerationseries)
5. [Per-slot energy derivation](#5-per-slot-energy-derivation)
6. [Window and clamping](#6-window-and-clamping)
7. [Per-day totals](#7-per-day-totals)
8. [Integration in the DAG](#8-integration-in-the-dag)
9. [Tests](#9-tests)

---

## 1. Responsibilities

- Reconstruct per-slot kWh on the `PriceSeries` grid via the primary power-averaging path; fall back to counter differencing when PV-power history is empty.
- Emit one slot per price-grid slot in the window `[yesterday 00:00 local, now)` — historical/observed data, not future projections.
- Apply an end-of-day proportional correction so today's slot sum matches the inverter's authoritative today-total counter (skipped when the correction factor is out of `[0.5, 2.0]`).
- Compute two scalar totals: `total_yesterday_kwh`, `total_today_so_far_kwh`.
- Survive sampling gaps and midnight resets via per-day grouping and the midnight=0 anchor on the counter path (the cross-midnight delta is never computed; each day stands alone).

What this module deliberately does **not** do:

- It does not read Home Assistant state. The translators snapshot HA; the coordinator persists the rolling histories and deposits `PvPowerHistory` and `GenerationHistory` as primary.
- It does not back-fill missing power samples beyond a slot. A slot with zero PV-power samples gets `0.0` kWh on the power path; if the counter path is in use, samples within the day are linearly interpolated.
- It does not look ahead. Slots whose `start >= now` are not emitted, and the slot in progress is clamped to end at `now`.

---

## 2. Public API

```python
def build_observed_generation_series(
    pv_power_history: PvPowerHistory,
    generation_history: GenerationHistory,
    price_slots: tuple,                   # PriceSeries.slots
    now: datetime | None = None,
    local_tz: tzinfo = timezone.utc,
) -> ObservedGenerationSeries
```

`now` defaults to `datetime.now(timezone.utc)`. It is recorded as `computed_at`, is the right edge of the emitted window (slots whose `end` falls past `now` are clamped), and is the reference for the yesterday/today date buckets.

`local_tz` controls day-boundary calculations (yesterday-start, today-start). Defaults to UTC; the coordinator passes `ctx.config.local_tz`.

Decision tree at call time:

1. If `price_slots` is empty → empty series.
2. Else if `pv_power_history.samples` is non-empty → power-averaging path (§5a).
3. Else if `generation_history.samples` is non-empty → counter-differencing path (§5b).
4. Else → empty series.
5. After steps 2 or 3, if `generation_history` has a today reading, apply the end-of-day correction to today's slots (§5c).

---

## 3. Inputs

**`PvPowerHistory`** — `tuple[PvPowerReading, ...]` ordered by `timestamp` ascending. Each `PvPowerReading` is `(power_w: float, timestamp: datetime)` — a single snapshot of the inverter's instantaneous PV power. Built by the coordinator across cycles and persisted; trimmed to the configured retention window (see `contract/const.py`). This is the **primary** input used to derive per-slot kWh.

**`GenerationHistory`** — `tuple[GenerationReading, ...]` ordered by `timestamp` ascending. Each `GenerationReading` is `(today_total_kwh: float, timestamp: datetime)` — a single snapshot of the inverter's daily-resetting today-total cumulative-kWh counter (updates roughly every 10 min). Used in two ways: (a) as a fallback when no PV-power samples exist, and (b) as the authoritative end-of-day anchor that scales the power-averaged slots into agreement with the inverter's own daily total.

**`price_slots`** — the slot tuple from the 72h `PriceSeries` produced by `PricingNode`. Used as the single source of truth for slot boundaries and resolution; the generation module never re-derives them.

---

## 4. Output: `ObservedGenerationSeries`

```python
@dataclass(frozen=True)
class ObservedGenerationSeries:
    slots: tuple[ObservedGenerationSlot, ...]
    computed_at: datetime
    total_yesterday_kwh: float = 0.0
    total_today_so_far_kwh: float = 0.0
```

Each `ObservedGenerationSlot`:

| Field | Meaning |
|---|---|
| `start`, `end` | UTC slot boundaries, identical to the matching `PriceSlot` |
| `generated_kwh` | Per-slot kWh derived via either the power-averaging path (§5a, primary) or the counter-differencing path (§5b, fallback), then optionally scaled by the end-of-day correction (§5c). `0.0` where the slot has no inputs at all. |
| `source` | `"inverter"` on every slot |

Slots whose `start < yesterday 00:00 local` or `start >= now` are omitted entirely — the result is a contiguous run only within that window. Inside the window, every price slot has exactly one corresponding generation slot.

---

## 5. Per-slot energy derivation

### 5a. Primary path — instantaneous power averaging

Used whenever `pv_power_history.samples` is non-empty. For each in-window price slot:

```
slot_kwh = mean(power_W for sample.timestamp in [slot.start, end_clamped)) × duration_h / 1000
```

`end_clamped = min(slot.end, now)` for the in-progress slot; full slots use `slot.end`. Slots with no samples in their interval get `0.0`. This path does not interpolate across gaps — gaps just produce zero-kWh slots until samples arrive. Sampling cadence is the coordinator update interval, so under normal operation every slot sees several samples.

### 5b. Fallback path — counter differencing

Used when `pv_power_history.samples` is empty but `generation_history.samples` is non-empty. Per-slot kWh is computed directly from the cumulative counter, not by differencing consecutive raw samples:

```
generated_kwh(slot) = max(0, today_total(slot.end) − today_total(slot.start))
```

`today_total(t)` is estimated by `_total_at(t)`:

1. **Group samples by UTC day**, keeping only the segment *after the last in-day reset*. Within a day, a reset (`curr.today_total_kwh < prev.today_total_kwh`) starts a new segment; the segment ending the day is retained so interpolation reflects the live counter rather than a stale pre-reset value.
2. **Implicit anchor at `(UTC midnight, 0)`**: a counter reset at midnight is never observed as a sample; the day's first sample is interpolated linearly from `(midnight, 0)` to `(first_sample.timestamp, first_sample.today_total_kwh)`.
3. **Between adjacent samples** within a day: linear interpolation.
4. **After the last sample** of a day: clamped to that sample's value (no extrapolation).
5. **Day with no samples**: returns `0.0`.

Why this scheme rather than pairwise differencing:

- The cross-midnight delta is never computed. Each day's totals stand on their own, anchored at zero — so a reset at midnight needs no special-case handling.
- A slot that contains a reset *during the day* (rare; e.g. an inverter restart) is handled by the per-day "keep only the post-last-reset segment" rule.
- Sampling gaps (coordinator offline for an hour) yield a single straight-line ramp across the gap rather than a chunk that has to be re-split per slot.

The `max(0, …)` guard on the per-slot delta protects against floating-point underflow or pathological out-of-order interpolation; it is not the primary reset-handling mechanism.

### 5c. End-of-day proportional correction

After §5a or §5b builds the slots, the helper looks up `today_total = _latest_today_total(generation_history.samples, now, local_tz)` — the most recent counter reading whose timestamp falls on today's local-tz date. If present:

```
factor = today_total / sum(slot.generated_kwh for slot in today's slots)
```

When `0.5 <= factor <= 2.0`, every today slot is scaled by `factor`. When the factor is `None` (no today reading), out-of-bounds (suggests a faulty sensor or impossibly large divergence), or the today-slot sum is `0.0`, the correction is skipped. Yesterday slots are never scaled — the counter resets at midnight, so the previous day's authoritative total is lost by the time today's history is being corrected.

This brings the power-averaged slots into agreement with the inverter's own daily total at end of day, while preserving the per-slot *shape* (which the counter alone cannot give due to its ~10-minute update cadence).

---

## 6. Window and clamping

A slot is included iff `yesterday_00:00 <= slot.start < now`. This single rule handles three boundary cases: slots before yesterday, slots in tomorrow, and slots in the future of `now`.

For the slot that contains `now` (i.e., `slot.start < now <= slot.end`), the right edge is clamped: the kWh is computed against `min(slot.end, now)`, so `total_today_so_far_kwh` reflects only the elapsed portion of the in-progress slot. The slot's `end` field on the emitted `ObservedGenerationSlot` is still the original `slot.end` (the grid is preserved); only the value is partial.

---

## 7. Per-day totals

| Field | Slots included |
|---|---|
| `total_yesterday_kwh` | `slot.start.date() == now.date() - 1 day` |
| `total_today_so_far_kwh` | `slot.start.date() == now.date()` |

Both are rounded to 4 decimals. `total_today_so_far_kwh` reflects energy actually observed in slots that have already started — it grows monotonically through the day (modulo the dropped midnight interval).

---

## 8. Integration in the DAG

`ObservedGenerationNode` (`pipeline/nodes/tier2.py`):

```python
tier = 2
output_type = ObservedGenerationSeries
consumes = [PvPowerHistory, GenerationHistory, PriceSeries]
```

Tier 2 because it depends on the T1 secondary `PriceSeries`. `PvPowerHistory` and `GenerationHistory` are primary, deposited by the coordinator after appending the current cycle's samples.

Coordinator flow (`orchestration/coordinator.py:_async_update_data`):

1. Run translators → `primary[PvPowerReading]` (instantaneous power snapshot from the inverter) and `primary[GenerationReading]` (today-total counter snapshot). Either may be `None` if the sensor is missing/unavailable.
2. Coordinator appends each non-`None` reading to its respective `PersistentStore[T]` (`pv_power_samples`, `generation_samples`) with retention trimming.
3. `primary[PvPowerHistory]` and `primary[GenerationHistory]` are deposited from the current store contents.
4. `DagEngine.run(primary, ...)` → `PricingNode` produces `PriceSeries` → `ObservedGenerationNode` calls `build_observed_generation_series(pv_power_history, generation_history, price_series.slots, now=ctx.now, local_tz=ctx.config.local_tz)` → `secondary[ObservedGenerationSeries]`.
5. `_build_sensor_dict` exposes the value under the `"observed_generation"` key.

Either translator returning `None` simply skips that cycle's append — existing history still feeds the node. The module's empty-history guards cover fresh installs (returns an empty series when both histories are empty).

---

## 9. Tests

All generation-inbound tests live in `tests/test_generation_inbound.py` and run under pure-Python pytest (no HA harness needed). Shared fixtures come from `tests/conftest.py` — `BASE_DT`, `make_price()`, and `default_tariff_config()`.

Run them with:

```bash
./venv/bin/python -m pytest tests/test_generation_inbound.py -v
```

### Coverage

| Group | Test | What it checks |
|---|---|---|
| Empty / degenerate | `test_empty_history_yields_empty_series` | Both histories empty → empty `slots`, totals = 0 |
| | `test_single_counter_sample_cannot_be_differenced` | One counter sample alone produces nothing (counter path) |
| | `test_empty_price_grid_yields_empty_series` | No price slots → no generation slots even with valid history |
| Counter path (§5b) | `test_one_interval_fully_inside_one_slot` | 2 kWh delta over one hour lands fully in the matching hourly slot |
| | `test_interval_spanning_two_slots_split_by_overlap` | 4 kWh over 10:30–11:30 → 2 kWh in hour-10 + 2 kWh in hour-11 (linear interpolation midway) |
| | `test_multiple_intervals_aggregate_within_slot` | Two consecutive 15-min deltas sum into hour-10 via `today_total(end) − today_total(start)` |
| | `test_reset_handled_by_per_day_grouping` | Yesterday's last counter value and today's reset-to-zero are handled by per-day grouping with the midnight=0 anchor — no special pairwise drop needed |
| | `test_resamples_onto_quarter_hour_grid` | 1h counter span carrying 4 kWh produces four 1 kWh slots on a 15-min grid (linear interpolation across the hour) |
| Power path (§5a) | `test_power_averaging_one_sample_per_slot` | A single power sample inside a slot maps to `power_W × duration_h / 1000` |
| | `test_power_averaging_multiple_samples_averaged` | Multiple samples in a slot are averaged before conversion |
| | `test_power_slot_with_no_samples_gives_zero` | No samples in a slot → `0.0` kWh (no interpolation across the gap) |
| | `test_power_path_covers_yesterday_slots` | Power path covers the full `[yesterday 00:00, now)` window, not just today |
| | `test_power_path_takes_precedence_over_counter` | When both histories are non-empty, the power path is used and the counter path is skipped |
| End-of-day correction (§5c) | `test_correction_scales_today_slots_to_counter_total` | Today slots scaled by `today_total / sum(today_slot_kwh)` |
| | `test_correction_does_not_modify_yesterday_slots` | Yesterday slots untouched by the correction |
| | `test_correction_skipped_when_factor_out_of_range` | Factor outside `[0.5, 2.0]` → correction skipped |
| | `test_correction_skipped_when_no_counter_reading` | No today counter reading → correction skipped |
| Window | `test_slots_in_tomorrow_excluded` | Tomorrow's price slots never appear in output |
| | `test_slots_starting_at_or_after_now_excluded` | `slot.start >= now` excluded; the in-progress slot is included |
| | `test_slots_before_yesterday_midnight_excluded` | Samples from two days ago don't leak slots into output |
| Per-day totals | `test_totals_split_between_yesterday_and_today` | Yesterday/today totals bucketed by `start.date()` |
| Source label | `test_source_is_inverter_on_every_slot` | Every emitted slot has `source == "inverter"` |
| **Node wiring** | `test_observed_generation_node_produces_series_from_primary_and_secondary` | `ObservedGenerationNode.run(ctx)` reads `PvPowerHistory` + `GenerationHistory` from primary + `PriceSeries` from secondary, deposits result at `ctx.secondary[ObservedGenerationSeries]` |

### What is intentionally **not** covered here

- **HA-side counter reading** (`GenerationTranslator` parsing the sensor state) — translator-level concern, covered alongside the other translator tests in `tests/test_coordinator.py` when extended.
- **Persistent sample-history I/O** — coordinator's responsibility (load on `async_setup`, append + trim + save in `_async_update_data`); not yet covered in `tests/test_coordinator.py`.
- **End-to-end DAG wiring with the full engine** — exercised via the node-wiring test against a hand-built `NodeContext`; the engine-level tests live in `tests/test_coordinator.py`.
