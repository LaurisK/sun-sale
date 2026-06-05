# sunSale – development guidelines

## Code commenting standard

**All functions must have a docstring** — including private, static, and helper functions.

Use **Google-style Python docstrings**:

```python
def example(arg1: int, arg2: str) -> bool:
    """Brief one-line description.

    Args:
        arg1: What this argument represents.
        arg2: What this argument represents.

    Returns:
        What the return value means.

    Raises:
        ValueError: When and why this is raised (omit section if no exceptions).
    """
```

Rules:
- The opening line is a brief imperative statement (`Return …`, `Build …`, `Compute …`).
- Include `Args:`, `Returns:`, and `Raises:` sections for any function where they add value. Omit empty sections.
- For trivially obvious one-purpose functions (e.g. simple property getters), a single-line docstring is acceptable when the return type annotation makes the intent self-evident.
- For DAG `_compute()` overrides, a one-liner is sufficient when the parent class docstring already states the node's purpose.
- **Section comments** (e.g. `# --- Section name ---`) are acceptable to mark significant logical groups within a module. Keep them sparse.
- **Inline comments** should explain *why*, never *what*. Add one only when the logic is non-obvious, counter-intuitive, or works around a specific constraint.
- Do not write comments that merely restate what the code already says.

## solis_modbus integration auto-detection

When the user selects the Solis inverter platform during setup, sunSale queries
`hass.config_entries.async_entries("solis_modbus")` to auto-discover the inverter:

- **Exactly one entry** — auto-selected silently; the `inverter_solis` config-flow step is
  skipped entirely and the config entry ID is stored as `solis_config_entry_id`.
- **Multiple entries** — a compact picker is shown so the user selects which inverter.
- **No entries** — the full 12-field manual entity mapping form is shown as a fallback.

At coordinator startup, when `solis_config_entry_id` is present in the config entry,
`inbound/solis_entity_resolver.py` resolves all required entity IDs by scanning the
HA entity registry for entities belonging to that config entry. Matching is done by
`unique_id` suffix (register-based for time/switches, named suffix for sensors/numbers).

Existing configs that store individual entity IDs directly (no `solis_config_entry_id`)
continue to work unchanged via the legacy path in `coordinator.py`.

## Integration check coverage

Every pipeline module that consumes or produces data must have a corresponding deep-check in `tools/integration_check.py`. The check must validate:

- All data the module **consumes** — cross-checked against its upstream source (raw HA entity state, `snap.inputs`, or an upstream `snap.pipeline` key).
- All data the module **exposes** — every declared field in the debug API serialization, including aggregate totals (sums, counts) verified against per-slot values.

**Servicing modules** — those that only route or actuate without producing pipeline data (e.g. `event_router`, `InverterController`) — are exempt.

When adding a new pipeline module:
1. Expose its output in `debug_view.py` under `pipeline` (or `outputs` for final deliverables).
2. Add a `check_<module>()` function and result dataclass.
3. Add a `<Module>CheckWidget` and wire it into `_DEEP_CATS`, `IntegrationCheckApp`, and `compose()`.

## Observed-series bake-in

`inbound/observed_engine.py` is the shared engine for `observed_generation`
and `observed_grid`. It is parameterised by a list of `Side` specs (1 for
generation, 2 for grid: import + export). `build_slots_for_window` takes
`samples_by_side: dict[str, Sequence]` — each side averages its own stream
(no sign-split). Generation passes `{generation: pv_power_samples}`; grid
passes `{grid_import: imp_samples, grid_export: exp_samples}`.

Grid power is **always** sourced from two separate non-negative HA entities
mapped via `CONF_INVERTER_ENTITY_GRID_IMPORT_POWER` and
`CONF_INVERTER_ENTITY_GRID_EXPORT_POWER`. The legacy signed
`GridObserver` has been removed; if a deployment only has a signed net
sensor, the user must create per-direction template helpers
(`max(0, ...)` and `max(0, -...)`). The coordinator synthesises a signed
`grid_power_kw` value purely for dashboard display from the same pair —
nothing downstream uses it.

Today's slots are raw averaged each cycle; yesterday is finalised once per
(date, side) by `try_bake_yesterday` in `inbound/observed_bake_in.py`, which:

1. Resolves an authoritative yesterday-total via
   `inbound/yesterday_total_resolver.py`. Preferred source is a dedicated HA
   entity (config keys `inverter_entity_{generation,grid_import,grid_export}_yesterday`);
   fallback is a `CounterSnapshotRecord` captured pre-rollover by
   `inbound/pre_rollover_snapshot.py`.
2. Applies proportional scaling (`slot × counter_total/slot_sum`); zero stays
   zero. The bake is skipped (slots stay raw) when the factor lies outside
   `[BAKE_IN_FACTOR_MIN, BAKE_IN_FACTOR_MAX]` or the slot sum is zero.
3. Persists a `BakedDayRecord` per side per local date carrying
   `counter_total_used`, `source_kind`, `baked_slots`, `baked_sum`.

Hard cutoff `BAKE_IN_HARD_CUTOFF_LOCAL` (default 06:00 local): if no source
materialises by then, the record is written with `source_kind="failed_no_source"`
and the slots stay raw. Records are frozen after their first successful write.

`tools/integration_check.py:check_baked_observed` flags records where
`|counter_total_used - baked_sum| > max(abs_tol, rel_tol × counter_total_used)`
per side (`_BAKE_CHECK_TOLERANCES`).

`inbound/inverter_time.py` tracks the HA↔inverter clock skew via a rolling
median over `InverterTimeReading` samples. When the configured
`CONF_INVERTER_ENTITY_INVERTER_CLOCK` entity is set and at least
`INVERTER_TIME_MIN_SAMPLES` readings have been collected, the coordinator
passes the median skew as `clock_skew_seconds` to `maybe_capture_snapshots`,
which shifts the rollover window so the snapshot fires relative to the
inverter's idea of midnight (not HA's). With no entity mapped — or while the
buffer is still warming up — the snapshot falls back to HA-local timing.

User-visible sensors for the observed series live in `sensor.py`:

- `Today*LiveSensor` (generation / imported / exported) — read directly
  from the inverter's daily-resetting counter via
  `coordinator.data["today_*_live_kwh"]`. `TOTAL_INCREASING` energy class
  so they integrate with HA's energy dashboard.
- `Today*SlotSumSensor` (diagnostic, disabled by default) — read
  `ObservedGenerationSeries.total_today_so_far_kwh` /
  `ObservedGridSeries.total_today_*_kwh`. May drift from the live counter
  mid-day; expected to converge after the next-day bake-in.
- `Yesterday*BakedSensor` — read `BakedObservedHistory` for yesterday's
  local date; expose `source_kind`, `counter_total_used`, `baked_at` as
  attributes. `TOTAL` energy class (finalised, never re-updates for that
  date).

These sensors are pure pass-throughs of pipeline state already validated by
`check_observed_generation`, `check_observed_grid`, and
`check_baked_observed`, so they do **not** get their own deep-check
widgets.
