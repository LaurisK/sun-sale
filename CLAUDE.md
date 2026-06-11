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

## Mode override semantics

`select.sunsale_mode_override` is the single source of operator mode intent.
Options are the dispatchable `StorageMode` values plus the sentinel
`sunsale` (rendered as **"sunSale"** in the panel UI via an option-label
map in `sun-sale-panel.js`; entity option strings stay snake_case for HA).

Three rules govern dispatch in
`outbound/inverter_control_module.py:InverterControlModule.tick`:

- **Write once, on change.** `apply_mode` runs only on the cycle the
  resolved target *differs* from `last_commanded_mode` — a button press, or
  a schedule slot boundary. Holding an unchanged, engaged target re-asserts
  nothing: `_dispatch_current_slot` returns the `holding` outcome and issues
  no Modbus write. The mode is set once and then observed; a freshly-failed
  write is recovered by the verify loop (below). A *drift* that appears after
  the verify window has closed is recovered by the slow reconciliation path
  (further below) — not by blind re-asserts every cycle. This is the fix for
  the inverter mode being "constantly set".
- **Override bypasses `automation_enabled`.** When `coordinator.mode_override`
  is set, the dispatcher resolves the override as the target regardless of
  the automation switch. Operator intent always reaches the inverter (once,
  on the cycle it changes).
- **`automation_enabled` only gates the scheduler path.** When the override
  is `None`, the dispatcher only writes the current schedule slot's mode
  while the switch is on; with it off, the module is observer-only.

The legacy sentinel `"auto"` is folded into `"sunsale"` by the restore
handler in `select.py:async_added_to_hass`, so upgrading from a pre-rename
install does not strand persisted state.

`sensor.sunsale_observed_inverter_mode` surfaces per-tick dispatch
diagnostics — `last_dispatch_outcome` (`ok` write issued / `reconcile`
re-write for an unchanged target / `holding` target unchanged and engaged /
`no_target` / `no_spec` / `automation_disabled`), `last_dispatch_target`,
`last_dispatch_tick_at`, and `automation_enabled_at_dispatch` — so a user can
confirm whether a UI mode change actually reached `apply_mode` or fell
through one of the gates.

### Commanded-mode tracking + verify loop (Phase 2)

The control module keeps its own truth of what the inverter was last asked
to do — `last_commanded_mode` / `last_commanded_at` — independent of the
solis_modbus state cache. On any cycle where the resolved target differs
from this stored value:

1. `apply_mode` is invoked with `force=True`, which bypasses the cached-
   readback comparison in `_apply_43110_bits` / `_set_number`. The Solis
   integration's polling cache can lag a successful write by up to one
   slow-poll interval (~30 s), so trusting it on the commanded-change
   cycle would risk silently dropping the write.
2. The first verify is scheduled at +2 s (`_VERIFY_INITIAL_DELAY_S`),
   then poll every 5 s (`_VERIFY_POLL_INTERVAL_S`) up to 30 s of total
   elapsed wall-clock (`_VERIFY_WINDOW_S`). Each poll re-reads **every
   register the commanded mode writes** — the 43110 bitmask plus whichever
   of `charge_a` / `discharge_a` / `export_limit_w` / `rc_setpoint_w` the
   spec carries — and compares each to its target (`_build_register_status`,
   exact match for the bitmask, `_NUMBER_WRITE_EPSILON` slack for the
   numbers, mirroring `apply_mode`'s write tolerance).
3. **All** registers match → `verify_state = "ok"` and the loop stops.
   Any mismatch within the window → keep polling. Mismatch past the window
   (first time) → log warning, force-rewrite, reset the window, resume
   polling. Mismatch past the second window → `verify_state = "mismatch"`,
   log error, stop. Worst-case time to verdict ≈ 60 s.

A new commanded change always supersedes any pending verify (cancel +
reschedule). `sensor.sunsale_observed_inverter_mode` exposes
`last_commanded_mode`, `last_commanded_at`, `verify_state`,
`last_verify_at`, `last_verify_observed_reg`, and `register_status` (the
per-register desired-vs-observed rows) so the operator can see the loop
status at a glance.

### Slow drift reconciliation (Phase 2.5)

The verify loop only lives ~60 s after a commanded change. Once it settles
on `verify_state == "ok"`, nothing in Phases 0–2 corrects a *later* drift —
the inverter being changed at its own screen, or a register slipping — while
the schedule holds the same mode. `_dispatch_current_slot` would keep
returning `holding` and the panel would show a red register grid for hours
without acting. The reconciliation path closes that gap:

- **Drift re-command.** On every `holding` tick where `verify_state == "ok"`,
  `_registers_drifted()` re-checks the commanded mode's registers against the
  inverter. A drift increments a consecutive-cycle counter; at
  `_DRIFT_RECONCILE_CYCLES` (default 2 ticks ≈ 10 min — a debounce against a
  single transient readback glitch) the unchanged target is re-commanded via
  the same force-write + verify-loop path (`outcome = "reconcile"`). The
  counter resets the moment a tick reads clean, or on any write.
- **Conditioned on `ok`.** Drift reconciliation only runs from the steady
  engaged state. It deliberately does **not** fire while `verify_state` is
  `pending` (the verify loop owns that window) or `mismatch` (the verify
  loop's terminal "this write won't take — check the Modbus chain" verdict is
  respected; a stuck inverter is not re-spammed every few cycles).
- **`mismatch` self-heal.** If a `holding` tick finds `verify_state ==
  "mismatch"` but the registers now all match (operator fixed it at the
  inverter, comms restored), `verify_state` flips back to `ok` — the terminal
  mismatch never re-checks on its own, so the badge would otherwise stay red
  forever.
- **Automation re-enable re-asserts.** A False→True `automation_enabled`
  transition arms `_reassert_next`, so the next dispatch re-commands the
  scheduled mode even if it equals `last_commanded_mode` (the inverter may
  have drifted while automation was paused). Without this, toggling automation
  off→on never re-asserted, because `last_commanded_mode` survived the pause.

Both triggers re-enter `_force_write_and_verify` (the shared force-write +
verify-restart block), so a reconciliation is verified exactly like a fresh
command — it can itself converge to `ok` or, if the write genuinely won't
take, settle at `mismatch` and stop.

Because the verify loop runs on `async_call_later` *between* coordinator
ticks, the module pushes an `on_state_change` callback
(`coordinator._on_control_state_change`) after each verify mutation; the
coordinator mirrors the fresh state via `_mirror_control_module_state` and
calls `async_update_listeners`, so the panel's badge and per-register
colours track the verify loop in real time instead of lagging to the next
5-minute cycle.

### Panel readout + force-verify service (Phase 3)

The panel's mode-override row uses a structured readout instead of the
plain `"Observed: <state>"` line: `Commanded: <X> → Observed: <Y> [badge]`,
where the badge renders the verify state — green "Engaged" (ok), amber
"Verifying…" (pending), red "Mismatch" (after retry). The currently-
selected button mirrors the same state via the `.pending` (pulsing amber
outline) and `.mismatch` (red) classes, so engagement is visible at a
glance without reading the badge text. `_renderModeReadoutInner` is the
single source of truth — `_renderScheduleDrawer` and `_syncScheduleDrawer`
both call it.

Below the headline, `_renderRegisterGrid` lists one `desired → observed`
row per participating register (from the `register_status` attribute),
each coloured independently: **green** when the register matches, **amber**
while the verify window is still open (`verify_state` pending), **red**
once the window has closed on a mismatch. This gives the operator the full
register-level picture of why a mode is or isn't engaged, not just the
aggregate badge.

`sun_sale.force_verify_inverter_mode` is a HA service that bypasses the
+30 s scheduled verify and runs one immediately. Useful for confirming
engagement right after a manual mode change without waiting on the next
verify-tick. Implementation: `InverterControlModule.force_verify_now`
cancels any pending verify and re-runs the same `_on_verify_tick` body
the scheduled callback would have.

## Integration check coverage

Every pipeline module that consumes or produces data must have a corresponding deep-check in the `tools/checks/` package. The check must validate:

- All data the module **consumes** — cross-checked against its upstream source (raw HA entity state, `snap.inputs`, or an upstream `snap.pipeline` key).
- All data the module **exposes** — every declared field in the debug API serialization, including aggregate totals (sums, counts) verified against per-slot values.

**Servicing modules** — those that only route or actuate without producing pipeline data (e.g. `event_router`, `InverterController`) — are exempt.

The harness was split out of the former ~5k-line `tools/integration_check.py`
monolith into the `tools/checks/` package — one module per domain
(`forecast.py`, `pricing.py`, `observed.py`, …), each owning its result
dataclass(es), `check_*` function(s), and `<Module>CheckWidget`(s). The shared
infrastructure lives in `client.py` / `snapshot.py` / `registry.py` /
`validators.py`; the TUI shell is `app.py`; reporting is `report.py`; the
`main()` entry point is `cli.py`. `tools/integration_check.py` remains as a thin
shim that re-exports the package's public API (so `python tools/integration_check.py`
and `from tools.integration_check import …` both keep working).

When adding a new pipeline module:
1. Expose its output in `debug_view.py` under `pipeline` (or `outputs` for final deliverables).
2. Add a `check_<module>()` function and result dataclass plus a `<Module>CheckWidget`
   in the matching `tools/checks/<domain>.py` (a new module file, or an existing
   same-domain one), and re-export them from `tools/checks/__init__.py`.
3. Wire the widget into `tools/checks/app.py` — `_DEEP_CATS`, the `IntegrationCheckApp`
   constructor, `compose()`, and the `CSS` block — and call `check_<module>()` in
   the results assembly inside `tools/checks/cli.py:main`.

## Observed-series bake-in

`inbound/observer/engine.py` is the shared engine for `observed_generation`
and `observed_grid`. It is parameterised by a list of `Side` specs (1 for
generation, 2 for grid: import + export). `build_slots_for_window` takes
`samples_by_side: dict[str, Sequence]` — each side averages its own stream
(no sign-split). Generation passes `{generation: pv_power_samples}`; grid
passes `{grid_import: imp_samples, grid_export: exp_samples}`.

Grid power is sourced per-direction by `GridImportPowerObserver` and
`GridExportPowerObserver`. Each accepts two entity IDs:

  - `entity_id` — a non-negative directional sensor mapped via
    `CONF_INVERTER_ENTITY_GRID_IMPORT_POWER` / `..._GRID_EXPORT_POWER`. When
    present, the observer reads it as-is (clamped ≥ 0).
  - `signed_entity_id` — a single signed net-flow sensor (sunSale convention:
    positive = import, negative = export). Used only when the directional
    entity is empty. The observer projects onto its side via a polarity
    constant (`+1` for import, `-1` for export) then clamps ≥ 0. The Solis
    auto-detect path passes the resolved `grid_power` (=`grid_power_net`)
    here so installs without per-direction sensors work out of the box.

The coordinator also synthesises a signed `grid_power_kw` value purely for
dashboard display — nothing downstream uses it.

Today's slots are raw averaged each cycle; yesterday is finalised once per
(date, side) by `try_bake_yesterday` in `inbound/observer/bake_in.py`, which:

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

## Derived-power observers (consumption + losses)

`inbound/observer/derived.py` adds two **synthetic** observed series that
have no single sensor of their own — each cycle composes a
`DerivedPowerSample` from five primary readings and the engine averages
per-slot consumption / losses formulas from that shared stream:

  * **consumption_kw** = `max(0, backup + ac_port_signed + grid_net_signed)`
  * **losses_kw**      = `max(0, solar − battery_signed − ac_port_signed − backup)`

Sign conventions follow the rest of the codebase: `ac_port_signed` is
positive=inverter→grid (raw Solis convention), `grid_net_signed` is
positive=import (sunSale), `battery_signed` is positive=charging
(sunSale). When the user formula is written with opposite signs
(export-positive grid, discharge-positive battery), the implementation
flips signs to match the codebase — the physical balance is the same.

`DerivedPowerSample` is persisted via `STORAGE_KEY_DERIVED_POWER`. The
composer (`build_derived_power_sample`) returns `None` when any of the
five inputs is missing this cycle — partial samples would bias the
per-slot mean asymmetrically. AC port + backup translators are mapped
via two new config keys (`CONF_INVERTER_ENTITY_AC_PORT_POWER`,
`CONF_INVERTER_ENTITY_BACKUP_POWER`); the Solis resolver auto-fills both
from `ac_grid_port_power` and `backup_load_power`.

No bake-in is wired for either side in this phase: today and yesterday
are both raw averaged. The plug points (`baked_history` kwarg on the
series builders, `BakedObservedHistory` declared in the DAG nodes' `consumes`)
are kept so a future phase can wire a household-consumption yesterday-total
source for the consumption side without changing the series-builder surface.
Losses has no authoritative inverter-side counter and will stay raw.

The series are exposed under `pipeline.observed_consumption` and
`pipeline.observed_losses` in `debug_view.py`; `derived_power_history`
appears under `inputs`. Integration checks `check_observed_consumption`
and `check_observed_losses` validate per-slot non-negativity and the
declared totals against slot sums.
