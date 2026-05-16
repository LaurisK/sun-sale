# Inbound — Generation module

Reference for `custom_components/sun_sale/inbound/generation.py`.

The generation module produces an **`ObservedGenerationSeries`** — per-slot kWh actually produced by the PV system, aligned to the same grid as `PriceSeries` and covering **yesterday 00:00 → now**. It is pure Python with no Home Assistant imports.

It is the *observed* counterpart to `inbound/forecast.py`: forecast predicts future generation; this module measures past generation.

## Contents

1. [Responsibilities](#1-responsibilities)
2. [Public API](#2-public-api)
3. [Inputs](#3-inputs)
4. [Output: `ObservedGenerationSeries`](#4-output-observedgenerationseries)
5. [Differencing the cumulative counter](#5-differencing-the-cumulative-counter)
6. [Resampling and window](#6-resampling-and-window)
7. [Per-day totals](#7-per-day-totals)
8. [Integration in the DAG](#8-integration-in-the-dag)
9. [Tests](#9-tests)

---

## 1. Responsibilities

- Difference the inverter's daily-resetting cumulative kWh counter (sampled each coordinator cycle) into per-interval energy.
- Resample those intervals onto the `PriceSeries` grid using the same overlap-weighted formula as `inbound/forecast.py`.
- Emit only slots in the window `[yesterday 00:00 UTC, now)` — historical/observed data, not future projections.
- Compute two scalar totals: `total_yesterday_kwh`, `total_today_so_far_kwh`.
- Survive sampling gaps and midnight resets gracefully (intervals that cross a counter reset are dropped — see §5).

What this module deliberately does **not** do:

- It does not read Home Assistant state. The translator (`GenerationTranslator`) snapshots the counter; the coordinator persists the rolling history and deposits `GenerationHistory` as primary.
- It does not back-fill missing samples or interpolate. If the coordinator was offline for an hour, that hour's interval is one wide chunk distributed by area weight across the slots it overlaps.
- It does not look ahead. Slots whose `start >= now` are not emitted.

---

## 2. Public API

```python
def build_observed_generation_series(
    history: GenerationHistory,
    price_series: PriceSeries,
    now: datetime | None = None,
) -> ObservedGenerationSeries
```

`now` defaults to `datetime.now(timezone.utc)`. It is recorded as `computed_at`, is the right edge of the emitted window, and is the reference for the yesterday/today date buckets.

If `history.samples` has fewer than two entries, or `price_series.slots` is empty, an empty `ObservedGenerationSeries` is returned.

---

## 3. Inputs

**`GenerationHistory`** — `tuple[GenerationReading, ...]` ordered by `timestamp` ascending. Each `GenerationReading` is `(today_total_kwh: float, timestamp: datetime)` — a single snapshot of the inverter's today-total cumulative-kWh counter. Built up by the coordinator across cycles and persisted to `STORAGE_KEY_GENERATION`; trimmed to the last `GENERATION_HISTORY_RETENTION_DAYS` (currently 2) before each save.

**`PriceSeries`** — the 72h price grid produced by `PricingNode`. Used as the single source of truth for slot boundaries and resolution; the generation module never re-derives them.

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
| `generated_kwh` | Overlap-weighted observed energy for this slot, rounded to 6 decimals; `0.0` where no observed interval overlaps |
| `source` | `"inverter"` on every slot |

Slots whose `start < yesterday 00:00 UTC` or `start >= now` are omitted entirely — the result is a contiguous run only within that window. Inside the window, every price slot has exactly one corresponding generation slot.

---

## 5. Differencing the cumulative counter

For each consecutive pair `(prev, curr)` in `history.samples`:

- `curr.timestamp <= prev.timestamp` → ignored (out-of-order).
- `curr.today_total_kwh < prev.today_total_kwh` → **counter reset detected**; the interval is dropped and `prev` advances to `curr`.
- Otherwise the interval `(prev.timestamp, curr.timestamp, curr.today_total_kwh - prev.today_total_kwh)` is emitted.

The cross-midnight interval is intentionally lost. The alternative — splitting the unknown energy across the midnight boundary — would need a "midnight value" we don't have. At a 5-minute sampling cadence the discarded amount is at most one cycle of generation near midnight, which is negligible.

---

## 6. Resampling and window

Same formula as `inbound/forecast.py`:

```
generated_kwh(slot) = Σ interval_kwh * overlap_seconds / interval_duration_seconds
```

over all observed intervals that intersect the target slot. The window filter is applied at slot emission: a slot is included iff `yesterday_00:00 <= slot.start < now`. This single rule handles all three boundary cases (slots before yesterday, slots in tomorrow, slots in the future of `now`).

The area-weighted formula handles arbitrary sampling cadence: a sparse 1h interval will be split proportionally across four 15-min slots; densely-sampled 5-min intervals all sum cleanly into the slot that contains them.

---

## 7. Per-day totals

| Field | Slots included |
|---|---|
| `total_yesterday_kwh` | `slot.start.date() == now.date() - 1 day` |
| `total_today_so_far_kwh` | `slot.start.date() == now.date()` |

Both are rounded to 4 decimals. `total_today_so_far_kwh` reflects energy actually observed in slots that have already started — it grows monotonically through the day (modulo the dropped midnight interval).

---

## 8. Integration in the DAG

`ObservedGenerationNode` (`pipeline/nodes.py`):

```python
tier = 2
output_type = ObservedGenerationSeries
consumes = [GenerationHistory, PriceSeries]
```

Tier 2 because it depends on the T1 secondary `PriceSeries`. `GenerationHistory` is primary, deposited by the coordinator after appending the current cycle's sample.

Coordinator flow (`orchestration/coordinator.py:_async_update_data`):

1. Run translators → `primary[GenerationReading]` (current snapshot from `sensor.<...solar_energy>`).
2. Coordinator appends the new reading to `self._generation_samples`, trims everything older than `GENERATION_HISTORY_RETENTION_DAYS`, and persists to `STORAGE_KEY_GENERATION`.
3. `primary[GenerationHistory] = GenerationHistory(samples=tuple(self._generation_samples))` is deposited.
4. `DagEngine.run(primary, ...)` → `PricingNode` produces `PriceSeries` → `ObservedGenerationNode` calls `build_observed_generation_series(history, price_series, now=ctx.now)` → `secondary[ObservedGenerationSeries]`.
5. `_build_sensor_dict` exposes the value under the `"observed_generation"` key.

The translator returns `None` when the sensor is unset, unavailable, or unparseable; the coordinator handles `None` by not appending to history that cycle (but still deposits the existing history as `GenerationHistory`). The inbound module's "fewer than two samples → empty series" guard covers fresh installs.

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
| Empty / degenerate | `test_empty_history_yields_empty_series` | Zero samples → empty `slots`, totals = 0 |
| | `test_single_sample_cannot_be_differenced` | One sample alone produces nothing |
| | `test_empty_price_grid_yields_empty_series` | No price slots → no generation slots even with valid history |
| Per-slot differencing | `test_one_interval_fully_inside_one_slot` | 2 kWh delta over one hour lands fully in the matching hourly slot |
| | `test_interval_spanning_two_slots_split_by_overlap` | 4 kWh over 10:30–11:30 → 2 kWh in hour-10 + 2 kWh in hour-11 |
| | `test_multiple_intervals_aggregate_within_slot` | Two consecutive 15-min deltas sum into hour-10 |
| Midnight reset | `test_reset_interval_dropped_when_counter_resets` | `curr < prev` interval dropped; following same-day interval kept |
| Window | `test_slots_in_tomorrow_excluded` | Tomorrow's price slots never appear in output |
| | `test_slots_starting_at_or_after_now_excluded` | `slot.start >= now` excluded; the in-progress slot is included |
| | `test_slots_before_yesterday_midnight_excluded` | Samples from two days ago don't leak slots into output |
| Per-day totals | `test_totals_split_between_yesterday_and_today` | Yesterday/today totals bucketed by `start.date()` |
| Source label | `test_source_is_inverter_on_every_slot` | Every emitted slot has `source == "inverter"` |
| Resampling | `test_resamples_onto_quarter_hour_grid` | 1h interval carrying 4 kWh produces four 1 kWh slots on a 15-min grid |
| **Node wiring** | `test_observed_generation_node_produces_series_from_primary_and_secondary` | `ObservedGenerationNode.run(ctx)` reads `GenerationHistory` from primary + `PriceSeries` from secondary, deposits result at `ctx.secondary[ObservedGenerationSeries]` |

### What is intentionally **not** covered here

- **HA-side counter reading** (`GenerationTranslator` parsing the sensor state) — translator-level concern, covered alongside the other translator tests in `tests/test_coordinator.py` when extended.
- **Persistent sample-history I/O** — coordinator's responsibility (load on `async_setup`, append + trim + save in `_async_update_data`); not yet covered in `tests/test_coordinator.py`.
- **End-to-end DAG wiring with the full engine** — exercised via the node-wiring test against a hand-built `NodeContext`; the engine-level tests live in `tests/test_coordinator.py`.
