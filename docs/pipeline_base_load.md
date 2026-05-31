# Pipeline — Base load module

Reference for `custom_components/sun_sale/pipeline/base_load.py`.

The base-load module produces two values: a **`BaseLoadProfile`** (24-hour low-percentile floor of household consumption, keyed by local hour-of-day) and a **`BatteryRuntimeEstimate`** (how long the battery can sustain that baseload before hitting `min_soc`). It is pure Python with no Home Assistant imports.

## Summary

24h hour-of-day baseload profile from `HouseholdLoadReading` history (P10 per bucket, local-time keyed) plus battery-runtime worst-case estimate.

- **Exposes:** `BaseLoadProfile`, `BatteryRuntimeEstimate`.
- **Depends on:** `contract.models`.
- **Tests:** `tests/test_base_load.py` — full per-test coverage table in §9.

## Contents

1. [Responsibilities](#1-responsibilities)
2. [Public API](#2-public-api)
3. [Inputs](#3-inputs)
4. [Output: `BaseLoadProfile`](#4-output-baseloadprofile)
5. [Output: `BatteryRuntimeEstimate`](#5-output-batteryruntimeestimate)
6. [Profile algorithm](#6-profile-algorithm)
7. [Runtime-estimate algorithm](#7-runtime-estimate-algorithm)
8. [Integration in the DAG](#8-integration-in-the-dag)
9. [Tests](#9-tests)

---

## 1. Responsibilities

- Bucket historical household-load samples by **local hour-of-day** (24 buckets, no weekend/holiday split) and emit a P10 floor per bucket — the load level the home reliably draws even in its quietest minute of that hour.
- Always emit an exhaustive 24-slot profile: sparse buckets get a cross-bucket fallback, and a fully empty history gets a stub of `0.2 kW`. Downstream code never has to handle `None`.
- Forward-simulate **pure baseload drain** of the battery from `now` over a fixed horizon and report `runtime_minutes` + an absolute `until` timestamp.
- Be re-entrant and stateless: every call rebuilds the profile and re-runs the simulation from the inputs provided.

What this module deliberately does **not** do:

- It does not look at weekday/weekend/holiday classes. Day-class normalisation is reserved for `pipeline/profitability.py`; baseload is intentionally simpler.
- It does not model forecast solar generation in the runtime estimate. The output is a worst-case "household-only depletion" reserve, comparable across cycles and unaffected by weather noise.
- It does not model the scheduler's planned charge/discharge. Same reason: keep the estimate a fixed lower bound on the time we have before household consumption alone empties the battery.
- It does not own persistence. The coordinator appends each cycle's sample to `STORAGE_KEY_HOUSEHOLD_LOAD` and deposits the full `HouseholdLoadHistory` as primary; this module only reads it.

---

## 2. Public API

```python
def build_base_load_profile(
    history: HouseholdLoadHistory,
    local_tz: tzinfo,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,        # 30
    percentile: float = DEFAULT_PERCENTILE,        # 0.10
) -> BaseLoadProfile

def estimate_battery_runtime(
    battery_status: BatteryStatus,
    battery_config: BatteryConfig,
    profile: BaseLoadProfile,
    local_tz: tzinfo,
    now: datetime,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,    # 48
) -> BatteryRuntimeEstimate
```

`now` on `build_base_load_profile` defaults to `datetime.now(timezone.utc)` and is recorded as `BaseLoadProfile.computed_at`. It is also the right edge of the rolling window — only samples with `timestamp >= now - window_days` are considered. `now` on `estimate_battery_runtime` is required (the runtime estimate is meaningless without an explicit anchor).

Module-level tunables, all importable for tests:

| Constant | Default | Meaning |
|---|---|---|
| `MIN_HISTORY_DAYS` | `7` | Distinct local-date days required in the window before per-bucket percentiles are computed. Below this, the profile is sparse — every slot gets the fallback. |
| `MIN_BUCKET_SAMPLES` | `6` | Samples required in a single hour-bucket before it gets its own P10. Below this, that hour falls back. |
| `DEFAULT_PERCENTILE` | `0.10` | Per-bucket percentile (the "floor"). |
| `DEFAULT_FALLBACK_PERCENTILE` | `0.20` | Cross-bucket fallback percentile — wider than P10 because it's used when a single bucket is thin. |
| `DEFAULT_STUB_KW` | `0.2` | Last-resort floor on a fresh install with literally no samples. Matches `inbound/battery._DEFAULT_HOUSEHOLD_LOAD_KW`. |
| `DEFAULT_WINDOW_DAYS` | `30` | Rolling window for samples. |
| `DEFAULT_HORIZON_HOURS` | `48` | How far ahead `estimate_battery_runtime` simulates before giving up and returning `until=None`. |
| `SIMULATION_STEP_MINUTES` | `5` | Simulation step. Matches the coordinator update interval and gives sub-percent precision on the `until` timestamp. |

---

## 3. Inputs

**`HouseholdLoadHistory`** (`contract/models.py`) — `tuple[HouseholdLoadSample, ...]` sorted ascending by `timestamp`. Each sample is `(timestamp: datetime, load_kw: float)`. Built up by the coordinator across cycles: each cycle, `HouseholdLoadTranslator` reads the configured load sensor and emits a `HouseholdLoadReading | None`; the coordinator appends to a persistent rolling list (`STORAGE_KEY_HOUSEHOLD_LOAD`), trims to `HOUSEHOLD_LOAD_HISTORY_RETENTION_DAYS` (45), and deposits the full history as primary. Crucially the translator returns `None` on absent/unparseable sensor state — the 0.2 kW stub that `BatteryTranslator` uses for `BatteryReading.household_load_kw` is **not** persisted into this history (see `docs/base_load_missing.md` §8).

**`local_tz`** — a `tzinfo` (typically `ZoneInfo("Europe/Riga")`). All hour-of-day bucketing happens in local time. Storage stays tz-aware UTC; only the bucket key is local. The coordinator pulls this from `hass.config.time_zone` via `SunSaleCoordinator._resolve_local_tz` and threads it through `SunSaleConfig.local_tz`, which the DAG node reads as `ctx.config.local_tz`.

**`BatteryStatus`** and **`BatteryConfig`** (runtime estimate only) — `BatteryStatus.total_capacity_kwh` is the configured nominal capacity, `BatteryStatus.soc` is the live SoC, and `BatteryConfig.min_soc` is the floor the simulation drains toward.

---

## 4. Output: `BaseLoadProfile`

```python
@dataclass(frozen=True)
class BaseLoadProfile:
    slots: tuple[BaseLoadSlot, ...]    # length 24, indexed by hour
    fallback_kw: float                  # used for sparse buckets and as a stub
    overall_p10_kw: float               # diagnostic: P10 of all samples
    overall_median_kw: float            # diagnostic
    confidence: float | None            # 0..1, distinct_days / window_days; None when sparse
    sample_count: int                   # total samples in window
    distinct_days: int                  # distinct local-date days in window
    computed_at: datetime

    def at(self, t: datetime, local_tz) -> float
```

Each `BaseLoadSlot`:

| Field | Meaning |
|---|---|
| `hour` | Local hour 0..23. `slots[h].hour == h` always. |
| `baseload_kw` | P10 of in-bucket samples, **or** `profile.fallback_kw` when `sample_count < MIN_BUCKET_SAMPLES`. |
| `sample_count` | Number of samples that fell into this bucket within the rolling window. |
| `is_fallback` | `True` iff `baseload_kw` came from the cross-bucket fallback rather than the bucket's own P10. |

`confidence is None` is the explicit "too sparse to trust" signal: it means fewer than `MIN_HISTORY_DAYS` distinct local-date days fell in the window. In that state every slot is fallback, and `fallback_kw` is the overall P10 of whatever samples did make it (or `DEFAULT_STUB_KW` if there were zero).

`profile.at(t, local_tz)` is a one-liner: `slots[t.astimezone(local_tz).hour].baseload_kw`. Sensors and the runtime simulation both call it.

---

## 5. Output: `BatteryRuntimeEstimate`

```python
@dataclass(frozen=True)
class BatteryRuntimeEstimate:
    remaining_kwh_usable: float         # max(0, (soc − min_soc) * total_capacity)
    avg_drain_kw_next_hour: float       # mean drain over the first simulated hour
    runtime_minutes: float | None
    until: datetime | None              # now + runtime_minutes, tz-aware
    horizon_hours: int
    computed_at: datetime
```

| State | `runtime_minutes` | `until` | Meaning |
|---|---|---|---|
| SoC ≤ min_soc | `0.0` | `now` | Battery already empty (from the reserve's perspective). |
| Drains within horizon | float, minutes | tz-aware datetime in the future | Forward-sim hit zero at this time. |
| Survives full horizon | `None` | `None` | Battery did not drain within `horizon_hours`. Stable signal; the sensor reads "unavailable". |

`avg_drain_kw_next_hour` is the arithmetic mean of `profile.at(t, local_tz)` over the first hour's worth of simulation steps. It's a stable proxy for "how fast are we draining right now" for the dashboard — flickers only when the wall-clock crosses an hour boundary, not on every cycle.

`remaining_kwh_usable` is exposed even in the SoC-at-min case so the UI can show "0.0 of 9.0 kWh usable" rather than "unknown".

---

## 6. Profile algorithm

```
window = [s for s in history.samples if s.timestamp >= now - window_days]
distinct_days = |{ s.timestamp.astimezone(local_tz).date() for s in window }|

if distinct_days < MIN_HISTORY_DAYS:
    fallback = P10(window)  if window else DEFAULT_STUB_KW
    return BaseLoadProfile(
        slots = [BaseLoadSlot(h, fallback, 0, is_fallback=True) for h in 0..23],
        confidence = None,
        ...
    )

buckets[h] = [s.load_kw for s in window if s.timestamp.astimezone(local_tz).hour == h]
fallback_kw = P20(all window values)
for h in 0..23:
    if len(buckets[h]) < MIN_BUCKET_SAMPLES:
        slots[h] = BaseLoadSlot(h, fallback_kw, len(buckets[h]), is_fallback=True)
    else:
        slots[h] = BaseLoadSlot(h, P10(buckets[h]), len(buckets[h]), is_fallback=False)

confidence = min(1.0, distinct_days / window_days)
```

**Percentile interpolation** — `_percentile(values, p)` is a linear-interpolation quantile (same convention as `numpy.percentile` with `interpolation="linear"`): rank `= p * (n - 1)`, then interpolate between the two flanking sorted values. Empty input returns `0.0`; single-element input returns that element.

**Why P10 instead of true minimum** — single-sample minimum is dominated by sensor dropouts and the moment the fridge cycles off. P10 across a month gives the reliable floor — what the home draws almost always — without being fooled by noise.

**Why a separate cross-bucket fallback at P20** — P10 of a thin bucket is unreliable. When a bucket has fewer than `MIN_BUCKET_SAMPLES` observations we'd rather use a slightly conservative cross-bucket estimate (P20 of everything) than commit to a flaky per-bucket P10.

**Why local time** — bucketing by UTC hour would mean the same wall-clock pattern (e.g. "morning coffee at 07:00 local") falls into different buckets depending on DST and timezone. Bucketing locally is what makes the profile meaningful to a human looking at "what does my house draw at 3am?".

---

## 7. Runtime-estimate algorithm

```
usable_kwh = max(0, (status.soc - cfg.min_soc) * status.total_capacity_kwh)

if usable_kwh == 0:
    return BatteryRuntimeEstimate(0, avg, runtime_minutes=0, until=now, ...)

remaining = usable_kwh
elapsed_minutes = 0
t = now

while t < now + horizon_hours:
    drain_kw  = profile.at(t, local_tz)        # local-hour lookup
    drain_kwh = drain_kw * (SIMULATION_STEP_MINUTES / 60)

    if drain_kwh >= remaining:                 # drains mid-step
        fraction = remaining / drain_kwh
        return runtime_minutes = elapsed + STEP * fraction,
               until           = t + STEP * fraction

    remaining       -= drain_kwh
    elapsed_minutes += SIMULATION_STEP_MINUTES
    t += STEP

return runtime_minutes=None, until=None         # survived the horizon
```

A few notes:

- The step is **fixed at 5 minutes** regardless of what other modules use. Independence from the price/generation grid resolution means this works even if those streams are empty.
- The first-hour average is collected during the same loop. When the battery drains mid-step inside the first hour, the partial step is still counted toward the average so a sensor reading taken right at the cutoff still makes sense.
- A bucket with `baseload_kw == 0` (would only happen with a pathological profile) means no drain that hour — the simulation just walks forward.

**Why no schedule / no solar** — the scheduler's planned discharges and the solar forecast are both useful in different views, but they make the runtime estimate noisy and harder to reason about ("we had 8 hours yesterday, now we have 14 — did anything actually change?"). A "what if we did nothing" reserve is monotonic in inputs you can name: SoC, battery size, household pattern, hour of day. If a plan-aware "expected drain under current schedule" metric becomes valuable, it should be its own output type next to this one, not a parameter on this one.

---

## 8. Integration in the DAG

Two nodes — `BaseLoadProfileNode` in `pipeline/nodes/tier1.py`, `BatteryRuntimeNode` in `pipeline/nodes/tier2.py`:

```python
class BaseLoadProfileNode(DagNode):
    tier = 1
    output_type = BaseLoadProfile
    consumes = [HouseholdLoadHistory]

class BatteryRuntimeNode(DagNode):
    tier = 2
    output_type = BatteryRuntimeEstimate
    consumes = [BatteryStatus, BaseLoadProfile]
```

`BaseLoadProfileNode` is T1 because its only input is primary. `BatteryRuntimeNode` is T2 because it depends on `BaseLoadProfile` (T1 secondary) — `BatteryStatus` is also T1 secondary, produced by `BatteryStatusNode`. Both nodes read `ctx.config.local_tz`, which the coordinator populated from `hass.config.time_zone` at setup.

Coordinator flow (`orchestration/coordinator.py:_async_update_data`):

1. Run translators → `primary[HouseholdLoadReading]` (current sample; possibly `None` and therefore absent).
2. If a current sample exists, coordinator appends it to `self._household_load_samples`, trims by `HOUSEHOLD_LOAD_HISTORY_RETENTION_DAYS`, and persists to `STORAGE_KEY_HOUSEHOLD_LOAD`.
3. `primary[HouseholdLoadHistory] = HouseholdLoadHistory(samples=tuple(self._household_load_samples))` is deposited regardless of whether step 2 fired — old samples still produce a profile.
4. `DagEngine.run(primary, ...)` → `BaseLoadProfileNode` produces `BaseLoadProfile` → `BatteryRuntimeNode` produces `BatteryRuntimeEstimate`.
5. `_build_sensor_dict` exposes them under the `"base_load_profile"` and `"battery_runtime"` keys.

Downstream consumers:

- `sensor.CurrentBaseloadSensor` — calls `profile.at(now, local_tz)` for the current numeric value; full 24-slot breakdown lives in `extra_state_attributes`.
- `sensor.BatteryRuntimeMinutesSensor` / `BatteryDrainUntilSensor` / `BaseloadConfidenceSensor` — read the obvious fields straight off the dataclass.

Nothing in the calculation/schedule pipeline consumes `BaseLoadProfile` or `BatteryRuntimeEstimate` today. The profile may eventually replace the 0.2 kW stub inside `BatteryTranslator` (see `docs/base_load_missing.md` §8 — separate ticket).

---

## 9. Tests

All base-load tests live in `tests/test_base_load.py` and run under pure-Python pytest (no HA harness needed). `tests/conftest.py` is loaded for the HA stub modules but is not otherwise relied on — fixtures are inline `_samples()`, `_history()`, `_battery_status()`, `_battery_config()`, `_flat_profile()` helpers at the top of the file.

Run them with:

```bash
./venv/bin/python -m pytest tests/test_base_load.py -v
```

### Coverage

| Group | Test | What it checks |
|---|---|---|
| `_percentile` | `test_percentile_empty_returns_zero` | Empty input → 0.0 (used by sparse path) |
| | `test_percentile_single_value` | Single-element input returns that element regardless of `p` |
| | `test_percentile_p10_of_ten_values` | Interpolation: P10 of [1..10] = 1.9 |
| | `test_percentile_p100_returns_max` | `p=1.0` → max |
| | `test_percentile_p0_returns_min` | `p=0.0` → min |
| Sparsity | `test_empty_history_returns_sparse_profile_with_stub` | Empty history → 24 fallback slots all at `DEFAULT_STUB_KW`, `confidence=None` |
| | `test_below_min_history_days_returns_sparse` | 3 days of samples → sparse profile, but `fallback_kw = overall P10` (not the stub, since we have data) |
| | `test_minimum_history_yields_confidence` | At `MIN_HISTORY_DAYS` UTC days → `confidence == distinct_days / window_days` (note: 24h UTC spans `MIN_HISTORY_DAYS+1` Riga-local dates because UTC 22–23 spill forward) |
| Bucketing | `test_buckets_by_local_hour_not_utc` | A sample at 00:00 UTC in Riga winter (+2) lands in local-hour bucket 2, not bucket 0 |
| | `test_sparse_bucket_uses_fallback` | A bucket with `< MIN_BUCKET_SAMPLES` is marked `is_fallback`, value equals `fallback_kw` |
| | `test_p10_rejects_outlier_spikes` | 10 baseline samples + 1 spike per hour → P10 stays near baseline, spike is rejected |
| | `test_samples_outside_window_excluded` | Samples older than `window_days` don't affect `overall_p10_kw` |
| `BaseLoadProfile.at` | `test_profile_at_uses_local_hour` | `.at(t, local_tz)` indexes by `t.astimezone(local_tz).hour` |
| Runtime estimate | `test_runtime_zero_when_soc_at_min` | SoC == min_soc → `runtime_minutes == 0`, `until == now`, `remaining_kwh_usable == 0` |
| | `test_runtime_constant_drain` | 9 kWh usable @ 1 kW → ≈ 540 min runtime, `avg_drain_kw_next_hour == 1.0` |
| | `test_runtime_partial_step_interpolation` | 0.05 kWh usable @ 1 kW → 3 min runtime (mid-step interpolation in the very first step) |
| | `test_runtime_horizon_limits_simulation` | 90 kWh usable @ 0.5 kW with 2h horizon → `runtime_minutes is None`, `until is None`, `horizon_hours == 2` |
| | `test_runtime_drain_follows_profile_per_hour` | Profile with 4 kW only at local hour 15, 0.5 kW elsewhere → drain rate changes between hours; total ≈ 11h matches manual math |

### What is intentionally **not** covered here

- **`HouseholdLoadTranslator`** — sensor-state parsing (W→kW, clamp, `None`-on-absent). Covered alongside other translator tests in `tests/test_coordinator.py` when that file is extended; not relevant to the pure-Python profile/runtime math.
- **Coordinator persistence I/O** — `STORAGE_KEY_HOUSEHOLD_LOAD` load + append + trim + save. Coordinator's responsibility; same gap exists for `STORAGE_KEY_GENERATION` and is tracked together (see `docs/base_load_missing.md` §10).
- **End-to-end DAG wiring** — `BaseLoadProfileNode` / `BatteryRuntimeNode` registration with the engine and observer wiring. Exercised implicitly by `tests/test_coordinator.py` when the engine runs; the per-node `_compute()` is thin enough that mocking the context to assert "node produces profile" would just retest the function-level coverage above.
- **DST transitions** — the bucketing math works correctly across the spring/autumn DST jump (Python's `astimezone` handles it), but no test asserts behaviour on the specific transition days. Worth adding if a regression ever surfaces; not blocking.
