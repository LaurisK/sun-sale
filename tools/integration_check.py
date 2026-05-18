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

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Collapsible, DataTable, Footer, Static

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


def _remaining_eid(entity_id: str) -> str:
    """Derive the today-remaining forecast entity ID by substituting 'today' → 'today_remaining'.

    Args:
        entity_id: Entity ID containing '_today_' or '_today' substring.

    Returns:
        Modified entity ID, or empty string if no substitution pattern is found.
    """
    for pattern in ("_today_", "_today"):
        if pattern in entity_id:
            return entity_id.replace(pattern, pattern.replace("today", "today_remaining"), 1)
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
            r_eid = _remaining_eid(eid)
            if r_eid:
                snap.raw_entities[r_eid] = client.state(r_eid)
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
    entity_remaining: dict[str, float] = field(default_factory=dict)
    module_slots: list[dict] = field(default_factory=list)
    negative_slots: list[str] = field(default_factory=list)
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
    result.module_slots = forecast.get("slots") or []
    result.module_totals = {
        "today":    forecast.get("total_today_kwh", 0.0),
        "tomorrow": forecast.get("total_tomorrow_kwh", 0.0),
        "d2":       forecast.get("total_d2_kwh", 0.0),
        "d3":       forecast.get("total_d3_kwh", 0.0),
        "d4":       forecast.get("total_d4_kwh", 0.0),
        "d5":       forecast.get("total_d5_kwh", 0.0),
        "d6":       forecast.get("total_d6_kwh", 0.0),
    }

    for base_eid in arrays_with_data:
        r_eid = _remaining_eid(base_eid)
        if not r_eid:
            continue
        state = snap.raw_entities.get(r_eid)
        if state is None:
            continue
        try:
            result.entity_remaining[r_eid] = float(state["state"])
        except (KeyError, ValueError, TypeError):
            pass

    for s in result.module_slots:
        if s.get("expected_kwh", 0.0) < -1e-6:
            result.negative_slots.append(s["start"])
            result.overall_ok = False

    TOL = 0.001
    if result.entity_remaining and result.module_today_remaining_kwh is not None:
        expected_rem = sum(result.entity_remaining.values())
        if abs(expected_rem - result.module_today_remaining_kwh) > TOL:
            result.mismatches.append("remaining")
            result.overall_ok = False

    for day_label, _ in day_plan:
        exp = combined_by_day.get(day_label, 0.0)
        act = result.module_totals.get(day_label, 0.0)
        if abs(exp - act) > TOL:
            result.mismatches.append(day_label)
            result.overall_ok = False

    return result


# ---------------------------------------------------------------------------
# Textual TUI — inline mode
# ---------------------------------------------------------------------------


class ForecastSlotsTable(Static):
    """DataTable widget: array_1 | array_2 | total | target | module, today + tomorrow."""

    DEFAULT_CSS = """
    ForecastSlotsTable { height: auto; }
    ForecastSlotsTable DataTable { height: 25; }
    """

    def __init__(
        self,
        entity_slots: list[EntitySlots],
        array_eids: list[str],
        module_slots: list[dict],
    ) -> None:
        """Initialise with forecast check data.

        Args:
            entity_slots: All parsed raw entity slots from check_forecast.
            array_eids: Base (today) entity IDs for up to two solar arrays.
            module_slots: Module GenerationSlot dicts from the pipeline debug endpoint.
        """
        super().__init__()
        self._entity_slots = entity_slots
        self._array_eids = array_eids
        self._module_slots = module_slots

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with slot rows for today and tomorrow."""
        now_utc = datetime.now(timezone.utc)
        today_d = now_utc.date()
        tomorrow_d = today_d + timedelta(days=1)
        in_range = frozenset((today_d, tomorrow_d))

        array_data: list[dict[datetime, float]] = []
        for base_eid in (list(self._array_eids) + ["", ""])[:2]:
            tomorrow_eid = _tomorrow_eid(base_eid) if base_eid else ""
            dmap: dict[datetime, float] = {}
            for es in self._entity_slots:
                if es.entity_id in (base_eid, tomorrow_eid) and es.day_label in ("today", "tomorrow"):
                    for dt, w in es.slots:
                        if dt.date() in in_range:
                            dmap[dt] = w
            array_data.append(dmap)

        all_ent_times: set[datetime] = set()
        for dmap in array_data:
            all_ent_times.update(dmap.keys())
        sorted_ent = sorted(all_ent_times)
        unit = "W"
        if len(sorted_ent) >= 2:
            delta_s = (sorted_ent[1] - sorted_ent[0]).total_seconds()
            unit = "W" if abs(delta_s - 900) < 60 else "kWh"

        module_data: dict[datetime, float] = {}
        for s in self._module_slots:
            try:
                dt = datetime.fromisoformat(s["start"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt_utc = dt.astimezone(timezone.utc)
                if dt_utc.date() in in_range:
                    module_data[dt_utc] = s.get("expected_kwh", 0.0)
            except (KeyError, ValueError):
                continue

        all_times = sorted(
            t for t in (all_ent_times | module_data.keys())
            if t.date() in in_range
        )

        def _short(eid: str) -> str:
            return eid.removeprefix("sensor.") if eid else "—"

        col1 = _short(self._array_eids[0]) if self._array_eids else "array_1"
        col2 = _short(self._array_eids[1]) if len(self._array_eids) > 1 else "array_2"

        table = self.query_one(DataTable)
        table.add_columns(
            "Time",
            f"{col1} ({unit})",
            f"{col2} ({unit})",
            f"total ({unit})",
            "target (/4)",
            "module (kWh)",
        )

        dim = "dim"
        prev_date: date | None = None
        for dt in all_times:
            v1 = array_data[0].get(dt, 0.0)
            v2 = array_data[1].get(dt, 0.0)
            v_mod = module_data.get(dt)
            total = v1 + v2
            target = total / 4

            if dt.date() != prev_date:
                if prev_date is not None:
                    table.add_row(*[Text("─", style=dim)] * 6)
                label = "today" if dt.date() == today_d else "tomorrow"
                table.add_row(Text(label, style="bold"), *[""] * 5)
                prev_date = dt.date()

            table.add_row(
                Text(dt.strftime("%H:%M"), style="cyan"),
                Text(f"{v1:.0f}", style=dim if v1 == 0 else ""),
                Text(f"{v2:.0f}", style=dim if v2 == 0 else ""),
                Text(f"{total:.0f}", style=dim if total == 0 else ""),
                Text(f"{target:.2f}", style=dim if target == 0 else ""),
                Text(f"{v_mod:.3f}", style=dim if v_mod == 0 else "") if v_mod is not None
                else Text("—", style=dim),
            )


class ForecastSummaryTable(Static):
    """DataTable of per-day totals: array_1 | array_2 | total | module | check."""

    DEFAULT_CSS = """
    ForecastSummaryTable { height: auto; }
    ForecastSummaryTable DataTable { height: 12; }
    """

    def __init__(self, fc: ForecastCheckResult) -> None:
        """Initialise with the forecast check result.

        Args:
            fc: Result of check_forecast() containing entity slots and module totals.
        """
        super().__init__()
        self._fc = fc

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per day (today → d6) plus remaining."""
        fc = self._fc

        slot_lookup: dict[tuple[str, str], float] = {
            (es.entity_id, es.day_label): es.total_kwh
            for es in fc.entity_slots
        }

        def _short(eid: str) -> str:
            return eid.removeprefix("sensor.") if eid else "—"

        col1 = _short(fc.array_eids[0]) if fc.array_eids else "array_1"
        col2 = _short(fc.array_eids[1]) if len(fc.array_eids) > 1 else "array_2"

        table = self.query_one(DataTable)
        table.add_columns(
            "day",
            f"{col1} (kWh)",
            f"{col2} (kWh)",
            "total (kWh)",
            "module (kWh)",
            "",
        )

        day_plan: list[tuple[str, int | None]] = (
            [("today", None), ("tomorrow", None)]
            + [(f"d{n}", n) for n in range(2, 7)]
        )
        dim = "dim"

        for day_label, n in day_plan:
            a_kwh: list[float] = []
            for base_eid in (list(fc.array_eids) + ["", ""])[:2]:
                if not base_eid:
                    a_kwh.append(0.0)
                    continue
                if day_label == "today":
                    eid = base_eid
                elif day_label == "tomorrow":
                    eid = _tomorrow_eid(base_eid)
                else:
                    eid = _day_eid(base_eid, n)  # type: ignore[arg-type]
                a_kwh.append(slot_lookup.get((eid, day_label), 0.0))

            total = a_kwh[0] + a_kwh[1]
            mod = fc.module_totals.get(day_label, 0.0)
            is_bad = day_label in fc.mismatches
            check_style = "red" if is_bad else "green"

            table.add_row(
                Text(day_label, style="bold"),
                Text(f"{a_kwh[0]:.4f}", style=dim if a_kwh[0] == 0 else ""),
                Text(f"{a_kwh[1]:.4f}", style=dim if a_kwh[1] == 0 else ""),
                Text(f"{total:.4f}", style=dim if total == 0 else ""),
                Text(f"{mod:.4f}", style=dim if mod == 0 else ""),
                Text("✗" if is_bad else "✓", style=check_style),
            )

            if day_label == "today":
                rem_mod = fc.module_today_remaining_kwh
                rem_arr: list[float | None] = []
                for base_eid in (list(fc.array_eids) + ["", ""])[:2]:
                    r_eid = _remaining_eid(base_eid) if base_eid else ""
                    rem_arr.append(fc.entity_remaining.get(r_eid) if r_eid else None)
                rem_total = (
                    sum(v for v in rem_arr if v is not None)
                    if any(v is not None for v in rem_arr) else None
                )
                is_rem_bad = "remaining" in fc.mismatches
                rem_check_style = "red" if is_rem_bad else "green"

                def _fmt(v: float | None) -> Text:
                    if v is None:
                        return Text("—", style=dim)
                    return Text(f"{v:.4f}", style=dim if v == 0 else "")

                table.add_row(
                    Text("  remaining", style=dim),
                    _fmt(rem_arr[0]),
                    _fmt(rem_arr[1]),
                    _fmt(rem_total),
                    _fmt(rem_mod),
                    Text("✗" if is_rem_bad else "✓", style=rem_check_style)
                    if rem_mod is not None else Text(""),
                )

        if fc.negative_slots:
            table.add_row(
                Text("negative slots", style="red bold"),
                Text(""), Text(""), Text(""),
                Text(str(len(fc.negative_slots)), style="red"),
                Text("✗", style="red"),
            )


class ForecastCheckWidget(Static):
    """Collapsible forecast deep-check with two sub-sections: Slots and Summary."""

    DEFAULT_CSS = "ForecastCheckWidget { height: auto; }"

    def __init__(self, fc: ForecastCheckResult) -> None:
        """Initialise with the pre-computed forecast check result.

        Args:
            fc: Result of check_forecast() for one coordinator.
        """
        super().__init__()
        self._fc = fc

    def compose(self) -> ComposeResult:
        """Render two sub-Collapsibles: Slots (time-level) and Summary (day-level)."""
        fc = self._fc

        if fc.skipped:
            yield Static(f"  ⚠  forecast_check   SKIP   {fc.skip_reason}")
            return

        color = "green" if fc.overall_ok else "red"
        mark = "✓" if fc.overall_ok else "✗"
        status = "PASS" if fc.overall_ok else "FAIL"
        title = f"[{color}]{mark}[/{color}]  forecast_check   [{color}]{status}[/{color}]   {fc.n_arrays} array(s)"

        with Collapsible(title=title, collapsed=True):
            yield Static(f"  Arrays: {', '.join(fc.array_eids)}")

            with Collapsible(title="Slots", collapsed=False):
                yield ForecastSlotsTable(fc.entity_slots, fc.array_eids, fc.module_slots)
                if fc.negative_slots:
                    yield Static(f"  ⚠ {len(fc.negative_slots)} negative slot(s): {fc.negative_slots[:3]}")

            with Collapsible(title="Summary", collapsed=False):
                yield Static(f"  slot_count: {fc.module_slot_count}")
                yield ForecastSummaryTable(fc)


class IntegrationCheckApp(App):
    """Textual TUI running in inline mode for the sunSale integration check."""

    BINDINGS = [("q", "quit", "Quit")]
    CSS = """
    Screen { height: auto; }
    Collapsible { height: auto; }
    ForecastCheckWidget { height: auto; }
    ForecastSlotsTable DataTable { height: 25; }
    ForecastSummaryTable DataTable { height: 12; }
    """

    def __init__(
        self,
        report: list[tuple[Snapshot, list[CheckResult]]],
        forecast_results: dict[str, ForecastCheckResult],
    ) -> None:
        """Initialise with validator report and forecast check results.

        Args:
            report: Per-snapshot check results from run_checks().
            forecast_results: Per-entry forecast deep-check results.
        """
        super().__init__()
        self._report = report
        self._forecast_results = forecast_results
        self.exit_code = 0

    def compose(self) -> ComposeResult:
        """Yield check result lines, forecast Collapsible, separator, and summary."""
        for snap, results in self._report:
            cur_cat: str | None = None
            for res in results:
                if res.category == "forecast":
                    continue
                if res.category != cur_cat:
                    yield Static(f"  [dim][{res.category}][/dim]", markup=True)
                    cur_cat = res.category
                color = "green" if res.ok else "red"
                mark = "✓" if res.ok else "✗"
                yield Static(
                    f"  [{color}]{mark}[/{color}]  {res.name}  [dim]{res.detail[:90]}[/dim]",
                    markup=True,
                )

            fc = self._forecast_results.get(snap.entry_id)
            if fc is not None:
                yield Static("  [dim][forecast][/dim]", markup=True)
                yield ForecastCheckWidget(fc)

            yield Static("─" * 60)

        non_fc_total = sum(1 for _, rs in self._report for r in rs if r.category != "forecast")
        non_fc_passed = sum(
            1 for _, rs in self._report for r in rs if r.category != "forecast" and r.ok
        )
        fc_total = sum(1 for fc in self._forecast_results.values() if not fc.skipped)
        fc_passed = sum(
            1 for fc in self._forecast_results.values() if not fc.skipped and fc.overall_ok
        )
        all_total = non_fc_total + fc_total
        all_passed = non_fc_passed + fc_passed
        color = "green" if all_passed == all_total else "red"
        yield Static(
            f"[{color}]{all_passed}/{all_total} checks passed[/{color}]  [dim](q to quit)[/dim]",
            markup=True,
        )
        yield Footer()

    def on_mount(self) -> None:
        """Set exit code based on check results."""
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
    app.run(inline=True)
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())
