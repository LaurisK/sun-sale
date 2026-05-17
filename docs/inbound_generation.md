# Inbound — Generation module

Reference for `custom_components/sun_sale/inbound/generation.py`.

The generation module produces an **`ObservedGenerationSeries`** — per-slot kWh actually produced by the PV system, aligned to the same grid as `PriceSeries` and covering **yesterday 00:00 → now**. It is pure Python with no Home Assistant imports.

It is the *observed* counterpart to `inbound/forecast.py`: forecast predicts future generation; this module measures past generation.

## Contents

1. [Responsibilities](#1-responsibilities)
2. [Public API](#2-public-api)
3. [Inputs](#3-inputs)
4. [Output: `ObservedGenerationSeries`](#4-output-observedgenerationseries)
5. [Per-slot energy from the cumulative counter](#5-per-slot-energy-from-the-cumulative-counter)
6. [Window and clamping](#6-window-and-clamping)
7. [Per-day totals](#7-per-day-totals)
8. [Integration in the DAG](#8-integration-in-the-dag)
9. [Tests](#9-tests)

---

## 1. Responsibilities

- Reconstruct per-slot kWh from the inverter's daily-resetting cumulative counter (sampled each coordinator cycle) on the `PriceSeries` grid.
- Compute `generated_kwh = today_total(slot.end) - today_total(slot.start)` per slot, where `today_total(t)` is linearly interpolated from the day's samples and anchored at `(UTC midnight, 0)` — see §5.
- Emit one slot per price-grid slot in the window `[yesterday 00:00 UTC, now)` — historical/observed data, not future projections.
- Compute two scalar totals: `total_yesterday_kwh`, `total_today_so_far_kwh`.
- Survive sampling gaps and midnight resets via per-day grouping and the midnight=0 anchor (the cross-midnight delta is never computed; each day stands alone).

What this module deliberately does **not** do:

- It does not read Home Assistant state. The translator (`GenerationTranslator`) snapshots the counter; the coordinator persists the rolling history and deposits `GenerationHistory` as primary.
- It does not back-fill missing samples beyond the linear interpolation between adjacent samples within a day.
- It does not look ahead. Slots whose `start >= now` are not emitted, and the slot in progress is clamped to end at `now`.

---

## 2. Public API

```python
def build_observed_generation_series(
    history: GenerationHistory,
    price_series: PriceSeries,
    now: datetime | None = None,
) -> ObservedGenerationSeries
```

`now` defaults to `datetime.now(timezone.utc)`. It is recorded as `computed_at`, is the right edge of the emitted window (slots whose `end` falls past `now` are clamped), and is the reference for the yesterday/today date buckets.

If `history.samples` is empty or `price_series.slots` is empty, an empty `ObservedGenerationSeries` is returned. A single sample on a day will still be interpolated against the midnight=0 anchor, but in practice it produces no meaningful slots until at least two samples exist within the window.

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
| `generated_kwh` | `max(0, today_total(end_clamped) − today_total(start))`, rounded to 6 decimals; `0.0` where the day has no samples or the counter is flat across the slot |
| `source` | `"inverter"` on every slot |

Slots whose `start < yesterday 00:00 UTC` or `start >= now` are omitted entirely — the result is a contiguous run only within that window. Inside the window, every price slot has exactly one corresponding generation slot.

---

## 5. Per-slot energy from the cumulative counter

Per-slot kWh is computed directly from the cumulative counter, not by differencing consecutive raw samples:

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
| Per-slot interpolation | `test_one_interval_fully_inside_one_slot` | 2 kWh delta over one hour lands fully in the matching hourly slot |
| | `test_interval_spanning_two_slots_split_by_overlap` | 4 kWh over 10:30–11:30 → 2 kWh in hour-10 + 2 kWh in hour-11 (linear interpolation midway) |
| | `test_multiple_intervals_aggregate_within_slot` | Two consecutive 15-min deltas sum into hour-10 via `today_total(end) − today_total(start)` |
| Midnight reset | `test_reset_handled_by_per_day_grouping` | Yesterday's last counter value and today's reset-to-zero are handled by per-day grouping with the midnight=0 anchor — no special pairwise drop needed |
| Window | `test_slots_in_tomorrow_excluded` | Tomorrow's price slots never appear in output |
| | `test_slots_starting_at_or_after_now_excluded` | `slot.start >= now` excluded; the in-progress slot is included |
| | `test_slots_before_yesterday_midnight_excluded` | Samples from two days ago don't leak slots into output |
| Per-day totals | `test_totals_split_between_yesterday_and_today` | Yesterday/today totals bucketed by `start.date()` |
| Source label | `test_source_is_inverter_on_every_slot` | Every emitted slot has `source == "inverter"` |
| Grid resolution | `test_resamples_onto_quarter_hour_grid` | 1h counter span carrying 4 kWh produces four 1 kWh slots on a 15-min grid (linear interpolation across the hour) |
| **Node wiring** | `test_observed_generation_node_produces_series_from_primary_and_secondary` | `ObservedGenerationNode.run(ctx)` reads `GenerationHistory` from primary + `PriceSeries` from secondary, deposits result at `ctx.secondary[ObservedGenerationSeries]` |

### What is intentionally **not** covered here

- **HA-side counter reading** (`GenerationTranslator` parsing the sensor state) — translator-level concern, covered alongside the other translator tests in `tests/test_coordinator.py` when extended.
- **Persistent sample-history I/O** — coordinator's responsibility (load on `async_setup`, append + trim + save in `_async_update_data`); not yet covered in `tests/test_coordinator.py`.
- **End-to-end DAG wiring with the full engine** — exercised via the node-wiring test against a hand-built `NodeContext`; the engine-level tests live in `tests/test_coordinator.py`.
