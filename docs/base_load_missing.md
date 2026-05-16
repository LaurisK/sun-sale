# Base Load — Missing pieces (post-implementation follow-ups)

Companion doc to `pipeline/base_load.py`. Lists everything **outside** `base_load.py` itself that needs to be added or changed to make the baseload + battery-runtime nodes work end-to-end. To be picked up after `base_load.py` and its unit tests are merged.

## Contents

1. [Design decisions baked in](#1-design-decisions-baked-in)
2. [New primary data type](#2-new-primary-data-type)
3. [New translator](#3-new-translator)
4. [Coordinator changes](#4-coordinator-changes)
5. [Constants](#5-constants)
6. [DAG node registration](#6-dag-node-registration)
7. [Sensors](#7-sensors)
8. [The 0.2 kW household-load stub](#8-the-02-kw-household-load-stub)
9. [Local-time invariant](#9-local-time-invariant)
10. [Open items](#10-open-items)

---

## 1. Design decisions baked in

| Decision | Consequence |
|---|---|
| **No holiday / weekend bucketing.** Baseload profile is keyed by hour-of-day only. | `BaseLoadSlot` carries `hour: int` (0–23) and nothing else dimensional. No `DayClass` field, no `is_holiday` parameter anywhere in the baseload module or its node. 24 buckets total, not 72. |
| **All time is local time.** | Bucketing uses `sample.timestamp.astimezone(local_tz).hour`. Runtime-estimate "until" timestamps are also local. Storage / wire formats keep tz-aware ISO strings, but every comparison and bucket key is computed in local tz. |
| **`HouseholdLoadReading` returns `None` on unavailable.** | Coordinator skips appending that cycle. Percentile is never polluted by a default value. Distinct from the legacy 0.2 kW stub used elsewhere — see §8. |

---

## 2. New primary data type

Add to `contract/models.py`:

```python
@dataclass(frozen=True)
class HouseholdLoadReading:
    """Primary: one snapshot of measured household load (per cycle)."""
    timestamp: datetime          # tz-aware; coordinator passes `now`
    load_kw: float               # ≥ 0, W→kW already applied

@dataclass(frozen=True)
class HouseholdLoadSample:
    """Persisted historical sample. Same shape as the reading; separate
    type so storage I/O can evolve independently."""
    timestamp: datetime
    load_kw: float

@dataclass(frozen=True)
class HouseholdLoadHistory:
    """Primary: rolling sample history, sorted ascending by timestamp."""
    samples: tuple[HouseholdLoadSample, ...]
```

Plus the secondary types produced by `base_load.py` itself (`BaseLoadSlot`, `BaseLoadProfile`, `BatteryRuntimeEstimate`) — those are added as part of the base_load implementation, not in this follow-up.

---

## 3. New translator

`HouseholdLoadTranslator` in `inbound/translators.py`. Pattern: clone of `GenerationTranslator` (`inbound/translators.py:337-368`).

**Requirements:**

- `output_type = HouseholdLoadReading`
- Constructor takes `entity_id: str` — the existing `CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD` config key (already defined in `contract/const.py`).
- `parse(hass, now)` is sync, callable from tests.
- Returns `None` when entity is unset, state in `("unavailable", "unknown", "")`, or value fails to parse as float.
- Converts W → kW. Clamps to `max(0.0, value / 1000.0)`.
- **Does not** apply the 0.2 kW stub. Absence is signalled by `None`.
- Async `translate(hass, config, raw_config, now)` returns `parse(hass, now)`.

Register in coordinator `async_setup`'s `self._translators` list alongside the others (`orchestration/coordinator.py:260-275`).

---

## 4. Coordinator changes

Mirror the existing `GenerationReading` → `GenerationHistory` flow exactly (`orchestration/coordinator.py:166-167, 306-315, 386-389, 418-432`).

**New attributes** in `__init__`:

```python
self._household_load_store: Store | None = None
self._household_load_samples: list[HouseholdLoadSample] = []
```

**In `async_setup`:** open the store, load persisted samples:

```python
self._household_load_store = Store(self.hass, STORAGE_VERSION, STORAGE_KEY_HOUSEHOLD_LOAD)
stored = await self._household_load_store.async_load()
if stored:
    self._household_load_samples = [
        HouseholdLoadSample(
            timestamp=datetime.fromisoformat(s["ts"]),
            load_kw=s["kw"],
        )
        for s in stored.get("samples", [])
    ]
```

**In `_async_update_data`,** after generation history is deposited:

```python
current_load: HouseholdLoadReading | None = primary.get(HouseholdLoadReading)
if current_load is not None:
    await self._append_household_load_sample(current_load, now)
primary[HouseholdLoadHistory] = HouseholdLoadHistory(
    samples=tuple(self._household_load_samples)
)
```

**New private method** — straight clone of `_append_generation_sample`:

```python
async def _append_household_load_sample(
    self, reading: HouseholdLoadReading, now: datetime
) -> None:
    cutoff = now - timedelta(days=HOUSEHOLD_LOAD_HISTORY_RETENTION_DAYS)
    kept = [s for s in self._household_load_samples if s.timestamp >= cutoff]
    kept.append(HouseholdLoadSample(timestamp=now, load_kw=reading.load_kw))
    self._household_load_samples = kept
    if self._household_load_store is not None:
        await self._household_load_store.async_save({
            "samples": [
                {"ts": s.timestamp.isoformat(), "kw": s.load_kw}
                for s in self._household_load_samples
            ],
        })
```

---

## 5. Constants

Add to `contract/const.py`:

```python
STORAGE_KEY_HOUSEHOLD_LOAD = "sun_sale_household_load"
HOUSEHOLD_LOAD_HISTORY_RETENTION_DAYS = 45     # baseload window (30d) × 1.5 buffer
```

Imports in `orchestration/coordinator.py` need the two new symbols.

---

## 6. DAG node registration

Two nodes (defined in `pipeline/nodes.py` as part of the base_load implementation) need to be added to the `nodes` list in `coordinator.async_setup` (`orchestration/coordinator.py:280-291`):

- `BaseLoadProfileNode()` — Tier 1, consumes `HouseholdLoadHistory`
- `BatteryRuntimeNode()` — Tier 2, consumes `BatteryStatus`, `BaseLoadProfile`. Intentionally ignores `GenerationSeries` and `Schedule`: this is a worst-case "household-only depletion" reserve, comparable across cycles. Adding solar/schedule modelling is a separate ticket if needed.

No DAG engine changes needed — node registration alone wires the edges.

---

## 7. Sensors

Add to `_build_sensor_dict` (`orchestration/coordinator.py:434-459`):

```python
"base_load_profile": secondary.get(BaseLoadProfile),
"battery_runtime": secondary.get(BatteryRuntimeEstimate),
```

New entities in `sensor.py`:

| Entity | Source | Notes |
|---|---|---|
| `sun_sale_current_baseload_kw` | `BaseLoadProfile.at(now)` | Reads current local hour's bucket |
| `sun_sale_battery_runtime_minutes` | `BatteryRuntimeEstimate.runtime_minutes` | `None` → "not draining" |
| `sun_sale_battery_drain_until` | `BatteryRuntimeEstimate.until` | Local timestamp |
| `sun_sale_baseload_confidence` | `BaseLoadProfile.confidence` | 0–1 or unavailable |

---

## 8. The 0.2 kW household-load stub

**Current state.** `BatteryTranslator` calls `_read_household_load` (`inbound/translators.py:321-330`), which returns `_DEFAULT_HOUSEHOLD_LOAD_KW = 0.2` whenever the configured load sensor is missing, unavailable, or unparseable. The value lands in `BatteryReading.household_load_kw` and is consumed downstream by anything that needs an instantaneous load figure.

**Why a stub at all.** Several downstream calculations (calculator, dashboard, schedule estimation) want a *number*, not `None`. A hardcoded 0.2 kW is a "warm-but-quiet house" guess — wrong, but never wildly wrong, and never zero.

**Why we keep the stub for now.** The new `HouseholdLoadTranslator` only feeds the *baseload history* — there it must signal absence with `None` so the percentile isn't polluted. The pre-existing `BatteryTranslator` path keeps its 0.2 kW fallback so the rest of the pipeline doesn't suddenly start seeing zeros or `None` on installs without a load sensor. The two paths intentionally diverge.

**Planned takeover.** Once `BaseLoadProfile` is in place, a future change will replace the 0.2 kW constant in `BatteryTranslator` (or the consumers of `BatteryReading.household_load_kw`) with a lookup of the form:

```python
profile.at(now)   # local-hour bucket from the rolling profile
```

falling back to 0.2 kW only when the profile itself is sparse (`confidence is None`). That collapses the two divergent paths back into one: real sensor when available → historical baseload when sensor is down → static stub only on a fresh install.

**Action items for the takeover (separate ticket, do not bundle with base_load):**
- Decide whether the lookup happens inside `BatteryTranslator` (needs profile injected — awkward) or inside a new tier-1 node `CurrentBaseLoadNode` that produces a `CurrentBaseLoad(value_kw)` primary-ish type for consumers. Probably the node — cleaner separation.
- Audit current consumers of `BatteryReading.household_load_kw` and switch them to `CurrentBaseLoad` or equivalent.
- Delete `_DEFAULT_HOUSEHOLD_LOAD_KW` from `inbound/translators.py` once nothing references it.

Until that ticket lands, **leave the 0.2 kW constant alone.** It is not a bug — it is the documented stub.

---

## 9. Local-time invariant

This is a project-wide concern that the baseload work surfaces but does not own.

- `base_load.py` itself bucketing must use local time: `sample.timestamp.astimezone(local_tz).hour`. The local tz is read from a new field on `SunSaleConfig` (TBD: `local_tz: tzinfo`) populated by the coordinator from `hass.config.time_zone`.
- `BatteryRuntimeEstimate.until` is rendered to the dashboard / sensors as a local-time `datetime`.
- Sample storage continues to use tz-aware ISO strings (UTC offset preserved); only the bucketing key is local.

**Out of scope for the baseload work** but worth flagging: other modules currently mix `datetime.now(timezone.utc)` (e.g. `orchestration/coordinator.py:341`) with local-date assumptions elsewhere. A separate cleanup pass should normalise the whole pipeline to "store UTC, display local, bucket local" — track separately.

---

## 10. Open items

1. **`SunSaleConfig.local_tz`** — add the field, populate from `hass.config.time_zone` in `async_setup`, thread into `NodeContext` so nodes can read it. Needed before `BaseLoadProfileNode` works correctly.
2. **Config-flow validation for `CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD`** — currently optional. Decide whether to require it for the baseload feature, or surface "baseload disabled" in the UI when missing. Either is fine; just pick one.
3. **`BatteryTranslator` → `HouseholdLoadTranslator` consolidation** — see §8. Out of scope for the baseload work itself; tracked here so the duplicate sensor read doesn't go unnoticed.
4. **Tests for the coordinator's new persistence path** — `tests/test_coordinator.py` does not currently cover generation-history persistence (`docs/inbound_generation.md` §9 notes this gap); add a single test that covers both at once when this work lands.
