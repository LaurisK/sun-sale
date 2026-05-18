#!/usr/bin/env python3
"""sunSale integration validation harness.

Fetches a live snapshot from a running Home Assistant instance:
  - integration parameters (config) and per-module deliverables from
    /api/sun_sale/debug
  - raw HA state for every entity the integration consumes from
    /api/states/<entity_id>

Then runs a registry of validators against that snapshot and reports
pass/fail. Designed to be extended by adding more @validator functions.

Usage:
    HA_URL=http://host:port HA_TOKEN=... python tools/integration_check.py
    python tools/integration_check.py --json
    python tools/integration_check.py --filter pricing
    python tools/integration_check.py --values   # dump integration/consumed/exposed values
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from textual.app import App, ComposeResult
from textual.containers import ScrollableContainer
from textual.widgets import Collapsible, Footer, Header, Rule, Static

DEFAULT_HA_URL = "http://85.206.57.75:8124"
DEFAULT_HA_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiI1YWQzNTk2MmJmMGE0Yjg5YmY0ZTM5N2VjOWJkNDlhMiIsImlhdCI6MTc3NzI0NDI2NCwiZXhwIjoyMDkyNjA0MjY0fQ."
    "fKvXg_uBCvNV23MHaDoKiJrHDlD5VtlD1-7B_e7N7VQ"
)
DEBUG_PATH = "/api/sun_sale/debug"
STATE_PATH = "/api/states/{entity_id}"


# ---------------------------------------------------------------------------
# HA REST client
# ---------------------------------------------------------------------------


class HAClient:
    def __init__(self, base_url: str, token: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _get(self, path: str) -> Any:
        req = urllib.request.Request(
            self.base_url + path,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def debug(self) -> list[dict]:
        payload = self._get(DEBUG_PATH)
        if not isinstance(payload, list):
            raise ValueError(f"Expected list from {DEBUG_PATH}, got {type(payload).__name__}")
        return payload

    def state(self, entity_id: str) -> dict | None:
        try:
            return self._get(STATE_PATH.format(entity_id=entity_id))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    """One coordinator's complete observable state, including raw HA sources."""

    entry_id: str
    debug: dict
    raw_entities: dict[str, dict | None] = field(default_factory=dict)

    @property
    def config(self) -> dict:
        return self.debug.get("config", {}) or {}

    @property
    def inputs(self) -> dict:
        return self.debug.get("inputs", {}) or {}

    @property
    def pipeline(self) -> dict:
        return self.debug.get("pipeline", {}) or {}

    @property
    def outputs(self) -> dict:
        return self.debug.get("outputs", {}) or {}


# ---------------------------------------------------------------------------
# Entity ID helpers
# ---------------------------------------------------------------------------


def _tomorrow_eid(entity_id: str) -> str:
    for pattern in ("_today_", "_today"):
        if pattern in entity_id:
            return entity_id.replace(pattern, pattern.replace("today", "tomorrow"), 1)
    return ""


def _day_eid(entity_id: str, n: int) -> str:
    for pattern in ("_today_", "_today"):
        if pattern in entity_id:
            return entity_id.replace(pattern, pattern.replace("today", f"d{n}"), 1)
    return ""


def collect(client: HAClient) -> list[Snapshot]:
    """Build snapshots for every coordinator the integration has registered."""
    snapshots: list[Snapshot] = []
    for entry in client.debug():
        snap = Snapshot(entry_id=entry.get("entry_id", "?"), debug=entry)

        nordpool_eid = snap.config.get("nordpool_entity")
        if nordpool_eid:
            snap.raw_entities[nordpool_eid] = client.state(nordpool_eid)

        for key in ("solar_forecast_entity", "solar_forecast_entity_2"):
            eid = snap.config.get(key)
            if not eid:
                continue
            snap.raw_entities[eid] = client.state(eid)
            t_eid = _tomorrow_eid(eid)
            if t_eid:
                snap.raw_entities[t_eid] = client.state(t_eid)
            for n in range(2, 7):
                d_eid = _day_eid(eid, n)
                if d_eid:
                    snap.raw_entities[d_eid] = client.state(d_eid)

        snapshots.append(snap)
    return snapshots


# ---------------------------------------------------------------------------
# Validator registry
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    category: str
    ok: bool
    detail: str = ""


Validator = Callable[[Snapshot], CheckResult]
_VALIDATORS: list[Validator] = []


def validator(name: str, category: str) -> Callable[[Callable[[Snapshot], tuple[bool, str]]], Validator]:
    def wrap(fn: Callable[[Snapshot], tuple[bool, str]]) -> Validator:
        def run(snap: Snapshot) -> CheckResult:
            try:
                ok, detail = fn(snap)
            except Exception as exc:  # noqa: BLE001
                return CheckResult(name, category, False, f"raised {type(exc).__name__}: {exc}")
            return CheckResult(name, category, ok, detail)

        run.__name__ = fn.__name__
        _VALIDATORS.append(run)
        return run

    return wrap


# ---------------------------------------------------------------------------
# Built-in validators
# ---------------------------------------------------------------------------


@validator("config_entities_resolve", "config")
def _config_entities_resolve(snap: Snapshot) -> tuple[bool, str]:
    missing = [eid for eid, state in snap.raw_entities.items() if state is None]
    if missing:
        return False, f"unresolved: {missing}"
    return True, f"{len(snap.raw_entities)} entities resolved"


@validator("pricing_spot_matches_nordpool", "pricing")
def _pricing_spot_matches_nordpool(snap: Snapshot) -> tuple[bool, str]:
    """Every pricing slot whose start aligns with a Nordpool source entry
    must carry the same spot price."""
    pricing = snap.pipeline.get("pricing")
    if pricing is None:
        return False, "pipeline.pricing is null"
    nordpool_eid = snap.config.get("nordpool_entity")
    nordpool = snap.raw_entities.get(nordpool_eid) if nordpool_eid else None
    if nordpool is None:
        return False, "nordpool entity not fetched"
    attrs = nordpool.get("attributes", {}) or {}
    source_entries = (attrs.get("raw_today") or []) + (attrs.get("raw_tomorrow") or [])
    source_by_start: dict[str, float] = {}
    for entry in source_entries:
        try:
            t = datetime.fromisoformat(entry["start"])
        except (KeyError, ValueError):
            continue
        source_by_start[t.astimezone().isoformat()] = entry.get("value")
    overlap = 0
    mismatches: list[str] = []
    for s in pricing.get("slots") or []:
        try:
            slot_t = datetime.fromisoformat(s["start"]).astimezone().isoformat()
        except ValueError:
            continue
        if slot_t in source_by_start:
            overlap += 1
            src = source_by_start[slot_t]
            if src is not None and abs(s["spot"] - src) > 1e-4:
                mismatches.append(f"{s['start']}: spot {s['spot']} vs source {src}")
                if len(mismatches) > 3:
                    break
    if overlap == 0:
        return False, "no pricing slot starts overlap with Nordpool source"
    if mismatches:
        return False, "; ".join(mismatches)
    return True, f"{overlap}/{len(pricing.get('slots') or [])} pricing slots match Nordpool source spot"


@validator("pricing_covers_source", "pricing")
def _pricing_covers_source(snap: Snapshot) -> tuple[bool, str]:
    """All Nordpool source entries should appear in the pricing slot set."""
    pricing = snap.pipeline.get("pricing") or {}
    nordpool_eid = snap.config.get("nordpool_entity")
    nordpool = snap.raw_entities.get(nordpool_eid) if nordpool_eid else None
    if nordpool is None:
        return False, "nordpool entity not fetched"
    attrs = nordpool.get("attributes", {}) or {}
    source_entries = (attrs.get("raw_today") or []) + (attrs.get("raw_tomorrow") or [])
    if not source_entries:
        return True, "no source entries"
    slot_starts: set[str] = set()
    for s in pricing.get("slots") or []:
        try:
            slot_starts.add(datetime.fromisoformat(s["start"]).astimezone().isoformat())
        except ValueError:
            pass
    missing = []
    for entry in source_entries:
        try:
            t = datetime.fromisoformat(entry["start"]).astimezone().isoformat()
        except (KeyError, ValueError):
            continue
        if t not in slot_starts:
            missing.append(entry["start"])
    if missing:
        return False, f"{len(missing)} source entries missing from pricing (e.g. {missing[:2]})"
    return True, f"all {len(source_entries)} source entries present in pricing"


@validator("tariff_math_consistent", "pricing")
def _tariff_math(snap: Snapshot) -> tuple[bool, str]:
    pricing = snap.pipeline.get("pricing") or {}
    tariff = snap.inputs.get("tariff_config")
    slots = pricing.get("slots") or []
    if not tariff or not slots:
        return False, "missing tariff_config or pricing slots"
    dist = tariff["distribution_fee"]
    markup = tariff["markup"]
    tax = tariff["tax_rate"]
    s_dist = tariff["sell_distribution_fee"]
    s_markup = tariff["sell_markup"]
    s_tax = tariff["sell_tax_rate"]
    tol = 1e-3
    mismatches: list[str] = []
    for s in slots[:96]:
        spot = s["spot"]
        expected_buy = (spot + dist + markup) * (1 + tax)
        expected_sell = (spot - s_dist - s_markup) * (1 - s_tax)
        if abs(s["buy"] - expected_buy) > tol:
            mismatches.append(f"{s['start']}: buy {s['buy']} vs expected {expected_buy:.4f}")
        if abs(s["sell"] - expected_sell) > tol:
            mismatches.append(f"{s['start']}: sell {s['sell']} vs expected {expected_sell:.4f}")
        if len(mismatches) > 3:
            break
    if mismatches:
        return False, "; ".join(mismatches)
    return True, f"{len(slots)} slots match formula"


@validator("pricing_slots_contiguous", "pricing")
def _pricing_contiguous(snap: Snapshot) -> tuple[bool, str]:
    pricing = snap.pipeline.get("pricing") or {}
    slots = pricing.get("slots") or []
    res_s = pricing.get("resolution_s", 0)
    if len(slots) < 2 or res_s <= 0:
        return True, "n/a"
    prev = datetime.fromisoformat(slots[0]["start"])
    for s in slots[1:]:
        cur = datetime.fromisoformat(s["start"])
        delta = (cur - prev).total_seconds()
        if abs(delta - res_s) > 1:
            return False, f"gap at {s['start']}: {delta:.0f}s (expected {res_s}s)"
        prev = cur
    return True, f"{len(slots)} slots contiguous at {res_s}s"


@validator("calculation_within_pricing_window", "calculation")
def _calc_within_pricing(snap: Snapshot) -> tuple[bool, str]:
    pricing = snap.pipeline.get("pricing") or {}
    calc = snap.pipeline.get("calculation") or {}
    p_slots = pricing.get("slots") or []
    c_slots = calc.get("slots") or []
    if not p_slots or not c_slots:
        return True, "n/a"
    p_start = datetime.fromisoformat(p_slots[0]["start"])
    p_end = datetime.fromisoformat(p_slots[-1]["start"])
    out_of_window = []
    for s in c_slots:
        t = datetime.fromisoformat(s["start"])
        if t < p_start or t > p_end:
            out_of_window.append(s["start"])
    if out_of_window:
        return False, f"{len(out_of_window)} calculation slots outside pricing window"
    return True, f"{len(c_slots)} calculation slots within pricing window"


@validator("schedule_chronological", "schedule")
def _schedule_chronological(snap: Snapshot) -> tuple[bool, str]:
    schedule = snap.outputs.get("schedule")
    if schedule is None:
        return True, "no schedule"
    slots = schedule.get("slots") or []
    if len(slots) < 2:
        return True, f"{len(slots)} slot(s)"
    last = datetime.fromisoformat(slots[0]["start"])
    for s in slots[1:]:
        cur = datetime.fromisoformat(s["start"])
        if cur < last:
            return False, f"slot at {s['start']} precedes prior {last.isoformat()}"
        last = cur
    return True, f"{len(slots)} slots in order"


@validator("schedule_aligned_with_pricing", "schedule")
def _schedule_aligned(snap: Snapshot) -> tuple[bool, str]:
    schedule = snap.outputs.get("schedule")
    pricing = snap.pipeline.get("pricing") or {}
    if schedule is None:
        return True, "no schedule"
    p_starts = {s["start"] for s in pricing.get("slots") or []}
    if not p_starts:
        return True, "no pricing slots to compare"
    misaligned = [s["start"] for s in schedule.get("slots", []) if s["start"] not in p_starts]
    if misaligned:
        return False, f"{len(misaligned)} schedule slot(s) not aligned to a pricing slot start"
    return True, "all schedule slots aligned"


@validator("battery_soc_sane", "battery")
def _battery_soc(snap: Snapshot) -> tuple[bool, str]:
    battery = snap.inputs.get("battery")
    if battery is None:
        return True, "no battery state"
    soc = battery.get("soc")
    if soc is None:
        return False, "battery.soc is null"
    if not (0 <= soc <= 100):
        return False, f"soc={soc} outside [0, 100]"
    return True, f"soc={soc}"


@validator("degradation_cost_present", "battery")
def _degradation_cost(snap: Snapshot) -> tuple[bool, str]:
    deg = snap.pipeline.get("degradation_cost_per_kwh")
    battery = snap.inputs.get("battery") or {}
    if battery.get("estimated_capacity_kwh"):
        if deg is None or deg <= 0:
            return False, f"degradation_cost_per_kwh={deg} but estimated_capacity exists"
        return True, f"{deg:.5f} EUR/kWh"
    return True, "no capacity yet"


# ---------------------------------------------------------------------------
# Forecast check (TUI-specific deep validation)
# ---------------------------------------------------------------------------


@dataclass
class EntitySlots:
    entity_id: str
    day_label: str
    resolution_min: int
    slots: list[tuple[datetime, float]]
    total_kwh: float


@dataclass
class ForecastCheckResult:
    skipped: bool = False
    skip_reason: str = ""
    n_arrays: int = 0
    array_eids: list[str] = field(default_factory=list)
    entity_slots: list[EntitySlots] = field(default_factory=list)
    expected_totals: dict[str, float] = field(default_factory=dict)
    module_totals: dict[str, float] = field(default_factory=dict)
    module_slot_count: int = 0
    module_today_remaining_kwh: float | None = None
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def _parse_watts(watts: dict) -> tuple[list[tuple[datetime, float]], int]:
    """Parse {iso_str: watts} dict into sorted (datetime, watts) list + resolution_min."""
    parsed: list[tuple[datetime, float]] = []
    for ts_str, w in watts.items():
        try:
            dt = datetime.fromisoformat(str(ts_str))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            parsed.append((dt.astimezone(timezone.utc), float(w)))
        except (ValueError, TypeError):
            continue
    parsed.sort(key=lambda x: x[0])
    if len(parsed) >= 2:
        delta = (parsed[1][0] - parsed[0][0]).total_seconds()
        res = 15 if abs(delta - 900) < 60 else 60
    else:
        res = 60
    return parsed, res


def check_forecast(snap: Snapshot) -> ForecastCheckResult:
    """Deep forecast validation: raw entity data → expected totals → vs module output."""
    result = ForecastCheckResult()

    entity_1 = snap.config.get("solar_forecast_entity", "")
    entity_2 = snap.config.get("solar_forecast_entity_2", "")
    base_eids = [eid for eid in [entity_1, entity_2] if eid]

    if not base_eids:
        result.skipped = True
        result.skip_reason = "No solar forecast entities configured"
        return result

    arrays_with_data = [
        eid for eid in base_eids
        if isinstance(((snap.raw_entities.get(eid) or {}).get("attributes") or {}).get("watts"), dict)
    ]
    if not arrays_with_data:
        result.skipped = True
        result.skip_reason = "Open-Meteo Solar Forecast not present (no 'watts' attribute on entity)"
        return result

    result.n_arrays = len(arrays_with_data)
    result.array_eids = list(arrays_with_data)

    now = datetime.now(timezone.utc)
    today = now.date()

    day_plan: list[tuple[str, date]] = [
        ("today", today),
        ("tomorrow", today + timedelta(days=1)),
    ] + [(f"d{n}", today + timedelta(days=n)) for n in range(2, 7)]

    combined_by_day: dict[str, float] = {label: 0.0 for label, _ in day_plan}

    for base_eid in arrays_with_data:
        eid_day_pairs: list[tuple[str, str]] = [
            (base_eid, "today"),
            (_tomorrow_eid(base_eid), "tomorrow"),
        ] + [(_day_eid(base_eid, n), f"d{n}") for n in range(2, 7)]

        for eid, day_label in eid_day_pairs:
            if not eid:
                continue
            state = snap.raw_entities.get(eid)
            if not state:
                continue
            watts = (state.get("attributes") or {}).get("watts")
            if not isinstance(watts, dict):
                continue

            slots, res = _parse_watts(watts)
            slot_h = res / 60.0
            total = round(sum(w * slot_h / 1000.0 for _, w in slots), 4)
            combined_by_day[day_label] = round(combined_by_day[day_label] + total, 4)

            result.entity_slots.append(EntitySlots(
                entity_id=eid,
                day_label=day_label,
                resolution_min=res,
                slots=slots,
                total_kwh=total,
            ))

    result.expected_totals = dict(combined_by_day)

    forecast = snap.pipeline.get("forecast") or {}
    result.module_slot_count = forecast.get("slot_count", 0)
    result.module_today_remaining_kwh = forecast.get("today_remaining_kwh")
    result.module_totals = {
        "today":    forecast.get("total_today_kwh", 0.0),
        "tomorrow": forecast.get("total_tomorrow_kwh", 0.0),
        "d2":       forecast.get("total_d2_kwh", 0.0),
        "d3":       forecast.get("total_d3_kwh", 0.0),
        "d4":       forecast.get("total_d4_kwh", 0.0),
        "d5":       forecast.get("total_d5_kwh", 0.0),
        "d6":       forecast.get("total_d6_kwh", 0.0),
    }

    TOL = 0.001
    for day_label, _ in day_plan:
        exp = combined_by_day.get(day_label, 0.0)
        act = result.module_totals.get(day_label, 0.0)
        if abs(exp - act) > TOL:
            result.mismatches.append(day_label)
            result.overall_ok = False

    return result


# ---------------------------------------------------------------------------
# Textual TUI — forecast widget
# ---------------------------------------------------------------------------


class ForecastCheck(Static):
    """Expandable Collapsible widget showing the full forecast deep-check."""

    DEFAULT_CSS = "ForecastCheck { height: auto; }"

    def __init__(self, fc: ForecastCheckResult) -> None:
        super().__init__()
        self._fc = fc

    def compose(self) -> ComposeResult:
        fc = self._fc

        if fc.skipped:
            title = f"⚠  [forecast] forecast_check   SKIP   {fc.skip_reason}"
            with Collapsible(title=title):
                yield Static("No Open-Meteo Solar Forecast data found on configured entities.")
            return

        mark = "✓" if fc.overall_ok else "✗"
        status = "PASS" if fc.overall_ok else "FAIL"
        title = f"{mark}  [forecast] forecast_check   {status}   Tested {fc.n_arrays} array(s)"

        with Collapsible(title=title):
            yield Static("  ─── Arrays ───")
            for eid in fc.array_eids:
                yield Static(f"    • {eid}")
            yield Static("")

            yield Static("  ─── Raw entity data ───")

            for es in fc.entity_slots:
                unit = "W" if es.resolution_min == 15 else "Wh"
                label = f"{es.resolution_min}min"
                yield Static(
                    f"    {es.entity_id}  [{es.day_label} | {label}]   total: {es.total_kwh:.4f} kWh"
                )
                if es.day_label in ("today", "tomorrow") and es.slots:
                    non_zero = [(dt, w) for dt, w in es.slots if w > 0]
                    if non_zero:
                        parts = [f"{dt.strftime('%H:%M')} {w:.0f}{unit}" for dt, w in non_zero[:18]]
                        if len(non_zero) > 18:
                            parts.append(f"…+{len(non_zero) - 18}")
                        yield Static(f"      {' │ '.join(parts)}")

            yield Static("")
            yield Static("  Combined expected totals from raw entities:")
            for day_label, _ in [("today", None), ("tomorrow", None)] + [(f"d{n}", None) for n in range(2, 7)]:
                exp = fc.expected_totals.get(day_label, 0.0)
                yield Static(f"    {day_label:10s}  {exp:.4f} kWh")

            if fc.module_today_remaining_kwh is not None:
                yield Static(f"  Module today_remaining_kwh: {fc.module_today_remaining_kwh:.4f} kWh")

            yield Static("")

            yield Static("  ─── Module output (GenerationSeries) ───")
            yield Static(f"    slot_count: {fc.module_slot_count}")
            yield Static("")
            yield Static("    day        expected      module        check")
            yield Static("    " + "─" * 54)

            for day_label, _ in [("today", None), ("tomorrow", None)] + [(f"d{n}", None) for n in range(2, 7)]:
                exp = fc.expected_totals.get(day_label, 0.0)
                act = fc.module_totals.get(day_label, 0.0)
                is_mismatch = day_label in fc.mismatches
                if is_mismatch:
                    check_mark = "✗ MISMATCH"
                    line = f"    [red]{day_label:10s}  {exp:9.4f} kWh  {act:9.4f} kWh  {check_mark}[/red]"
                else:
                    check_mark = "✓"
                    line = f"    [green]{day_label:10s}  {exp:9.4f} kWh  {act:9.4f} kWh  {check_mark}[/green]"
                yield Static(line, markup=True)


# ---------------------------------------------------------------------------
# Textual TUI — main app
# ---------------------------------------------------------------------------


class IntegrationCheckApp(App):
    TITLE = "sunSale Integration Check"
    BINDINGS = [("q", "quit", "Quit")]
    CSS = """
    Screen { background: $background; }
    ScrollableContainer { padding: 1 2; }
    """

    def __init__(
        self,
        report: list[tuple[Snapshot, list[CheckResult]]],
        forecast_results: dict[str, ForecastCheckResult],
    ) -> None:
        super().__init__()
        self._report = report
        self._forecast_results = forecast_results
        self.exit_code = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with ScrollableContainer():
            for snap, results in self._report:
                fc = self._forecast_results.get(snap.entry_id)

                cur_cat: str | None = None
                for res in results:
                    if res.category == "forecast":
                        continue
                    if res.category != cur_cat:
                        yield Static(f"  [dim][{res.category}][/dim]", markup=True)
                        cur_cat = res.category
                    icon = "[green]✓[/green]" if res.ok else "[red]✗[/red]"
                    detail = res.detail[:90]
                    yield Static(f"  {icon}  {res.name}  [dim]{detail}[/dim]", markup=True)

                if fc is not None:
                    yield Static("  [dim][forecast][/dim]", markup=True)
                    yield ForecastCheck(fc)

                yield Rule()

            non_fc_total = sum(
                1 for _, rs in self._report for r in rs if r.category != "forecast"
            )
            non_fc_passed = sum(
                1 for _, rs in self._report for r in rs
                if r.category != "forecast" and r.ok
            )
            fc_total = sum(1 for fc in self._forecast_results.values() if not fc.skipped)
            fc_passed = sum(
                1 for fc in self._forecast_results.values()
                if not fc.skipped and fc.overall_ok
            )
            all_total = non_fc_total + fc_total
            all_passed = non_fc_passed + fc_passed
            color = "green" if all_passed == all_total else "red"
            yield Static(
                f"  [{color}]{all_passed}/{all_total} checks passed[/{color}]",
                markup=True,
            )
        yield Footer()

    def on_mount(self) -> None:
        any_failed = any(
            not r.ok for _, rs in self._report for r in rs if r.category != "forecast"
        )
        any_fc_failed = any(
            not fc.overall_ok and not fc.skipped for fc in self._forecast_results.values()
        )
        self.exit_code = 1 if (any_failed or any_fc_failed) else 0


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def run_checks(snapshots: list[Snapshot], category_filter: str | None) -> list[tuple[Snapshot, list[CheckResult]]]:
    out: list[tuple[Snapshot, list[CheckResult]]] = []
    for snap in snapshots:
        results = []
        for v in _VALIDATORS:
            res = v(snap)
            if category_filter and res.category != category_filter:
                continue
            results.append(res)
        out.append((snap, results))
    return out


def render_text(report: list[tuple[Snapshot, list[CheckResult]]]) -> str:
    lines: list[str] = []
    total = passed = 0
    for snap, results in report:
        lines.append(f"== entry {snap.entry_id} ==")
        cur_cat = None
        for r in results:
            if r.category != cur_cat:
                lines.append(f"  [{r.category}]")
                cur_cat = r.category
            mark = "PASS" if r.ok else "FAIL"
            lines.append(f"    {mark}  {r.name}: {r.detail}")
            total += 1
            if r.ok:
                passed += 1
        lines.append("")
    lines.append(f"{passed}/{total} checks passed")
    return "\n".join(lines)


def render_json(report: list[tuple[Snapshot, list[CheckResult]]]) -> str:
    out = []
    for snap, results in report:
        out.append({
            "entry_id": snap.entry_id,
            "checks": [
                {"name": r.name, "category": r.category, "ok": r.ok, "detail": r.detail}
                for r in results
            ],
        })
    return json.dumps(out, indent=2)


# ---------------------------------------------------------------------------
# Values dump
# ---------------------------------------------------------------------------

_INTEGRATION_INPUT_KEYS = frozenset({"tariff_config"})


def _section(title: str, payload: Any) -> str:
    body = json.dumps(payload, indent=2, sort_keys=True, default=str)
    return f"[{title}]\n{body}"


def render_values(snapshots: list[Snapshot]) -> str:
    parts: list[str] = []
    for snap in snapshots:
        debug = snap.debug
        inputs = snap.inputs
        consumed_primary = {
            k: v for k, v in inputs.items() if k not in _INTEGRATION_INPUT_KEYS
        }
        integration = {
            "entry_id": snap.entry_id,
            "automation_enabled": debug.get("automation_enabled"),
            "config": snap.config,
            **{k: inputs.get(k) for k in _INTEGRATION_INPUT_KEYS if k in inputs},
        }
        consumed = {
            "raw_entities": snap.raw_entities,
            "primary": consumed_primary,
        }
        exposed = {
            "pipeline": snap.pipeline,
            "outputs": snap.outputs,
            "last_dispatched_action": debug.get("last_dispatched_action"),
            "last_dispatched_at": debug.get("last_dispatched_at"),
            "timestamp": debug.get("timestamp"),
        }
        parts.append(f"== entry {snap.entry_id} ==\n")
        parts.append(_section("INTEGRATION", integration))
        parts.append("")
        parts.append(_section("CONSUMED", consumed))
        parts.append("")
        parts.append(_section("EXPOSED", exposed))
        parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="sunSale integration validation harness")
    p.add_argument("--url", default=os.environ.get("HA_URL", DEFAULT_HA_URL))
    p.add_argument("--token", default=os.environ.get("HA_TOKEN", DEFAULT_HA_TOKEN))
    p.add_argument("--json", action="store_true", help="emit JSON report (no TUI)")
    p.add_argument("--filter", help="only run checks in this category")
    p.add_argument("--dump-snapshot", action="store_true", help="print raw snapshot then exit")
    p.add_argument(
        "--values",
        action="store_true",
        help="print all integration/consumed/exposed values then exit",
    )
    args = p.parse_args(argv)

    client = HAClient(args.url, args.token)
    try:
        snapshots = collect(client)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"ERROR: could not reach HA at {args.url}: {e}", file=sys.stderr)
        return 2

    if not snapshots:
        print("ERROR: HA returned 0 coordinator entries — integration not loaded?", file=sys.stderr)
        return 2

    if args.dump_snapshot:
        print(json.dumps(
            [{"entry_id": s.entry_id, "debug": s.debug, "raw_entities": s.raw_entities} for s in snapshots],
            indent=2,
        ))
        return 0

    if args.values:
        print(render_values(snapshots))
        return 0

    report = run_checks(snapshots, args.filter)

    if args.json:
        print(render_json(report))
        any_failed = any(not r.ok for _, results in report for r in results)
        return 1 if any_failed else 0

    forecast_results = {snap.entry_id: check_forecast(snap) for snap in snapshots}
    app = IntegrationCheckApp(report, forecast_results)
    app.run()
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())
