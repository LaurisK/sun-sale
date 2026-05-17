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
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

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


def collect(client: HAClient) -> list[Snapshot]:
    """Build snapshots for every coordinator the integration has registered."""
    snapshots: list[Snapshot] = []
    for entry in client.debug():
        snap = Snapshot(entry_id=entry.get("entry_id", "?"), debug=entry)
        for key in ("nordpool_entity", "solar_forecast_entity", "solar_forecast_entity_2"):
            eid = snap.config.get(key)
            if eid:
                snap.raw_entities[eid] = client.state(eid)
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
    # Normalise source timestamps to UTC ISO for comparison.
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
    for s in slots[:96]:  # cap detail noise
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


@validator("forecast_has_data", "forecast")
def _forecast_has_data(snap: Snapshot) -> tuple[bool, str]:
    forecast = snap.pipeline.get("forecast")
    if forecast is None:
        return False, "pipeline.forecast is null"
    primary_eid = snap.config.get("solar_forecast_entity")
    primary_state = snap.raw_entities.get(primary_eid) if primary_eid else None
    has_source = primary_state is not None and primary_state.get("state") not in (None, "unavailable", "unknown")
    if has_source and forecast.get("slot_count", 0) == 0:
        return False, f"primary forecast entity has state {primary_state.get('state')} but 0 slots produced"
    return True, f"slot_count={forecast.get('slot_count')}, primary={forecast.get('primary')}"


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
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="sunSale integration validation harness")
    p.add_argument("--url", default=os.environ.get("HA_URL", DEFAULT_HA_URL))
    p.add_argument("--token", default=os.environ.get("HA_TOKEN", DEFAULT_HA_TOKEN))
    p.add_argument("--json", action="store_true", help="emit JSON report")
    p.add_argument("--filter", help="only run checks in this category")
    p.add_argument("--dump-snapshot", action="store_true", help="print raw snapshot then exit")
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

    report = run_checks(snapshots, args.filter)
    print(render_json(report) if args.json else render_text(report))
    any_failed = any(not r.ok for _, results in report for r in results)
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
