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
    if not (0.0 <= soc <= 1.0):
        return False, f"soc={soc:.4f} outside [0.0, 1.0]"
    return True, f"soc={soc:.1%}"


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
    module_yesterday_kwh: float = 0.0
    yesterday_store_date: str = ""
    yesterday_store_slots: list[tuple[datetime, float]] = field(default_factory=list)  # (utc_dt, kwh)
    yesterday_store_total_kwh: float = 0.0
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
    result.module_yesterday_kwh = forecast.get("total_yesterday_kwh", 0.0)
    result.module_today_remaining_kwh = forecast.get("today_remaining_kwh")

    yday_store = snap.inputs.get("yesterday_solar") or {}
    result.yesterday_store_date = yday_store.get("date") or ""
    yday_total = 0.0
    for entry in yday_store.get("entries") or []:
        try:
            dt = datetime.fromisoformat(entry["start"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            kwh = float(entry["kwh"])
            result.yesterday_store_slots.append((dt.astimezone(timezone.utc), kwh))
            yday_total += kwh
        except (KeyError, ValueError, TypeError):
            continue
    result.yesterday_store_total_kwh = round(yday_total, 4)
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
# Pricing deep check
# ---------------------------------------------------------------------------


@dataclass
class PricingCheckResult:
    """Result of the pricing deep-check: tariff formula vs module output."""

    skipped: bool = False
    skip_reason: str = ""
    module_slot_count: int = 0
    resolution_s: int = 0
    computed_at: str = ""
    negative_sell_count: int = 0
    tariff_config: dict = field(default_factory=dict)
    slot_rows: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_pricing(snap: Snapshot) -> PricingCheckResult:
    """Verify every pricing slot against the tariff formula.

    Args:
        snap: Coordinator snapshot containing pipeline.pricing and inputs.tariff_config.

    Returns:
        PricingCheckResult with per-slot formula comparisons and overall pass/fail.
    """
    result = PricingCheckResult()

    pricing = snap.pipeline.get("pricing")
    if not pricing:
        result.skipped = True
        result.skip_reason = "pipeline.pricing is null"
        return result

    tariff = snap.inputs.get("tariff_config")
    if not tariff:
        result.skipped = True
        result.skip_reason = "inputs.tariff_config is null"
        return result

    result.module_slot_count = pricing.get("slot_count", 0)
    result.resolution_s = pricing.get("resolution_s", 0)
    result.computed_at = pricing.get("computed_at", "")
    result.negative_sell_count = pricing.get("negative_sell_count", 0)
    result.tariff_config = dict(tariff)

    dist = tariff.get("distribution_fee", 0.0)
    markup = tariff.get("markup", 0.0)
    tax = tariff.get("tax_rate", 0.0)
    s_dist = tariff.get("sell_distribution_fee", 0.0)
    s_markup = tariff.get("sell_markup", 0.0)
    s_tax = tariff.get("sell_tax_rate", 0.0)
    TOL = 1e-3

    for s in pricing.get("slots") or []:
        spot = s.get("spot", 0.0)
        act_buy = s.get("buy", 0.0)
        act_sell = s.get("sell", 0.0)
        exp_buy = (spot + dist + markup) * (1 + tax)
        exp_sell = (spot - s_dist - s_markup) * (1 - s_tax)
        ok = abs(act_buy - exp_buy) <= TOL and abs(act_sell - exp_sell) <= TOL
        if not ok:
            result.mismatches.append(s.get("start", ""))
            result.overall_ok = False
        result.slot_rows.append({
            "start": s.get("start", ""),
            "spot": spot,
            "exp_buy": exp_buy,
            "act_buy": act_buy,
            "exp_sell": exp_sell,
            "act_sell": act_sell,
            "ok": ok,
        })

    return result


# ---------------------------------------------------------------------------
# Calculation deep check
# ---------------------------------------------------------------------------


@dataclass
class CalculationCheckResult:
    """Result of the calculation deep-check: sell_allowed logic vs pricing sell prices."""

    skipped: bool = False
    skip_reason: str = ""
    module_slot_count: int = 0
    total_negative_sale_kwh: float = 0.0
    computed_neg_sale_kwh: float = 0.0
    computed_at: str = ""
    lockout_windows: list[dict] = field(default_factory=list)
    slot_rows: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_calculation(snap: Snapshot) -> CalculationCheckResult:
    """Verify calculation sell_allowed flags match the pricing sell-price sign.

    Args:
        snap: Coordinator snapshot containing pipeline.calculation and pipeline.pricing.

    Returns:
        CalculationCheckResult with per-slot sell_allowed comparisons and overall pass/fail.
    """
    result = CalculationCheckResult()

    calc = snap.pipeline.get("calculation")
    if not calc:
        result.skipped = True
        result.skip_reason = "pipeline.calculation is null"
        return result

    pricing = snap.pipeline.get("pricing")
    if not pricing:
        result.skipped = True
        result.skip_reason = "pipeline.pricing is null (needed for expected sell_allowed)"
        return result

    result.module_slot_count = calc.get("slot_count", 0)
    result.total_negative_sale_kwh = calc.get("total_negative_sale_kwh", 0.0)
    result.computed_at = calc.get("computed_at", "")
    result.lockout_windows = list(calc.get("feed_in_lockout_windows") or [])

    price_by_start: dict[str, float] = {
        s["start"]: s.get("sell", 0.0)
        for s in (pricing.get("slots") or [])
    }

    comp_neg_sale = 0.0
    for s in calc.get("slots") or []:
        start = s.get("start", "")
        sell_act = s.get("sell_allowed", False)
        sell_price = price_by_start.get(start)
        sell_exp = sell_price is not None and sell_price > 0.0
        solar_kwh = s.get("expected_solar_kwh", 0.0)
        neg_sale_kwh = s.get("expected_solar_negative_sale_kwh", 0.0)
        comp_neg_sale += neg_sale_kwh
        notes = list(s.get("notes") or [])
        ok = sell_price is None or sell_act == sell_exp
        if not ok:
            result.mismatches.append(start)
            result.overall_ok = False
        result.slot_rows.append({
            "start": start,
            "sell_price": sell_price,
            "sell_exp": sell_exp,
            "sell_act": sell_act,
            "solar_kwh": solar_kwh,
            "neg_sale_kwh": neg_sale_kwh,
            "notes": notes,
            "ok": ok,
        })

    result.computed_neg_sale_kwh = round(comp_neg_sale, 4)
    if result.total_negative_sale_kwh > 0 and abs(comp_neg_sale - result.total_negative_sale_kwh) > 0.01:
        result.mismatches.append("neg_sale_sum_mismatch")
        result.overall_ok = False

    return result


# ---------------------------------------------------------------------------
# Schedule deep check
# ---------------------------------------------------------------------------


@dataclass
class ScheduleCheckResult:
    """Result of the schedule deep-check: slot ordering and profit consistency."""

    skipped: bool = False
    skip_reason: str = ""
    slot_count: int = 0
    total_expected_profit_eur: float = 0.0
    computed_profit_sum: float = 0.0
    slot_rows: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_schedule(snap: Snapshot) -> ScheduleCheckResult:
    """Validate schedule slot ordering and summed profit matches the declared total.

    Args:
        snap: Coordinator snapshot containing outputs.schedule.

    Returns:
        ScheduleCheckResult with per-slot data and overall pass/fail.
    """
    result = ScheduleCheckResult()

    schedule = snap.outputs.get("schedule")
    if not schedule:
        result.skipped = True
        result.skip_reason = "outputs.schedule is null"
        return result

    slots = schedule.get("slots") or []
    result.slot_count = len(slots)
    result.total_expected_profit_eur = schedule.get("total_expected_profit_eur") or 0.0

    now = datetime.now(timezone.utc)
    last_dt: datetime | None = None
    computed_profit = 0.0

    for s in slots:
        start_str = s.get("start", "")
        end_str = s.get("end", start_str)

        try:
            start_dt: datetime | None = datetime.fromisoformat(start_str).astimezone(timezone.utc)
        except (ValueError, AttributeError):
            start_dt = None

        try:
            end_dt: datetime | None = datetime.fromisoformat(end_str).astimezone(timezone.utc)
        except (ValueError, AttributeError):
            end_dt = None

        is_current = (
            start_dt is not None and end_dt is not None and start_dt <= now < end_dt
        )
        ok = last_dt is None or start_dt is None or start_dt >= last_dt
        if not ok:
            result.mismatches.append(start_str)
            result.overall_ok = False

        profit = s.get("expected_profit_eur") or 0.0
        computed_profit += profit

        result.slot_rows.append({
            "start": start_str,
            "end": end_str,
            "action": s.get("action", ""),
            "power_kw": s.get("power_kw") or 0.0,
            "profit_eur": profit,
            "reason": s.get("reason", "") or "",
            "is_current": is_current,
            "ok": ok,
        })

        if start_dt is not None:
            last_dt = start_dt

    result.computed_profit_sum = computed_profit
    if result.slot_count > 0 and abs(computed_profit - result.total_expected_profit_eur) > 1e-3:
        result.mismatches.append("profit_sum")
        result.overall_ok = False

    return result


# ---------------------------------------------------------------------------
# Battery deep check
# ---------------------------------------------------------------------------


@dataclass
class BatteryCheckResult:
    """Result of the battery deep-check: SOC range, capacity, power limits, and degradation cost."""

    skipped: bool = False
    skip_reason: str = ""
    # BatteryState (inputs.battery)
    soc: float | None = None
    power_kw: float | None = None
    estimated_capacity_kwh: float | None = None
    grid_power_kw: float | None = None
    # BatteryStatus (pipeline.battery_status)
    total_capacity_kwh: float | None = None
    max_charge_power_kw: float | None = None
    max_discharge_power_kw: float | None = None
    remaining_capacity_kwh: float | None = None
    # DegradationCost (pipeline.degradation_cost_per_kwh)
    degradation_cost_per_kwh: float | None = None
    expected_remaining_capacity_kwh: float | None = None
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_battery(snap: Snapshot) -> BatteryCheckResult:
    """Validate battery SOC, capacity consistency, and degradation cost presence.

    Args:
        snap: Coordinator snapshot containing inputs.battery, pipeline.battery_status,
              and pipeline.degradation_cost_per_kwh.

    Returns:
        BatteryCheckResult with observed values and overall pass/fail.
    """
    result = BatteryCheckResult()

    battery = snap.inputs.get("battery")
    if battery is None:
        result.skipped = True
        result.skip_reason = "inputs.battery is null (no battery configured)"
        return result

    result.soc = battery.get("soc")
    result.power_kw = battery.get("power_kw")
    result.estimated_capacity_kwh = battery.get("estimated_capacity_kwh")
    result.grid_power_kw = snap.inputs.get("grid_power_kw")
    result.degradation_cost_per_kwh = snap.pipeline.get("degradation_cost_per_kwh")

    bs = snap.pipeline.get("battery_status")
    if bs:
        result.total_capacity_kwh = bs.get("total_capacity_kwh")
        result.max_charge_power_kw = bs.get("max_charge_power_kw")
        result.max_discharge_power_kw = bs.get("max_discharge_power_kw")
        result.remaining_capacity_kwh = bs.get("remaining_capacity_kwh")

    # soc is stored as 0.0–1.0 fraction
    if result.soc is not None and not (0.0 <= result.soc <= 1.0):
        result.mismatches.append("soc_out_of_range")
        result.overall_ok = False

    if result.total_capacity_kwh and result.soc is not None and result.remaining_capacity_kwh is not None:
        expected_rem = result.soc * result.total_capacity_kwh
        result.expected_remaining_capacity_kwh = expected_rem
        if abs(expected_rem - result.remaining_capacity_kwh) > 0.1:
            result.mismatches.append("remaining_capacity_inconsistent")
            result.overall_ok = False

    if result.estimated_capacity_kwh:
        if result.degradation_cost_per_kwh is None or result.degradation_cost_per_kwh <= 0:
            result.mismatches.append("degradation_cost_missing")
            result.overall_ok = False

    return result


# ---------------------------------------------------------------------------
# Observed generation deep check
# ---------------------------------------------------------------------------


@dataclass
class ObservedGenerationCheckResult:
    """Result of the observed-generation deep-check: inverter counter vs declared totals."""

    skipped: bool = False
    skip_reason: str = ""
    slot_count: int = 0
    total_yesterday_kwh: float = 0.0
    total_today_so_far_kwh: float = 0.0
    computed_yesterday_kwh: float = 0.0
    computed_today_kwh: float = 0.0
    computed_at: str = ""
    slot_rows: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_observed_generation(snap: Snapshot) -> ObservedGenerationCheckResult:
    """Verify observed generation slot values are non-negative and daily totals match slot sums.

    Args:
        snap: Coordinator snapshot containing pipeline.observed_generation.

    Returns:
        ObservedGenerationCheckResult with per-slot data and overall pass/fail.
    """
    result = ObservedGenerationCheckResult()

    og = snap.pipeline.get("observed_generation")
    if not og:
        result.skipped = True
        result.skip_reason = "pipeline.observed_generation is null (no inverter history yet)"
        return result

    result.slot_count = og.get("slot_count", 0)
    result.total_yesterday_kwh = og.get("total_yesterday_kwh", 0.0)
    result.total_today_so_far_kwh = og.get("total_today_so_far_kwh", 0.0)
    result.computed_at = og.get("computed_at", "")

    now = datetime.now(timezone.utc)
    today = now.date()
    yesterday = today - timedelta(days=1)
    comp_yesterday = 0.0
    comp_today = 0.0

    for s in og.get("slots") or []:
        start_str = s.get("start", "")
        kwh = s.get("generated_kwh", 0.0)
        ok = kwh >= -1e-6
        if not ok:
            result.mismatches.append(start_str)
            result.overall_ok = False
        try:
            d = datetime.fromisoformat(start_str).astimezone(timezone.utc).date()
        except (ValueError, AttributeError):
            d = None
        if d == yesterday:
            comp_yesterday += kwh
        elif d == today:
            comp_today += kwh
        result.slot_rows.append({"start": start_str, "generated_kwh": kwh, "ok": ok})

    result.computed_yesterday_kwh = round(comp_yesterday, 4)
    result.computed_today_kwh = round(comp_today, 4)
    TOL = 0.01
    if abs(comp_yesterday - result.total_yesterday_kwh) > TOL:
        result.mismatches.append("yesterday_total_mismatch")
        result.overall_ok = False
    if abs(comp_today - result.total_today_so_far_kwh) > TOL:
        result.mismatches.append("today_total_mismatch")
        result.overall_ok = False

    return result


# ---------------------------------------------------------------------------
# Forecast accuracy deep check
# ---------------------------------------------------------------------------


@dataclass
class ForecastAccuracyCheckResult:
    """Result of the forecast-accuracy deep-check: error arithmetic and aggregate metrics."""

    skipped: bool = False
    skip_reason: str = ""
    slot_count: int = 0
    total_forecast_kwh: float = 0.0
    total_observed_kwh: float = 0.0
    total_error_kwh: float = 0.0
    mean_absolute_error_kwh: float = 0.0
    bias_kwh: float = 0.0
    mape: float | None = None
    computed_at: str = ""
    slot_rows: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_forecast_accuracy(snap: Snapshot) -> ForecastAccuracyCheckResult:
    """Verify per-slot error arithmetic and total_error_kwh sum match slot data.

    Args:
        snap: Coordinator snapshot containing pipeline.forecast_error.

    Returns:
        ForecastAccuracyCheckResult with per-slot errors and overall pass/fail.
    """
    result = ForecastAccuracyCheckResult()

    fe = snap.pipeline.get("forecast_error")
    if not fe:
        result.skipped = True
        result.skip_reason = "pipeline.forecast_error is null (no inverter history for comparison)"
        return result

    result.slot_count = fe.get("slot_count", 0)
    result.total_forecast_kwh = fe.get("total_forecast_kwh", 0.0)
    result.total_observed_kwh = fe.get("total_observed_kwh", 0.0)
    result.total_error_kwh = fe.get("total_error_kwh", 0.0)
    result.mean_absolute_error_kwh = fe.get("mean_absolute_error_kwh", 0.0)
    result.bias_kwh = fe.get("bias_kwh", 0.0)
    result.mape = fe.get("mean_absolute_percentage_error")
    result.computed_at = fe.get("computed_at", "")

    comp_error = 0.0
    for s in fe.get("slots") or []:
        start_str = s.get("start", "")
        f_kwh = s.get("forecast_kwh", 0.0)
        o_kwh = s.get("observed_kwh", 0.0)
        e_kwh = s.get("error_kwh", 0.0)
        ok = abs(e_kwh - (o_kwh - f_kwh)) < 1e-4
        if not ok:
            result.mismatches.append(start_str)
            result.overall_ok = False
        comp_error += e_kwh
        result.slot_rows.append({
            "start": start_str,
            "forecast_kwh": f_kwh,
            "observed_kwh": o_kwh,
            "error_kwh": e_kwh,
            "relative_error": s.get("relative_error"),
            "ok": ok,
        })

    if result.slot_count > 0 and abs(comp_error - result.total_error_kwh) > 0.01:
        result.mismatches.append("total_error_sum_mismatch")
        result.overall_ok = False

    return result


# ---------------------------------------------------------------------------
# Charging profile deep check
# ---------------------------------------------------------------------------


@dataclass
class ChargingProfileCheckResult:
    """Result of the charging-profile deep-check: mode logic and aggregate kWh sums."""

    skipped: bool = False
    skip_reason: str = ""
    slot_count: int = 0
    free_capacity_kwh: float = 0.0
    today_remaining_generation_kwh: float = 0.0
    solar_exceeds_capacity: bool = False
    allocated_solar_kwh: float = 0.0
    total_no_export_kwh: float = 0.0
    computed_allocated_kwh: float = 0.0
    computed_no_export_kwh: float = 0.0
    computed_at: str = ""
    slot_rows: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_charging_profile(snap: Snapshot) -> ChargingProfileCheckResult:
    """Verify charging profile sell/no-export mode logic and allocated kWh sums.

    SELL slots must have positive sell price; NO_EXPORT slots must have non-positive sell price.
    Slot-level sums of each mode must match the declared allocated_solar_kwh and total_no_export_kwh.

    Args:
        snap: Coordinator snapshot containing pipeline.charging_profile.

    Returns:
        ChargingProfileCheckResult with per-slot mode data and overall pass/fail.
    """
    result = ChargingProfileCheckResult()

    cp = snap.pipeline.get("charging_profile")
    if not cp:
        result.skipped = True
        result.skip_reason = "pipeline.charging_profile is null"
        return result

    result.slot_count = cp.get("slot_count", 0)
    result.free_capacity_kwh = cp.get("free_capacity_kwh", 0.0)
    result.today_remaining_generation_kwh = cp.get("today_remaining_generation_kwh", 0.0)
    result.solar_exceeds_capacity = cp.get("solar_exceeds_capacity", False)
    result.allocated_solar_kwh = cp.get("allocated_solar_kwh", 0.0)
    result.total_no_export_kwh = cp.get("total_no_export_kwh", 0.0)
    result.computed_at = cp.get("computed_at", "")

    comp_allocated = 0.0
    comp_no_export = 0.0

    for s in cp.get("slots") or []:
        start_str = s.get("start", "")
        mode = (s.get("mode") or "").lower()
        expected_kwh = s.get("expected_kwh", 0.0)
        sell_eur = s.get("sell_eur_kwh", 0.0)

        ok = True
        if mode == "sell" and sell_eur <= 0:
            result.mismatches.append(f"sell_no_price:{start_str[:16]}")
            result.overall_ok = False
            ok = False
        elif mode == "no_export" and sell_eur > 0:
            result.mismatches.append(f"no_export_positive_price:{start_str[:16]}")
            result.overall_ok = False
            ok = False

        if mode == "solar_charge":
            comp_allocated += expected_kwh
        elif mode == "no_export":
            comp_no_export += expected_kwh

        result.slot_rows.append({
            "start": start_str,
            "mode": mode,
            "expected_kwh": expected_kwh,
            "sell_eur_kwh": sell_eur,
            "ok": ok,
        })

    result.computed_allocated_kwh = round(comp_allocated, 4)
    result.computed_no_export_kwh = round(comp_no_export, 4)
    TOL = 0.01
    if abs(comp_allocated - result.allocated_solar_kwh) > TOL:
        result.mismatches.append("allocated_solar_sum_mismatch")
        result.overall_ok = False
    if abs(comp_no_export - result.total_no_export_kwh) > TOL:
        result.mismatches.append("no_export_sum_mismatch")
        result.overall_ok = False

    return result


# ---------------------------------------------------------------------------
# Base load deep check
# ---------------------------------------------------------------------------


@dataclass
class BaseLoadCheckResult:
    """Result of the base-load deep-check: 24 hourly slots, non-negative values, confidence."""

    skipped: bool = False
    skip_reason: str = ""
    fallback_kw: float = 0.0
    overall_p10_kw: float = 0.0
    overall_median_kw: float = 0.0
    confidence: float | None = None
    sample_count: int = 0
    distinct_days: int = 0
    computed_at: str = ""
    slot_rows: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_base_load(snap: Snapshot) -> BaseLoadCheckResult:
    """Verify base-load profile has 24 slots with non-negative kW values.

    Args:
        snap: Coordinator snapshot containing pipeline.base_load_profile.

    Returns:
        BaseLoadCheckResult with per-hour data and overall pass/fail.
    """
    result = BaseLoadCheckResult()

    blp = snap.pipeline.get("base_load_profile")
    if not blp:
        result.skipped = True
        result.skip_reason = "pipeline.base_load_profile is null"
        return result

    result.fallback_kw = blp.get("fallback_kw", 0.0)
    result.overall_p10_kw = blp.get("overall_p10_kw", 0.0)
    result.overall_median_kw = blp.get("overall_median_kw", 0.0)
    result.confidence = blp.get("confidence")
    result.sample_count = blp.get("sample_count", 0)
    result.distinct_days = blp.get("distinct_days", 0)
    result.computed_at = blp.get("computed_at", "")

    slots = blp.get("slots") or []
    if len(slots) != 24:
        result.mismatches.append(f"slot_count={len(slots)} (expected 24)")
        result.overall_ok = False

    if result.confidence is not None and not (0.0 <= result.confidence <= 1.0):
        result.mismatches.append("confidence_out_of_range")
        result.overall_ok = False

    for s in slots:
        hour = s.get("hour", 0)
        kw = s.get("baseload_kw", 0.0)
        ok = kw >= 0.0
        if not ok:
            result.mismatches.append(f"negative_kw_h{hour}")
            result.overall_ok = False
        result.slot_rows.append({
            "hour": hour,
            "baseload_kw": kw,
            "sample_count": s.get("sample_count", 0),
            "is_fallback": s.get("is_fallback", False),
            "ok": ok,
        })

    return result


# ---------------------------------------------------------------------------
# Battery runtime deep check
# ---------------------------------------------------------------------------


@dataclass
class BatteryRuntimeCheckResult:
    """Result of the battery-runtime deep-check: usable kWh, drain rate, and runtime estimate."""

    skipped: bool = False
    skip_reason: str = ""
    remaining_kwh_usable: float = 0.0
    avg_drain_kw_next_hour: float = 0.0
    runtime_minutes: float | None = None
    expected_runtime_minutes: float | None = None
    until: str | None = None
    horizon_hours: int = 0
    computed_at: str = ""
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_battery_runtime(snap: Snapshot) -> BatteryRuntimeCheckResult:
    """Validate battery runtime estimate: non-negative remaining kWh and drain rate.

    Args:
        snap: Coordinator snapshot containing pipeline.battery_runtime.

    Returns:
        BatteryRuntimeCheckResult with runtime data and overall pass/fail.
    """
    result = BatteryRuntimeCheckResult()

    brt = snap.pipeline.get("battery_runtime")
    if not brt:
        result.skipped = True
        result.skip_reason = "pipeline.battery_runtime is null"
        return result

    result.remaining_kwh_usable = brt.get("remaining_kwh_usable", 0.0)
    result.avg_drain_kw_next_hour = brt.get("avg_drain_kw_next_hour", 0.0)
    result.runtime_minutes = brt.get("runtime_minutes")
    result.until = brt.get("until")
    result.horizon_hours = brt.get("horizon_hours", 0)
    result.computed_at = brt.get("computed_at", "")

    if result.remaining_kwh_usable < -1e-6:
        result.mismatches.append("negative_remaining_kwh")
        result.overall_ok = False

    if result.avg_drain_kw_next_hour < 0:
        result.mismatches.append("negative_drain_rate")
        result.overall_ok = False

    if result.avg_drain_kw_next_hour > 0:
        expected_rt = (result.remaining_kwh_usable / result.avg_drain_kw_next_hour) * 60.0
        result.expected_runtime_minutes = round(expected_rt, 1)
        if result.runtime_minutes is not None and abs(result.runtime_minutes - expected_rt) > 1.0:
            result.mismatches.append("runtime_formula_mismatch")
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
        yesterday_store_slots: list[tuple[datetime, float]] | None = None,
    ) -> None:
        """Initialise with forecast check data.

        Args:
            entity_slots: All parsed raw entity slots from check_forecast.
            array_eids: Base (today) entity IDs for up to two solar arrays.
            module_slots: Module GenerationSlot dicts from the pipeline debug endpoint.
            yesterday_store_slots: Parsed coordinator-store entries for yesterday (utc_dt, kwh).
        """
        super().__init__()
        self._entity_slots = entity_slots
        self._array_eids = array_eids
        self._module_slots = module_slots
        self._yesterday_store: dict[datetime, float] = {
            dt: kwh for dt, kwh in (yesterday_store_slots or [])
        }

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with slot rows for yesterday, today, and tomorrow."""
        now_utc = datetime.now(timezone.utc)
        today_d = now_utc.date()
        yesterday_d = today_d - timedelta(days=1)
        tomorrow_d = today_d + timedelta(days=1)
        in_range = frozenset((yesterday_d, today_d, tomorrow_d))

        array_data: list[dict[datetime, float]] = []
        for base_eid in (list(self._array_eids) + ["", ""])[:2]:
            tomorrow_eid = _tomorrow_eid(base_eid) if base_eid else ""
            dmap: dict[datetime, float] = {}
            for es in self._entity_slots:
                # Yesterday has no entity source; only today + tomorrow come from raw entities.
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
                if dt.date() == yesterday_d:
                    label = "yesterday (module only — no entity source)"
                elif dt.date() == today_d:
                    label = "today"
                else:
                    label = "tomorrow"
                table.add_row(Text(label, style="bold"), *[""] * 5)
                prev_date = dt.date()

            is_yday = dt.date() == yesterday_d
            if is_yday:
                store_kwh = self._yesterday_store.get(dt)
                table.add_row(
                    Text(dt.strftime("%H:%M"), style="cyan"),
                    Text("—", style=dim),
                    Text("—", style=dim),
                    Text(f"{store_kwh:.4f}kWh", style=dim if store_kwh == 0 else "")
                    if store_kwh is not None else Text("—", style=dim),
                    Text("—", style=dim),
                    Text(f"{v_mod:.4f}", style=dim if v_mod == 0 else "") if v_mod is not None
                    else Text("—", style=dim),
                )
            else:
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

        # Yesterday row first — store total vs module total; no per-array breakdown available.
        dim = "dim"
        store_known = bool(fc.yesterday_store_slots)
        yday_label = f"yesterday ({fc.yesterday_store_date})" if fc.yesterday_store_date else "yesterday"
        table.add_row(
            Text(yday_label, style="bold dim"),
            Text("—", style=dim),
            Text("—", style=dim),
            Text(f"{fc.yesterday_store_total_kwh:.4f}", style=dim if fc.yesterday_store_total_kwh == 0 else "")
            if store_known else Text("—", style=dim),
            Text(f"{fc.module_yesterday_kwh:.4f}", style=dim if fc.module_yesterday_kwh == 0 else ""),
            Text(""),   # display only — no pass/fail check for yesterday
        )

        day_plan: list[tuple[str, int | None]] = (
            [("today", None), ("tomorrow", None)]
            + [(f"d{n}", n) for n in range(2, 7)]
        )

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
                yield ForecastSlotsTable(fc.entity_slots, fc.array_eids, fc.module_slots, fc.yesterday_store_slots)
                if fc.negative_slots:
                    yield Static(f"  ⚠ {len(fc.negative_slots)} negative slot(s): {fc.negative_slots[:3]}")

            with Collapsible(title="Summary", collapsed=False):
                yield Static(f"  slot_count: {fc.module_slot_count}")
                yield ForecastSummaryTable(fc)


class ObservedGenerationSlotsTable(Static):
    """DataTable: Time | Generated (kWh) | ✓/✗, grouped by date."""

    DEFAULT_CSS = """
    ObservedGenerationSlotsTable { height: auto; }
    ObservedGenerationSlotsTable DataTable { height: 18; }
    """

    def __init__(self, og: ObservedGenerationCheckResult) -> None:
        """Initialise with the observed generation check result.

        Args:
            og: Result of check_observed_generation() containing per-slot generated kWh.
        """
        super().__init__()
        self._og = og

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per slot, date-separated."""
        table = self.query_one(DataTable)
        table.add_columns("Time", "Generated (kWh)", "")
        dim = "dim"
        prev_date = None

        for row in self._og.slot_rows:
            try:
                dt = datetime.fromisoformat(row["start"]).astimezone(timezone.utc)
                time_str = dt.strftime("%H:%M")
                cur_date = dt.date()
            except (ValueError, AttributeError):
                time_str = row["start"]
                cur_date = None

            if cur_date is not None and cur_date != prev_date:
                if prev_date is not None:
                    table.add_row(*[Text("─", style=dim)] * 3)
                table.add_row(Text(str(cur_date), style="bold"), "", "")
                prev_date = cur_date

            kwh = row["generated_kwh"]
            ok = row["ok"]
            table.add_row(
                Text(time_str, style="cyan"),
                Text(f"{kwh:.4f}", style=dim if kwh == 0 else ""),
                Text("✗" if not ok else "", style="red"),
            )


class ObservedGenerationCheckWidget(Static):
    """Collapsible observed-generation deep-check: inverter slots and daily totals."""

    DEFAULT_CSS = "ObservedGenerationCheckWidget { height: auto; }"

    def __init__(self, og: ObservedGenerationCheckResult) -> None:
        """Initialise with the pre-computed observed generation check result.

        Args:
            og: Result of check_observed_generation() for one coordinator.
        """
        super().__init__()
        self._og = og

    def compose(self) -> ComposeResult:
        """Render Slots sub-Collapsible and yesterday/today totals."""
        og = self._og

        if og.skipped:
            yield Static(f"  ⚠  observed_generation_check   SKIP   {og.skip_reason}")
            return

        color = "green" if og.overall_ok else "red"
        mark = "✓" if og.overall_ok else "✗"
        status = "PASS" if og.overall_ok else "FAIL"
        title = (
            f"[{color}]{mark}[/{color}]  observed_generation_check   [{color}]{status}[/{color}]"
            f"   {og.slot_count} slots"
            f"   yest={og.total_yesterday_kwh:.3f}kWh  today={og.total_today_so_far_kwh:.3f}kWh"
        )

        with Collapsible(title=title, collapsed=True):
            yest_ok = "yesterday_total_mismatch" not in og.mismatches
            today_ok = "today_total_mismatch" not in og.mismatches
            yest_style = "green" if yest_ok else "red"
            today_style = "green" if today_ok else "red"
            yield Static(
                f"  yesterday: computed [{yest_style}]{og.computed_yesterday_kwh:.4f}[/{yest_style}]kWh"
                f"  declared {og.total_yesterday_kwh:.4f}kWh\n"
                f"  today:     computed [{today_style}]{og.computed_today_kwh:.4f}[/{today_style}]kWh"
                f"  declared {og.total_today_so_far_kwh:.4f}kWh",
                markup=True,
            )
            with Collapsible(title="Slots", collapsed=False):
                yield ObservedGenerationSlotsTable(og)


class ForecastAccuracySlotsTable(Static):
    """DataTable: Time | Forecast (kWh) | Observed (kWh) | Error (kWh) | Rel Error | ✓/✗."""

    DEFAULT_CSS = """
    ForecastAccuracySlotsTable { height: auto; }
    ForecastAccuracySlotsTable DataTable { height: 18; }
    """

    def __init__(self, fa: ForecastAccuracyCheckResult) -> None:
        """Initialise with the forecast accuracy check result.

        Args:
            fa: Result of check_forecast_accuracy() containing per-slot error data.
        """
        super().__init__()
        self._fa = fa

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per error slot."""
        table = self.query_one(DataTable)
        table.add_columns("Time", "Forecast (kWh)", "Observed (kWh)", "Error (kWh)", "Rel Err", "")
        dim = "dim"
        prev_date = None

        for row in self._fa.slot_rows:
            try:
                dt = datetime.fromisoformat(row["start"]).astimezone(timezone.utc)
                time_str = dt.strftime("%H:%M")
                cur_date = dt.date()
            except (ValueError, AttributeError):
                time_str = row["start"]
                cur_date = None

            if cur_date is not None and cur_date != prev_date:
                if prev_date is not None:
                    table.add_row(*[Text("─", style=dim)] * 6)
                table.add_row(Text(str(cur_date), style="bold"), *[""] * 5)
                prev_date = cur_date

            err = row["error_kwh"]
            ok = row["ok"]
            err_style = "green" if err > 0.01 else ("red" if err < -0.01 else dim)
            rel = row["relative_error"]
            rel_str = f"{rel:.1%}" if rel is not None else "—"

            table.add_row(
                Text(time_str, style="cyan"),
                Text(f"{row['forecast_kwh']:.4f}", style=dim),
                Text(f"{row['observed_kwh']:.4f}"),
                Text(f"{err:+.4f}", style=err_style),
                Text(rel_str, style=dim),
                Text("✗" if not ok else "", style="red"),
            )


class ForecastAccuracyCheckWidget(Static):
    """Collapsible forecast-accuracy deep-check: per-slot errors and aggregate metrics."""

    DEFAULT_CSS = "ForecastAccuracyCheckWidget { height: auto; }"

    def __init__(self, fa: ForecastAccuracyCheckResult) -> None:
        """Initialise with the pre-computed forecast accuracy check result.

        Args:
            fa: Result of check_forecast_accuracy() for one coordinator.
        """
        super().__init__()
        self._fa = fa

    def compose(self) -> ComposeResult:
        """Render Slots sub-Collapsible and metric summary."""
        fa = self._fa

        if fa.skipped:
            yield Static(f"  ⚠  forecast_accuracy_check   SKIP   {fa.skip_reason}")
            return

        color = "green" if fa.overall_ok else "red"
        mark = "✓" if fa.overall_ok else "✗"
        status = "PASS" if fa.overall_ok else "FAIL"
        mape_str = f"  MAPE={fa.mape:.1%}" if fa.mape is not None else ""
        title = (
            f"[{color}]{mark}[/{color}]  forecast_accuracy_check   [{color}]{status}[/{color}]"
            f"   {fa.slot_count} slots"
            f"   MAE={fa.mean_absolute_error_kwh:.4f}kWh  bias={fa.bias_kwh:+.4f}kWh{mape_str}"
        )

        with Collapsible(title=title, collapsed=True):
            lines = (
                f"  forecast={fa.total_forecast_kwh:.3f}kWh  "
                f"observed={fa.total_observed_kwh:.3f}kWh  "
                f"error={fa.total_error_kwh:+.3f}kWh"
            )
            yield Static(lines)
            with Collapsible(title="Slots", collapsed=False):
                yield ForecastAccuracySlotsTable(fa)


class ChargingProfileSlotsTable(Static):
    """DataTable: Time | Mode | Expected (kWh) | Sell (€/kWh) | ✓/✗."""

    DEFAULT_CSS = """
    ChargingProfileSlotsTable { height: auto; }
    ChargingProfileSlotsTable DataTable { height: 15; }
    """

    def __init__(self, cp: ChargingProfileCheckResult) -> None:
        """Initialise with the charging profile check result.

        Args:
            cp: Result of check_charging_profile() containing per-slot mode assignments.
        """
        super().__init__()
        self._cp = cp

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per charging profile slot."""
        table = self.query_one(DataTable)
        table.add_columns("Time", "Mode", "Expected (kWh)", "Sell (€/kWh)", "")
        dim = "dim"

        mode_styles = {
            "solar_charge": "green",
            "sell": "cyan",
            "no_export": "yellow",
            "idle": "dim",
        }

        for row in self._cp.slot_rows:
            try:
                dt = datetime.fromisoformat(row["start"]).astimezone(timezone.utc)
                time_str = dt.strftime("%H:%M")
            except (ValueError, AttributeError):
                time_str = row["start"]

            mode = row["mode"]
            ok = row["ok"]
            kwh = row["expected_kwh"]
            sell = row["sell_eur_kwh"]
            m_style = mode_styles.get(mode, "")

            table.add_row(
                Text(time_str, style="cyan"),
                Text(mode, style=m_style),
                Text(f"{kwh:.4f}", style=dim if kwh == 0 else ""),
                Text(f"{sell:.4f}", style=dim if sell == 0 else ""),
                Text("✗" if not ok else "", style="red"),
            )


class ChargingProfileCheckWidget(Static):
    """Collapsible charging-profile deep-check: mode logic and kWh allocation sums."""

    DEFAULT_CSS = "ChargingProfileCheckWidget { height: auto; }"

    def __init__(self, cp: ChargingProfileCheckResult) -> None:
        """Initialise with the pre-computed charging profile check result.

        Args:
            cp: Result of check_charging_profile() for one coordinator.
        """
        super().__init__()
        self._cp = cp

    def compose(self) -> ComposeResult:
        """Render profile summary line and Slots sub-Collapsible."""
        cp = self._cp

        if cp.skipped:
            yield Static(f"  ⚠  charging_profile_check   SKIP   {cp.skip_reason}")
            return

        color = "green" if cp.overall_ok else "red"
        mark = "✓" if cp.overall_ok else "✗"
        status = "PASS" if cp.overall_ok else "FAIL"
        exceed = "  ⚠ solar>capacity" if cp.solar_exceeds_capacity else ""
        title = (
            f"[{color}]{mark}[/{color}]  charging_profile_check   [{color}]{status}[/{color}]"
            f"   {cp.slot_count} slots"
            f"   free={cp.free_capacity_kwh:.3f}kWh"
            f"   allocated={cp.allocated_solar_kwh:.3f}kWh"
            f"   no_export={cp.total_no_export_kwh:.3f}kWh{exceed}"
        )

        with Collapsible(title=title, collapsed=True):
            alloc_ok = "allocated_solar_sum_mismatch" not in cp.mismatches
            noexp_ok = "no_export_sum_mismatch" not in cp.mismatches
            alloc_style = "green" if alloc_ok else "red"
            noexp_style = "green" if noexp_ok else "red"
            yield Static(
                f"  allocated:  computed [{alloc_style}]{cp.computed_allocated_kwh:.4f}[/{alloc_style}]kWh"
                f"  declared {cp.allocated_solar_kwh:.4f}kWh\n"
                f"  no_export:  computed [{noexp_style}]{cp.computed_no_export_kwh:.4f}[/{noexp_style}]kWh"
                f"  declared {cp.total_no_export_kwh:.4f}kWh",
                markup=True,
            )
            with Collapsible(title="Slots", collapsed=False):
                yield ChargingProfileSlotsTable(cp)


class BaseLoadSlotsTable(Static):
    """DataTable: Hour | Baseload (kW) | Samples | Fallback? | ✓/✗."""

    DEFAULT_CSS = """
    BaseLoadSlotsTable { height: auto; }
    BaseLoadSlotsTable DataTable { height: 15; }
    """

    def __init__(self, bl: BaseLoadCheckResult) -> None:
        """Initialise with the base load check result.

        Args:
            bl: Result of check_base_load() containing per-hour baseload data.
        """
        super().__init__()
        self._bl = bl

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per hour (0–23)."""
        table = self.query_one(DataTable)
        table.add_columns("Hour", "Baseload (kW)", "Samples", "Fallback", "")
        dim = "dim"

        for row in self._bl.slot_rows:
            ok = row["ok"]
            fallback = row["is_fallback"]
            table.add_row(
                Text(f"{row['hour']:02d}:00"),
                Text(f"{row['baseload_kw']:.4f}", style="red" if not ok else ""),
                Text(str(row["sample_count"]), style=dim if row["sample_count"] == 0 else ""),
                Text("fallback" if fallback else "", style=dim),
                Text("✗" if not ok else "", style="red"),
            )


class BaseLoadCheckWidget(Static):
    """Collapsible base-load deep-check: 24-hour profile with confidence and metrics."""

    DEFAULT_CSS = "BaseLoadCheckWidget { height: auto; }"

    def __init__(self, bl: BaseLoadCheckResult) -> None:
        """Initialise with the pre-computed base load check result.

        Args:
            bl: Result of check_base_load() for one coordinator.
        """
        super().__init__()
        self._bl = bl

    def compose(self) -> ComposeResult:
        """Render metric summary and Slots sub-Collapsible."""
        bl = self._bl

        if bl.skipped:
            yield Static(f"  ⚠  base_load_check   SKIP   {bl.skip_reason}")
            return

        color = "green" if bl.overall_ok else "red"
        mark = "✓" if bl.overall_ok else "✗"
        status = "PASS" if bl.overall_ok else "FAIL"
        conf_str = f"{bl.confidence:.0%}" if bl.confidence is not None else "low"
        title = (
            f"[{color}]{mark}[/{color}]  base_load_check   [{color}]{status}[/{color}]"
            f"   median={bl.overall_median_kw:.3f}kW  P10={bl.overall_p10_kw:.3f}kW"
            f"   confidence={conf_str}  days={bl.distinct_days}"
        )

        with Collapsible(title=title, collapsed=True):
            with Collapsible(title="Slots (24h profile)", collapsed=False):
                yield BaseLoadSlotsTable(bl)


class BatteryRuntimeCheckWidget(Static):
    """Collapsible battery-runtime deep-check: usable kWh, drain rate, and until estimate."""

    DEFAULT_CSS = "BatteryRuntimeCheckWidget { height: auto; }"

    def __init__(self, brt: BatteryRuntimeCheckResult) -> None:
        """Initialise with the pre-computed battery runtime check result.

        Args:
            brt: Result of check_battery_runtime() for one coordinator.
        """
        super().__init__()
        self._brt = brt

    def compose(self) -> ComposeResult:
        """Render runtime fields as a simple Static block."""
        brt = self._brt

        if brt.skipped:
            yield Static(f"  ⚠  battery_runtime_check   SKIP   {brt.skip_reason}")
            return

        color = "green" if brt.overall_ok else "red"
        mark = "✓" if brt.overall_ok else "✗"
        status = "PASS" if brt.overall_ok else "FAIL"
        rt_str = f"{brt.runtime_minutes:.0f}min" if brt.runtime_minutes is not None else "∞"
        title = (
            f"[{color}]{mark}[/{color}]  battery_runtime_check   [{color}]{status}[/{color}]"
            f"   {brt.remaining_kwh_usable:.3f}kWh usable"
            f"   drain={brt.avg_drain_kw_next_hour:.3f}kW"
            f"   runtime={rt_str}"
        )

        with Collapsible(title=title, collapsed=True):
            rt_ok = "runtime_formula_mismatch" not in brt.mismatches
            rt_style = "green" if rt_ok else "red"
            exp_rt_str = (
                f"  Expected runtime: [{rt_style}]{brt.expected_runtime_minutes:.0f}[/{rt_style}]min"
                f"  (usable/drain×60)  actual: {rt_str}\n"
                if brt.expected_runtime_minutes is not None else ""
            )
            lines = [
                f"  Remaining usable: {brt.remaining_kwh_usable:.4f} kWh",
                f"  Avg drain next hour: {brt.avg_drain_kw_next_hour:.4f} kW",
                f"  Runtime: {rt_str}",
                f"  Until: {brt.until or '—'}",
                f"  Horizon: {brt.horizon_hours}h",
                f"  Computed at: {brt.computed_at}",
            ]
            yield Static(exp_rt_str + "\n".join(lines), markup=True)


# ---------------------------------------------------------------------------
# Household consumption deep check
# ---------------------------------------------------------------------------


@dataclass
class HouseholdConsumptionCheckResult:
    """Result of the household-consumption deep-check: today-total kWh sanity."""

    skipped: bool = False
    skip_reason: str = ""
    consumption_today_kwh: float | None = None
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_household_consumption(snap: Snapshot) -> HouseholdConsumptionCheckResult:
    """Validate household consumption today-total is non-negative when present.

    Args:
        snap: Coordinator snapshot containing inputs.consumption_today_kwh.

    Returns:
        HouseholdConsumptionCheckResult with observed value and overall pass/fail.
    """
    result = HouseholdConsumptionCheckResult()

    raw = snap.inputs.get("consumption_today_kwh")
    if raw is None:
        result.skipped = True
        result.skip_reason = "inputs.consumption_today_kwh is null (sensor not configured)"
        return result

    try:
        result.consumption_today_kwh = float(raw)
    except (TypeError, ValueError):
        result.mismatches.append("non_numeric_value")
        result.overall_ok = False
        return result

    if result.consumption_today_kwh < 0.0:
        result.mismatches.append("negative_consumption")
        result.overall_ok = False

    return result


class HouseholdConsumptionCheckWidget(Static):
    """Collapsible household-consumption deep-check: today-total kWh sanity."""

    DEFAULT_CSS = "HouseholdConsumptionCheckWidget { height: auto; }"

    def __init__(self, hc: HouseholdConsumptionCheckResult) -> None:
        """Initialise with the pre-computed household consumption check result.

        Args:
            hc: Result of check_household_consumption() for one coordinator.
        """
        super().__init__()
        self._hc = hc

    def compose(self) -> ComposeResult:
        """Render consumption value and sanity status."""
        hc = self._hc

        if hc.skipped:
            yield Static(f"  ⚠  household_consumption_check   SKIP   {hc.skip_reason}")
            return

        color = "green" if hc.overall_ok else "red"
        mark = "✓" if hc.overall_ok else "✗"
        status = "PASS" if hc.overall_ok else "FAIL"
        kwh_str = f"{hc.consumption_today_kwh:.3f}kWh" if hc.consumption_today_kwh is not None else "—"
        title = (
            f"[{color}]{mark}[/{color}]  household_consumption_check   [{color}]{status}[/{color}]"
            f"   today={kwh_str}"
        )

        with Collapsible(title=title, collapsed=True):
            yield Static(f"  Consumption today: {kwh_str}")


# ---------------------------------------------------------------------------
# Profitability deep check
# ---------------------------------------------------------------------------


@dataclass
class ProfitabilityCheckResult:
    """Result of the profitability deep-check: score range and peak cross-check."""

    skipped: bool = False
    skip_reason: str = ""
    score: float | None = None
    today_peak_eur_kwh: float = 0.0
    today_class: str = ""
    window_days: int = 0
    class_medians: dict = field(default_factory=dict)
    computed_at: str = ""
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_profitability(snap: Snapshot) -> ProfitabilityCheckResult:
    """Validate profitability score range and cross-check today's peak against pipeline.pricing.

    Args:
        snap: Coordinator snapshot containing pipeline.profitability_score and pipeline.pricing.

    Returns:
        ProfitabilityCheckResult with score data and overall pass/fail.
    """
    result = ProfitabilityCheckResult()

    ps = snap.pipeline.get("profitability_score")
    if not ps:
        result.skipped = True
        result.skip_reason = "pipeline.profitability_score is null (insufficient price history)"
        return result

    result.score = ps.get("score")
    result.today_peak_eur_kwh = ps.get("today_peak_eur_kwh", 0.0)
    result.today_class = ps.get("today_class", "")
    result.window_days = ps.get("window_days", 0)
    result.class_medians = dict(ps.get("class_medians") or {})
    result.computed_at = ps.get("computed_at", "")

    if result.score is not None and not (0.0 <= result.score <= 1.0):
        result.mismatches.append("score_out_of_range")
        result.overall_ok = False

    # Cross-check: today's peak should equal max spot across today's pricing slots.
    pricing = snap.pipeline.get("pricing")
    if pricing and result.today_peak_eur_kwh > 0:
        now_utc = datetime.now(timezone.utc)
        today_utc = now_utc.date()
        today_spots = []
        for s in pricing.get("slots") or []:
            try:
                slot_date = datetime.fromisoformat(s["start"]).astimezone(timezone.utc).date()
            except (KeyError, ValueError):
                continue
            if slot_date == today_utc:
                today_spots.append(s.get("spot", 0.0))
        if today_spots:
            expected_peak = max(today_spots)
            if abs(expected_peak - result.today_peak_eur_kwh) > 1e-3:
                result.mismatches.append("peak_mismatch")
                result.overall_ok = False

    return result


class ProfitabilityCheckWidget(Static):
    """Collapsible profitability deep-check: score, peak, and window stats."""

    DEFAULT_CSS = "ProfitabilityCheckWidget { height: auto; }"

    def __init__(self, pr: ProfitabilityCheckResult) -> None:
        """Initialise with the pre-computed profitability check result.

        Args:
            pr: Result of check_profitability() for one coordinator.
        """
        super().__init__()
        self._pr = pr

    def compose(self) -> ComposeResult:
        """Render score and window stats as a collapsible static block."""
        pr = self._pr

        if pr.skipped:
            yield Static(f"  ⚠  profitability_check   SKIP   {pr.skip_reason}")
            return

        color = "green" if pr.overall_ok else "red"
        mark = "✓" if pr.overall_ok else "✗"
        status = "PASS" if pr.overall_ok else "FAIL"
        score_str = f"{pr.score:.0%}" if pr.score is not None else "sparse"
        title = (
            f"[{color}]{mark}[/{color}]  profitability_check   [{color}]{status}[/{color}]"
            f"   score={score_str}  peak={pr.today_peak_eur_kwh:.4f}€/kWh"
            f"  class={pr.today_class}  window={pr.window_days}d"
        )

        with Collapsible(title=title, collapsed=True):
            medians_str = ", ".join(
                f"{k}={v:.4f}" for k, v in sorted(pr.class_medians.items())
            )
            peak_ok = "peak_mismatch" not in pr.mismatches
            peak_style = "green" if peak_ok else "red"
            yield Static(
                f"  Today class: {pr.today_class}\n"
                f"  Today peak: [{peak_style}]{pr.today_peak_eur_kwh:.4f}[/{peak_style}] €/kWh\n"
                f"  Score: {score_str}\n"
                f"  Window: {pr.window_days} days\n"
                f"  Class medians: {medians_str or '—'}\n"
                f"  Computed at: {pr.computed_at}",
                markup=True,
            )


@dataclass
class ForecastQualityCheckResult:
    """Result of the forecast quality deep-check: EMA bucket counts and metric ranges."""

    skipped: bool = False
    skip_reason: str = ""
    sunrise_utc: str = ""
    sunset_utc: str = ""
    group1_bucket_count: int = 0
    group2_bucket_count: int = 0
    group3_bucket_count: int = 0
    group3_pending_count: int = 0
    group1_buckets: list[dict] = field(default_factory=list)
    group2_buckets: list[dict] = field(default_factory=list)
    group3_buckets: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_forecast_quality(snap: Snapshot) -> ForecastQualityCheckResult:
    """Validate forecast quality store structure and metric plausibility.

    Args:
        snap: Coordinator snapshot containing pipeline.forecast_quality.

    Returns:
        ForecastQualityCheckResult with bucket counts and overall pass/fail.
    """
    result = ForecastQualityCheckResult()
    fq = snap.pipeline.get("forecast_quality")
    if not fq:
        result.skipped = True
        result.skip_reason = "pipeline.forecast_quality is null (no quality data yet)"
        return result

    result.sunrise_utc = fq.get("sunrise_utc") or ""
    result.sunset_utc  = fq.get("sunset_utc") or ""
    result.group3_pending_count = fq.get("group3_pending_count", 0)

    def _validate_buckets(group_dict: dict, label: str) -> list[dict]:
        rows: list[dict] = []
        for key, m in (group_dict or {}).items():
            n = m.get("n", 0)
            mae = m.get("mae_wh")
            rmse = m.get("rmse_wh")
            mape = m.get("mape_pct")
            r2   = m.get("r2")
            ok = True
            issues = []
            if n < 0:
                ok = False
                issues.append("negative_n")
            if mae is not None and mae < 0:
                ok = False
                issues.append("negative_mae")
            if rmse is not None and rmse < 0:
                ok = False
                issues.append("negative_rmse")
            if mape is not None and mape < 0:
                ok = False
                issues.append("negative_mape")
            if r2 is not None and not (-10.0 <= r2 <= 1.0):
                ok = False
                issues.append("r2_out_of_range")
            if not ok:
                result.mismatches.append(f"{label}[{key}]: {','.join(issues)}")
                result.overall_ok = False
            rows.append({
                "key": key, "n": n, "mae_wh": mae, "rmse_wh": rmse,
                "bias_wh": m.get("bias_wh"), "mape_pct": mape, "r2": r2,
                "ok": ok,
            })
        return rows

    result.group1_buckets = _validate_buckets(fq.get("group1") or {}, "group1")
    result.group2_buckets = _validate_buckets(fq.get("group2") or {}, "group2")
    result.group3_buckets = _validate_buckets(fq.get("group3") or {}, "group3")
    result.group1_bucket_count = len(result.group1_buckets)
    result.group2_bucket_count = len(result.group2_buckets)
    result.group3_bucket_count = len(result.group3_buckets)
    return result


class ForecastQualityBucketTable(Static):
    """DataTable: bucket key | n | Bias | MAE | RMSE | MAPE% | R² | ✓/✗."""

    DEFAULT_CSS = """
    ForecastQualityBucketTable { height: auto; }
    ForecastQualityBucketTable DataTable { height: 14; }
    """

    def __init__(self, title: str, rows: list[dict]) -> None:
        """Initialise with a group title and pre-computed bucket rows.

        Args:
            title: Human-readable group label (e.g. "Group 1 — Intensity").
            rows: List of dicts from _validate_buckets() with key, n, metrics, ok.
        """
        super().__init__()
        self._title = title
        self._rows = rows

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield Static(f"  {self._title}", markup=False)
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per bucket."""
        table = self.query_one(DataTable)
        table.add_columns("Bucket", "n", "Bias Wh", "MAE Wh", "RMSE Wh", "MAPE %", "R²", "")
        for row in self._rows:
            fmt = lambda v: f"{v:.1f}" if v is not None else "—"
            fmt4 = lambda v: f"{v:.4f}" if v is not None else "—"
            ok_style = "" if row["ok"] else "red"
            table.add_row(
                Text(str(row["key"]),     style="cyan"),
                Text(str(row["n"])),
                Text(fmt(row["bias_wh"]),  style=ok_style),
                Text(fmt(row["mae_wh"]),   style=ok_style),
                Text(fmt(row["rmse_wh"]),  style=ok_style),
                Text(fmt(row["mape_pct"]), style=ok_style),
                Text(fmt4(row["r2"])),
                Text("✓" if row["ok"] else "✗", style="green" if row["ok"] else "red"),
            )


class ForecastQualityCheckWidget(Static):
    """Collapsible forecast quality deep-check: sunrise/sunset, per-group bucket tables."""

    DEFAULT_CSS = "ForecastQualityCheckWidget { height: auto; }"

    def __init__(self, fqr: ForecastQualityCheckResult) -> None:
        """Initialise with the pre-computed forecast quality check result.

        Args:
            fqr: Result of check_forecast_quality() for one coordinator.
        """
        super().__init__()
        self._fqr = fqr

    def compose(self) -> ComposeResult:
        """Render quality store summary and per-group bucket tables as a collapsible block."""
        fqr = self._fqr

        if fqr.skipped:
            yield Static(f"  ⚠  forecast_quality   SKIP   {fqr.skip_reason}")
            return

        color = "green" if fqr.overall_ok else "red"
        mark = "✓" if fqr.overall_ok else "✗"
        status = "PASS" if fqr.overall_ok else "FAIL"
        title = (
            f"[{color}]{mark}[/{color}]  forecast_quality   [{color}]{status}[/{color}]"
            f"   G1={fqr.group1_bucket_count}b"
            f"  G2={fqr.group2_bucket_count}b"
            f"  G3={fqr.group3_bucket_count}b"
            f"  pending={fqr.group3_pending_count}"
        )

        with Collapsible(title=title, collapsed=True):
            sunrise_str = fqr.sunrise_utc or "—"
            sunset_str  = fqr.sunset_utc  or "—"
            yield Static(
                f"  Sunrise UTC: {sunrise_str}\n"
                f"  Sunset  UTC: {sunset_str}\n"
                f"  Group3 pending: {fqr.group3_pending_count}",
                markup=False,
            )
            if fqr.mismatches:
                yield Static(
                    "  [red]Mismatches:[/red] " + ", ".join(fqr.mismatches),
                    markup=True,
                )
            if fqr.group1_buckets:
                sorted_g1 = sorted(fqr.group1_buckets, key=lambda r: int(r["key"]))
                yield ForecastQualityBucketTable("Group 1 — Intensity (forecast Wh bin)", sorted_g1)
            if fqr.group2_buckets:
                sorted_g2 = sorted(fqr.group2_buckets, key=lambda r: int(r["key"]))
                yield ForecastQualityBucketTable("Group 2 — Solar-Day Position (#1=sunrise, #N=sunset)", sorted_g2)
            if fqr.group3_buckets:
                sorted_g3 = sorted(fqr.group3_buckets, key=lambda r: int(r["key"]))
                yield ForecastQualityBucketTable("Group 3 — Forecast Horizon (d0=today … d6=6d ahead)", sorted_g3)


# ---------------------------------------------------------------------------
# Monthly bill deep check
# ---------------------------------------------------------------------------


@dataclass
class MonthlyBillCheckResult:
    """Result of the monthly bill deep-check: total verification and per-slot cost."""

    skipped: bool = False
    skip_reason: str = ""
    slot_count: int = 0
    carry_eur: float = 0.0
    yday_to_now_eur: float = 0.0
    total_month_eur: float = 0.0
    month_str: str = ""
    previous_month_str: str = ""
    previous_month_eur: float = 0.0
    pricing_mismatch_count: int = 0
    energy_mismatch_count: int = 0
    grid_history_sample_count: int = 0
    grid_history_first_sample: str = ""
    grid_history_last_sample: str = ""
    mismatches: list[str] = field(default_factory=list)
    slot_rows: list[dict] = field(default_factory=list)
    overall_ok: bool = True


def check_monthly_bill(snap: Snapshot) -> MonthlyBillCheckResult:
    """Validate monthly bill totals, per-slot cost formulas, pricing, and energy alignment.

    Cross-checks (every BillSlot is verified, including zero-power slots):
      * carry + yday_to_now == total_month_eur
      * sum(slot.net_cost_eur) == yday_to_now_eur
      * per-slot net_cost_eur == imported_kwh*buy − exported_kwh*sell
        (no floor on sell — negative prices honoured)
      * per-slot imported_kwh / exported_kwh reconstructed from the upstream
        ``inputs.grid_power_history`` samples within [slot.start, slot.end).
      * each bill slot's buy/sell prices match the overlapping
        ``pipeline.pricing`` slot, verifying PriceSeries was applied faithfully.

    Builds a dense ``slot_rows`` list — one entry per BillSlot — so widgets can
    render every slot (including those with zero imported/exported) without
    hiding gaps in the GridPowerHistory.

    Args:
        snap: Coordinator snapshot containing pipeline.monthly_bill, pipeline.pricing,
            and inputs.grid_power_history.

    Returns:
        MonthlyBillCheckResult with aggregation cross-checks, dense per-slot
        rows, and overall pass/fail.
    """
    result = MonthlyBillCheckResult()

    mb = snap.pipeline.get("monthly_bill")
    if not mb:
        result.skipped = True
        result.skip_reason = "pipeline.monthly_bill is null (no grid power history yet)"
        return result

    result.slot_count = mb.get("slot_count", 0)
    result.carry_eur = mb.get("carry_eur", 0.0)
    result.yday_to_now_eur = mb.get("yday_to_now_eur", 0.0)
    result.total_month_eur = mb.get("total_month_eur", 0.0)
    result.month_str = mb.get("month_str", "")
    result.previous_month_str = mb.get("previous_month_str", "")
    result.previous_month_eur = mb.get("previous_month_eur", 0.0)

    if abs((result.carry_eur + result.yday_to_now_eur) - result.total_month_eur) > 1e-4:
        result.mismatches.append("total_mismatch")
        result.overall_ok = False

    slots = mb.get("slots") or []
    slot_sum = sum(s.get("net_cost_eur", 0.0) for s in slots)
    if abs(slot_sum - result.yday_to_now_eur) > 1e-4:
        result.mismatches.append("yday_sum_mismatch")
        result.overall_ok = False

    pricing = snap.pipeline.get("pricing") or {}
    pricing_slots = pricing.get("slots") or []
    parsed_pricing: list[tuple[datetime, float, float]] = []
    for p in pricing_slots:
        try:
            parsed_pricing.append((
                datetime.fromisoformat(p.get("start", "")),
                float(p.get("buy", 0.0)),
                float(p.get("sell", 0.0)),
            ))
        except (ValueError, TypeError):
            continue
    parsed_pricing.sort(key=lambda x: x[0])

    history = snap.inputs.get("grid_power_history") or {}
    raw_samples = history.get("samples") or []
    parsed_samples: list[tuple[datetime, float]] = []
    for s in raw_samples:
        try:
            parsed_samples.append((
                datetime.fromisoformat(s.get("timestamp", "")),
                float(s.get("power_kw", 0.0)),
            ))
        except (ValueError, TypeError):
            continue
    parsed_samples.sort(key=lambda x: x[0])
    result.grid_history_sample_count = len(parsed_samples)
    if parsed_samples:
        result.grid_history_first_sample = parsed_samples[0][0].isoformat()
        result.grid_history_last_sample = parsed_samples[-1][0].isoformat()

    slot_formula_errors = 0
    pricing_mismatches = 0
    energy_mismatches = 0

    for s in slots:
        try:
            bs_start = datetime.fromisoformat(s.get("start", ""))
            bs_end = datetime.fromisoformat(s.get("end", ""))
        except (ValueError, TypeError):
            continue

        imp = s.get("imported_kwh", 0.0)
        exp = s.get("exported_kwh", 0.0)
        buy = s.get("buy_eur_kwh", 0.0)
        sell = s.get("sell_eur_kwh", 0.0)
        actual_cost = s.get("net_cost_eur", 0.0)
        expected_cost = imp * buy - exp * sell
        cost_ok = abs(expected_cost - actual_cost) <= 1e-4
        if not cost_ok:
            slot_formula_errors += 1

        match_pricing = None
        for i, (ps_start, _, _) in enumerate(parsed_pricing):
            next_start = parsed_pricing[i + 1][0] if i + 1 < len(parsed_pricing) else None
            if ps_start <= bs_start and (next_start is None or bs_start < next_start):
                match_pricing = parsed_pricing[i]
                break
        pricing_ok = True
        if match_pricing is not None:
            _, exp_buy, exp_sell = match_pricing
            if abs(exp_buy - buy) > 1e-4 or abs(exp_sell - sell) > 1e-4:
                pricing_mismatches += 1
                pricing_ok = False

        samples_in_slot = [
            kw for (ts, kw) in parsed_samples if bs_start <= ts < bs_end
        ]
        sample_count = len(samples_in_slot)
        duration_h = (bs_end - bs_start).total_seconds() / 3600.0
        if samples_in_slot:
            avg_kw = sum(samples_in_slot) / sample_count
            net_kwh = avg_kw * duration_h
            exp_imp = max(0.0, net_kwh)
            exp_exp = max(0.0, -net_kwh)
        else:
            exp_imp = 0.0
            exp_exp = 0.0
        energy_ok = abs(exp_imp - imp) <= 1e-3 and abs(exp_exp - exp) <= 1e-3
        if not energy_ok:
            energy_mismatches += 1

        result.slot_rows.append({
            "start": s.get("start", ""),
            "end": s.get("end", ""),
            "imported_kwh": imp,
            "exported_kwh": exp,
            "expected_imported_kwh": exp_imp,
            "expected_exported_kwh": exp_exp,
            "buy_eur_kwh": buy,
            "sell_eur_kwh": sell,
            "net_cost_eur": actual_cost,
            "expected_net_cost_eur": expected_cost,
            "sample_count": sample_count,
            "cost_ok": cost_ok,
            "pricing_ok": pricing_ok,
            "energy_ok": energy_ok,
        })

    if slot_formula_errors:
        result.mismatches.append(f"{slot_formula_errors}_slot_formula_mismatch")
        result.overall_ok = False
    result.pricing_mismatch_count = pricing_mismatches
    if pricing_mismatches:
        result.mismatches.append(f"{pricing_mismatches}_pricing_mismatch")
        result.overall_ok = False
    result.energy_mismatch_count = energy_mismatches
    if energy_mismatches:
        result.mismatches.append(f"{energy_mismatches}_energy_mismatch")
        result.overall_ok = False

    return result


class MonthlyBillSlotsTable(Static):
    """DataTable: Time | Imp kWh | Exp kWh | Buy € | Sell € | Net € | Samples | ✓/✗.

    Renders every slot — including those with zero imported/exported — so
    gaps in the source GridPowerHistory are visible to the reviewer instead
    of being silently dropped from the breakdown.
    """

    DEFAULT_CSS = """
    MonthlyBillSlotsTable { height: auto; }
    MonthlyBillSlotsTable DataTable { height: 22; }
    """

    def __init__(self, mb: MonthlyBillCheckResult) -> None:
        """Initialise with the monthly bill check result.

        Args:
            mb: Result of check_monthly_bill() containing the dense slot_rows.
        """
        super().__init__()
        self._mb = mb

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per BillSlot, date-separated."""
        table = self.query_one(DataTable)
        table.add_columns(
            "Time", "Imp kWh", "Exp kWh", "Buy €", "Sell €", "Net €", "Samp", "",
        )
        dim = "dim"
        prev_date = None

        for row in self._mb.slot_rows:
            try:
                dt = datetime.fromisoformat(row["start"]).astimezone(timezone.utc)
                time_str = dt.strftime("%H:%M")
                cur_date = dt.date()
            except (ValueError, AttributeError):
                time_str = row["start"]
                cur_date = None

            if cur_date is not None and cur_date != prev_date:
                if prev_date is not None:
                    table.add_row(*[Text("─", style=dim)] * 8)
                table.add_row(Text(str(cur_date), style="bold"), *[""] * 7)
                prev_date = cur_date

            imp = row["imported_kwh"]
            exp = row["exported_kwh"]
            buy = row["buy_eur_kwh"]
            sell = row["sell_eur_kwh"]
            net = row["net_cost_eur"]
            samples = row["sample_count"]
            ok = row["cost_ok"] and row["pricing_ok"] and row["energy_ok"]

            zero_imp_exp = imp == 0.0 and exp == 0.0
            base_style = dim if zero_imp_exp else ""
            samples_style = "red" if samples == 0 else dim

            table.add_row(
                Text(time_str, style="cyan"),
                Text(f"{imp:.4f}", style=base_style),
                Text(f"{exp:.4f}", style=base_style),
                Text(f"{buy:.4f}", style=dim),
                Text(f"{sell:.4f}", style=dim),
                Text(f"{net:.4f}", style=base_style),
                Text(str(samples), style=samples_style),
                Text("✓" if ok else "✗", style="green" if ok else "red"),
            )


class MonthlyBillCheckWidget(Static):
    """Collapsible monthly bill deep-check: total verification and bill breakdown."""

    DEFAULT_CSS = "MonthlyBillCheckWidget { height: auto; }"

    def __init__(self, mb: MonthlyBillCheckResult) -> None:
        """Initialise with the pre-computed monthly bill check result.

        Args:
            mb: Result of check_monthly_bill() for one coordinator.
        """
        super().__init__()
        self._mb = mb

    def compose(self) -> ComposeResult:
        """Render bill summary, slot table, and mismatch status."""
        mb = self._mb

        if mb.skipped:
            yield Static(f"  ⚠  monthly_bill_check   SKIP   {mb.skip_reason}")
            return

        color = "green" if mb.overall_ok else "red"
        mark = "✓" if mb.overall_ok else "✗"
        status = "PASS" if mb.overall_ok else "FAIL"
        title = (
            f"[{color}]{mark}[/{color}]  monthly_bill_check   [{color}]{status}[/{color}]"
            f"   {mb.slot_count} slots  carry={mb.carry_eur:.4f}€"
            f"  yday_to_now={mb.yday_to_now_eur:.4f}€  total={mb.total_month_eur:.4f}€"
        )

        with Collapsible(title=title, collapsed=True):
            mismatches_str = ", ".join(mb.mismatches) if mb.mismatches else "none"
            prev = (
                f"{mb.previous_month_str}: {mb.previous_month_eur:.4f} EUR"
                if mb.previous_month_str else "—"
            )
            grid_window = (
                f"{mb.grid_history_first_sample} → {mb.grid_history_last_sample}"
                if mb.grid_history_sample_count else "—"
            )
            yield Static(
                f"  Month: {mb.month_str}\n"
                f"  Carry (month start → yday start): {mb.carry_eur:.4f} EUR\n"
                f"  Live (since carry boundary): {mb.yday_to_now_eur:.4f} EUR\n"
                f"  Total month bill: {mb.total_month_eur:.4f} EUR\n"
                f"  Previous month: {prev}\n"
                f"  Slots: {mb.slot_count}\n"
                f"  Grid power samples: {mb.grid_history_sample_count}  ({grid_window})\n"
                f"  Pricing mismatches: {mb.pricing_mismatch_count}\n"
                f"  Energy reconstruction mismatches: {mb.energy_mismatch_count}\n"
                f"  Mismatches: {mismatches_str}",
                markup=True,
            )
            with Collapsible(title="Slots", collapsed=False):
                yield MonthlyBillSlotsTable(mb)


# Categories handled by deep-check widgets; excluded from the plain validator display.
_DEEP_CATS: frozenset[str] = frozenset({
    "forecast", "pricing", "calculation", "schedule", "battery",
    "observed_generation", "forecast_accuracy", "charging_profile", "base_load",
    "battery_runtime", "household_consumption", "profitability", "forecast_quality",
    "monthly_bill",
})


class PricingSlotsTable(Static):
    """DataTable: Time | Spot | Exp Buy | Act Buy | Exp Sell | Act Sell | ✓/✗."""

    DEFAULT_CSS = """
    PricingSlotsTable { height: auto; }
    PricingSlotsTable DataTable { height: 22; }
    """

    def __init__(self, pc: PricingCheckResult) -> None:
        """Initialise with the pricing check result.

        Args:
            pc: Result of check_pricing() containing per-slot formula comparisons.
        """
        super().__init__()
        self._pc = pc

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per pricing slot."""
        table = self.query_one(DataTable)
        table.add_columns("Time", "Spot", "Exp Buy", "Act Buy", "Exp Sell", "Act Sell", "")
        dim = "dim"

        for row in self._pc.slot_rows:
            try:
                dt = datetime.fromisoformat(row["start"]).astimezone(timezone.utc)
                time_str = dt.strftime("%m-%d %H:%M")
            except (ValueError, AttributeError):
                time_str = row["start"]

            ok = row["ok"]
            bad_style = "red" if not ok else ""
            table.add_row(
                Text(time_str, style="cyan"),
                Text(f"{row['spot']:.4f}"),
                Text(f"{row['exp_buy']:.4f}", style=dim),
                Text(f"{row['act_buy']:.4f}", style=bad_style),
                Text(f"{row['exp_sell']:.4f}", style=dim),
                Text(f"{row['act_sell']:.4f}", style=bad_style),
                Text("✓" if ok else "✗", style="green" if ok else "red"),
            )


class PricingCheckWidget(Static):
    """Collapsible pricing deep-check: formula verification per slot + tariff config."""

    DEFAULT_CSS = "PricingCheckWidget { height: auto; }"

    def __init__(self, pc: PricingCheckResult) -> None:
        """Initialise with the pre-computed pricing check result.

        Args:
            pc: Result of check_pricing() for one coordinator.
        """
        super().__init__()
        self._pc = pc

    def compose(self) -> ComposeResult:
        """Render Slots sub-Collapsible and optional Tariff Config sub-Collapsible."""
        pc = self._pc

        if pc.skipped:
            yield Static(f"  ⚠  pricing_check   SKIP   {pc.skip_reason}")
            return

        color = "green" if pc.overall_ok else "red"
        mark = "✓" if pc.overall_ok else "✗"
        status = "PASS" if pc.overall_ok else "FAIL"
        neg = f"  {pc.negative_sell_count} negative-sell" if pc.negative_sell_count else ""
        res_min = pc.resolution_s // 60
        title = (
            f"[{color}]{mark}[/{color}]  pricing_check   [{color}]{status}[/{color}]"
            f"   {pc.module_slot_count} slots  res={res_min}min{neg}"
        )

        with Collapsible(title=title, collapsed=True):
            with Collapsible(title="Slots", collapsed=False):
                yield PricingSlotsTable(pc)
            with Collapsible(title="Tariff Config", collapsed=True):
                lines = "\n".join(f"  {k}: {v}" for k, v in pc.tariff_config.items())
                yield Static(lines)


class CalculationSlotsTable(Static):
    """DataTable: Time | Sell (€) | Exp Sell? | Act Sell? | Solar (kWh) | Notes | ✓/✗."""

    DEFAULT_CSS = """
    CalculationSlotsTable { height: auto; }
    CalculationSlotsTable DataTable { height: 22; }
    """

    def __init__(self, cc: CalculationCheckResult) -> None:
        """Initialise with the calculation check result.

        Args:
            cc: Result of check_calculation() containing per-slot sell_allowed comparisons.
        """
        super().__init__()
        self._cc = cc

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per calculation slot, date-separated."""
        table = self.query_one(DataTable)
        table.add_columns("Time", "Sell (€)", "Exp Sell?", "Act Sell?", "Solar (kWh)", "Notes", "")
        dim = "dim"
        prev_date = None

        for row in self._cc.slot_rows:
            try:
                dt = datetime.fromisoformat(row["start"]).astimezone(timezone.utc)
                time_str = dt.strftime("%H:%M")
                cur_date = dt.date()
            except (ValueError, AttributeError):
                time_str = row["start"]
                cur_date = None

            if cur_date is not None and cur_date != prev_date:
                if prev_date is not None:
                    table.add_row(*[Text("─", style=dim)] * 7)
                table.add_row(Text(str(cur_date), style="bold"), *[""] * 6)
                prev_date = cur_date

            ok = row["ok"]
            sp = row["sell_price"]
            sp_str = f"{sp:.4f}" if sp is not None else "—"
            exp_str = "✓" if row["sell_exp"] else "✗"
            act_str = "✓" if row["sell_act"] else "✗"
            act_style = "" if ok else "red"
            solar = row["solar_kwh"]
            notes_str = ", ".join(row["notes"])[:30] if row["notes"] else ""

            table.add_row(
                Text(time_str, style="cyan"),
                Text(sp_str, style=dim if sp is None or sp == 0.0 else ""),
                Text(exp_str, style=dim if not row["sell_exp"] else ""),
                Text(act_str, style=act_style),
                Text(f"{solar:.4f}", style=dim if solar == 0 else ""),
                Text(notes_str, style=dim),
                Text("✓" if ok else "✗", style="green" if ok else "red"),
            )


class CalculationCheckWidget(Static):
    """Collapsible calculation deep-check: sell_allowed logic + lockout windows."""

    DEFAULT_CSS = "CalculationCheckWidget { height: auto; }"

    def __init__(self, cc: CalculationCheckResult) -> None:
        """Initialise with the pre-computed calculation check result.

        Args:
            cc: Result of check_calculation() for one coordinator.
        """
        super().__init__()
        self._cc = cc

    def compose(self) -> ComposeResult:
        """Render Slots sub-Collapsible; list lockout windows when present."""
        cc = self._cc

        if cc.skipped:
            yield Static(f"  ⚠  calculation_check   SKIP   {cc.skip_reason}")
            return

        color = "green" if cc.overall_ok else "red"
        mark = "✓" if cc.overall_ok else "✗"
        status = "PASS" if cc.overall_ok else "FAIL"
        n_lock = len(cc.lockout_windows)
        neg = f"  neg_sale={cc.total_negative_sale_kwh:.3f}kWh" if cc.total_negative_sale_kwh else ""
        title = (
            f"[{color}]{mark}[/{color}]  calculation_check   [{color}]{status}[/{color}]"
            f"   {cc.module_slot_count} slots  {n_lock} lockout window(s){neg}"
        )

        with Collapsible(title=title, collapsed=True):
            if cc.total_negative_sale_kwh > 0:
                neg_ok = "neg_sale_sum_mismatch" not in cc.mismatches
                neg_style = "green" if neg_ok else "red"
                yield Static(
                    f"  neg_sale: computed [{neg_style}]{cc.computed_neg_sale_kwh:.4f}[/{neg_style}]kWh"
                    f"  declared {cc.total_negative_sale_kwh:.4f}kWh",
                    markup=True,
                )
            with Collapsible(title="Slots", collapsed=False):
                yield CalculationSlotsTable(cc)
            if cc.lockout_windows:
                lines = "  Lockout windows:\n" + "\n".join(
                    f"    {w['start']}  →  {w['end']}" for w in cc.lockout_windows
                )
                yield Static(lines)


class ScheduleSlotsTable(Static):
    """DataTable: Time | Action | Power (kW) | Profit (€) | Reason."""

    DEFAULT_CSS = """
    ScheduleSlotsTable { height: auto; }
    ScheduleSlotsTable DataTable { height: 18; }
    """

    def __init__(self, sc: ScheduleCheckResult) -> None:
        """Initialise with the schedule check result.

        Args:
            sc: Result of check_schedule() containing per-slot action and profit data.
        """
        super().__init__()
        self._sc = sc

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per schedule slot, date-separated."""
        table = self.query_one(DataTable)
        table.add_columns("Time", "Action", "Power (kW)", "Profit (€)", "Reason", "")
        dim = "dim"
        prev_date = None

        for row in self._sc.slot_rows:
            try:
                dt = datetime.fromisoformat(row["start"]).astimezone(timezone.utc)
                time_str = dt.strftime("%H:%M")
                cur_date = dt.date()
            except (ValueError, AttributeError):
                time_str = row["start"]
                cur_date = None

            if cur_date is not None and cur_date != prev_date:
                if prev_date is not None:
                    table.add_row(*[Text("─", style=dim)] * 6)
                table.add_row(Text(str(cur_date), style="bold"), *[""] * 5)
                prev_date = cur_date

            is_current = row["is_current"]
            ok = row["ok"]
            profit = row["profit_eur"]
            profit_style = "green" if profit > 0 else ("red" if profit < 0 else dim)
            time_style = "bold cyan" if is_current else "cyan"
            prefix = "▶ " if is_current else "  "

            table.add_row(
                Text(prefix + time_str, style=time_style),
                Text(row["action"], style="bold" if is_current else ""),
                Text(f"{row['power_kw']:.2f}", style=dim if row["power_kw"] == 0 else ""),
                Text(f"{profit:+.4f}", style=profit_style),
                Text(row["reason"][:40] if row["reason"] else "—", style=dim),
                Text("✗" if not ok else "", style="red"),
            )


class ScheduleCheckWidget(Static):
    """Collapsible schedule deep-check: per-slot actions, power, and profit."""

    DEFAULT_CSS = "ScheduleCheckWidget { height: auto; }"

    def __init__(self, sc: ScheduleCheckResult) -> None:
        """Initialise with the pre-computed schedule check result.

        Args:
            sc: Result of check_schedule() for one coordinator.
        """
        super().__init__()
        self._sc = sc

    def compose(self) -> ComposeResult:
        """Render Slots sub-Collapsible with schedule data."""
        sc = self._sc

        if sc.skipped:
            yield Static(f"  ⚠  schedule_check   SKIP   {sc.skip_reason}")
            return

        color = "green" if sc.overall_ok else "red"
        mark = "✓" if sc.overall_ok else "✗"
        status = "PASS" if sc.overall_ok else "FAIL"
        profit_str = f"{sc.total_expected_profit_eur:+.4f}"
        title = (
            f"[{color}]{mark}[/{color}]  schedule_check   [{color}]{status}[/{color}]"
            f"   {sc.slot_count} slots  profit={profit_str}€"
        )

        with Collapsible(title=title, collapsed=True):
            profit_match = "profit_sum" not in sc.mismatches
            profit_style = "green" if profit_match else "red"
            yield Static(
                f"  profit: computed [{profit_style}]{sc.computed_profit_sum:+.4f}[/{profit_style}]€"
                f"  declared {sc.total_expected_profit_eur:+.4f}€",
                markup=True,
            )
            with Collapsible(title="Slots", collapsed=False):
                yield ScheduleSlotsTable(sc)


class _BatteryDataTable(Static):
    """DataTable of battery field/value/status rows."""

    DEFAULT_CSS = """
    _BatteryDataTable { height: auto; }
    _BatteryDataTable DataTable { height: 12; }
    """

    def __init__(self, bc: BatteryCheckResult) -> None:
        """Initialise with the battery check result.

        Args:
            bc: Result of check_battery() containing observed battery values.
        """
        super().__init__()
        self._bc = bc

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False)

    def on_mount(self) -> None:
        """Populate the DataTable with battery state, status, and degradation rows."""
        bc = self._bc
        table = self.query_one(DataTable)
        table.add_columns("Field", "Value", "")
        dim = "dim"

        def _fv(v: float | None, fmt: str = ".3f") -> Text:
            return Text(f"{v:{fmt}}") if v is not None else Text("—", style=dim)

        # soc is 0.0–1.0 fraction; display as percentage
        soc_ok = bc.soc is None or 0.0 <= bc.soc <= 1.0
        table.add_row(
            Text("SOC"),
            Text(f"{bc.soc:.1%}") if bc.soc is not None else Text("—", style=dim),
            Text("✓" if soc_ok else "✗", style="green" if soc_ok else "red"),
        )
        table.add_row(Text("Battery power (kW)"), _fv(bc.power_kw), Text(""))
        table.add_row(Text("Grid power (kW)"), _fv(bc.grid_power_kw), Text(""))
        table.add_row(Text("Est. capacity (kWh)"), _fv(bc.estimated_capacity_kwh), Text(""))

        if bc.total_capacity_kwh is not None:
            table.add_row(Text("Total capacity (kWh)"), _fv(bc.total_capacity_kwh), Text(""))
        if bc.max_charge_power_kw is not None:
            table.add_row(Text("Max charge (kW)"), _fv(bc.max_charge_power_kw), Text(""))
        if bc.max_discharge_power_kw is not None:
            table.add_row(Text("Max discharge (kW)"), _fv(bc.max_discharge_power_kw), Text(""))

        if bc.remaining_capacity_kwh is not None:
            rem_ok = "remaining_capacity_inconsistent" not in bc.mismatches
            exp_str = (
                f"exp {bc.expected_remaining_capacity_kwh:.3f} → "
                if bc.expected_remaining_capacity_kwh is not None else ""
            )
            table.add_row(
                Text("Remaining capacity (kWh)"),
                Text(f"{exp_str}{bc.remaining_capacity_kwh:.3f}", style="" if rem_ok else "red"),
                Text("✓" if rem_ok else "✗", style="green" if rem_ok else "red"),
            )

        deg_ok = not (
            bc.estimated_capacity_kwh
            and (bc.degradation_cost_per_kwh is None or bc.degradation_cost_per_kwh <= 0)
        )
        table.add_row(
            Text("Degradation cost (€/kWh)"),
            _fv(bc.degradation_cost_per_kwh, ".5f"),
            Text("✓" if deg_ok else "✗", style="green" if deg_ok else "red"),
        )


class BatteryCheckWidget(Static):
    """Collapsible battery deep-check: SOC, power, capacity, and degradation cost."""

    DEFAULT_CSS = "BatteryCheckWidget { height: auto; }"

    def __init__(self, bc: BatteryCheckResult) -> None:
        """Initialise with the pre-computed battery check result.

        Args:
            bc: Result of check_battery() for one coordinator.
        """
        super().__init__()
        self._bc = bc

    def compose(self) -> ComposeResult:
        """Render a simple data table with battery state and sanity checks."""
        bc = self._bc

        if bc.skipped:
            yield Static(f"  ⚠  battery_check   SKIP   {bc.skip_reason}")
            return

        color = "green" if bc.overall_ok else "red"
        mark = "✓" if bc.overall_ok else "✗"
        status = "PASS" if bc.overall_ok else "FAIL"
        soc_str = f"{bc.soc:.1%}" if bc.soc is not None else "—"
        title = (
            f"[{color}]{mark}[/{color}]  battery_check   [{color}]{status}[/{color}]"
            f"   SOC={soc_str}"
        )

        with Collapsible(title=title, collapsed=True):
            yield _BatteryDataTable(bc)


class IntegrationCheckApp(App):
    """Textual TUI running in inline mode for the sunSale integration check."""

    BINDINGS = [("q", "quit", "Quit")]
    CSS = """
    Screen { height: auto; }
    Collapsible { height: auto; }
    ForecastCheckWidget { height: auto; }
    ForecastSlotsTable DataTable { height: 25; }
    ForecastSummaryTable DataTable { height: 12; }
    PricingCheckWidget { height: auto; }
    PricingSlotsTable DataTable { height: 22; }
    CalculationCheckWidget { height: auto; }
    CalculationSlotsTable DataTable { height: 22; }
    ScheduleCheckWidget { height: auto; }
    ScheduleSlotsTable DataTable { height: 18; }
    BatteryCheckWidget { height: auto; }
    _BatteryDataTable DataTable { height: 12; }
    ObservedGenerationCheckWidget { height: auto; }
    ObservedGenerationSlotsTable DataTable { height: 18; }
    ForecastAccuracyCheckWidget { height: auto; }
    ForecastAccuracySlotsTable DataTable { height: 18; }
    ChargingProfileCheckWidget { height: auto; }
    ChargingProfileSlotsTable DataTable { height: 15; }
    BaseLoadCheckWidget { height: auto; }
    BaseLoadSlotsTable DataTable { height: 15; }
    BatteryRuntimeCheckWidget { height: auto; }
    HouseholdConsumptionCheckWidget { height: auto; }
    ProfitabilityCheckWidget { height: auto; }
    ForecastQualityCheckWidget { height: auto; }
    ForecastQualityBucketTable DataTable { height: 14; }
    MonthlyBillCheckWidget { height: auto; }
    """

    def __init__(
        self,
        report: list[tuple[Snapshot, list[CheckResult]]],
        forecast_results: dict[str, ForecastCheckResult],
        pricing_results: dict[str, PricingCheckResult],
        calculation_results: dict[str, CalculationCheckResult],
        schedule_results: dict[str, ScheduleCheckResult],
        battery_results: dict[str, BatteryCheckResult],
        observed_gen_results: dict[str, ObservedGenerationCheckResult],
        forecast_acc_results: dict[str, ForecastAccuracyCheckResult],
        charging_profile_results: dict[str, ChargingProfileCheckResult],
        base_load_results: dict[str, BaseLoadCheckResult],
        battery_runtime_results: dict[str, BatteryRuntimeCheckResult],
        household_consumption_results: dict[str, HouseholdConsumptionCheckResult],
        profitability_results: dict[str, ProfitabilityCheckResult],
        forecast_quality_results: dict[str, ForecastQualityCheckResult],
        monthly_bill_results: dict[str, MonthlyBillCheckResult],
    ) -> None:
        """Initialise with validator report and all deep-check results.

        Args:
            report: Per-snapshot check results from run_checks().
            forecast_results: Per-entry solar forecast deep-check results.
            pricing_results: Per-entry pricing deep-check results.
            calculation_results: Per-entry calculation deep-check results.
            schedule_results: Per-entry schedule deep-check results.
            battery_results: Per-entry battery state/status deep-check results.
            observed_gen_results: Per-entry observed generation deep-check results.
            forecast_acc_results: Per-entry forecast accuracy deep-check results.
            charging_profile_results: Per-entry charging profile deep-check results.
            base_load_results: Per-entry base load profile deep-check results.
            battery_runtime_results: Per-entry battery runtime deep-check results.
            household_consumption_results: Per-entry household consumption deep-check results.
            profitability_results: Per-entry profitability score deep-check results.
            forecast_quality_results: Per-entry forecast quality EMA bucket deep-check results.
            monthly_bill_results: Per-entry monthly electricity bill deep-check results.
        """
        super().__init__()
        self._report = report
        self._forecast_results = forecast_results
        self._pricing_results = pricing_results
        self._calculation_results = calculation_results
        self._schedule_results = schedule_results
        self._battery_results = battery_results
        self._observed_gen_results = observed_gen_results
        self._forecast_acc_results = forecast_acc_results
        self._charging_profile_results = charging_profile_results
        self._base_load_results = base_load_results
        self._battery_runtime_results = battery_runtime_results
        self._household_consumption_results = household_consumption_results
        self._profitability_results = profitability_results
        self._forecast_quality_results = forecast_quality_results
        self._monthly_bill_results = monthly_bill_results
        self.exit_code = 0

    def _all_deep(self) -> list:
        """Return every deep-check result across all modules and entries.

        Returns:
            Flat list of result objects each having skipped and overall_ok attributes.
        """
        return (
            list(self._forecast_results.values())
            + list(self._pricing_results.values())
            + list(self._calculation_results.values())
            + list(self._schedule_results.values())
            + list(self._battery_results.values())
            + list(self._observed_gen_results.values())
            + list(self._forecast_acc_results.values())
            + list(self._charging_profile_results.values())
            + list(self._base_load_results.values())
            + list(self._battery_runtime_results.values())
            + list(self._household_consumption_results.values())
            + list(self._profitability_results.values())
            + list(self._forecast_quality_results.values())
            + list(self._monthly_bill_results.values())
        )

    def compose(self) -> ComposeResult:
        """Yield validator lines for non-deep categories, then one deep widget per module."""
        for snap, results in self._report:
            cur_cat: str | None = None
            for res in results:
                if res.category in _DEEP_CATS:
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

            eid = snap.entry_id
            for label, widget_cls, results_map in (
                ("forecast",                ForecastCheckWidget,                self._forecast_results),
                ("pricing",                 PricingCheckWidget,                 self._pricing_results),
                ("calculation",             CalculationCheckWidget,             self._calculation_results),
                ("schedule",                ScheduleCheckWidget,                self._schedule_results),
                ("battery",                 BatteryCheckWidget,                 self._battery_results),
                ("observed_generation",     ObservedGenerationCheckWidget,      self._observed_gen_results),
                ("forecast_accuracy",       ForecastAccuracyCheckWidget,        self._forecast_acc_results),
                ("charging_profile",        ChargingProfileCheckWidget,         self._charging_profile_results),
                ("base_load",               BaseLoadCheckWidget,                self._base_load_results),
                ("battery_runtime",         BatteryRuntimeCheckWidget,          self._battery_runtime_results),
                ("household_consumption",   HouseholdConsumptionCheckWidget,    self._household_consumption_results),
                ("profitability",           ProfitabilityCheckWidget,           self._profitability_results),
                ("forecast_quality",        ForecastQualityCheckWidget,         self._forecast_quality_results),
                ("monthly_bill",            MonthlyBillCheckWidget,             self._monthly_bill_results),
            ):
                r = results_map.get(eid)
                if r is not None:
                    yield Static(f"  [dim][{label}][/dim]", markup=True)
                    yield widget_cls(r)

            yield Static("─" * 60)

        non_deep_total = sum(1 for _, rs in self._report for r in rs if r.category not in _DEEP_CATS)
        non_deep_passed = sum(
            1 for _, rs in self._report for r in rs
            if r.category not in _DEEP_CATS and r.ok
        )
        deep = self._all_deep()
        deep_total = sum(1 for r in deep if not r.skipped)
        deep_passed = sum(1 for r in deep if not r.skipped and r.overall_ok)
        all_total = non_deep_total + deep_total
        all_passed = non_deep_passed + deep_passed
        color = "green" if all_passed == all_total else "red"
        yield Static(
            f"[{color}]{all_passed}/{all_total} checks passed[/{color}]  [dim](q to quit)[/dim]",
            markup=True,
        )
        yield Footer()

    def on_mount(self) -> None:
        """Set exit code based on check results."""
        any_non_deep_failed = any(
            not r.ok for _, rs in self._report for r in rs if r.category not in _DEEP_CATS
        )
        any_deep_failed = any(
            not r.overall_ok and not r.skipped for r in self._all_deep()
        )
        self.exit_code = 1 if (any_non_deep_failed or any_deep_failed) else 0


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

    forecast_results               = {s.entry_id: check_forecast(s)                  for s in snapshots}
    pricing_results                = {s.entry_id: check_pricing(s)                   for s in snapshots}
    calculation_results            = {s.entry_id: check_calculation(s)               for s in snapshots}
    schedule_results               = {s.entry_id: check_schedule(s)                  for s in snapshots}
    battery_results                = {s.entry_id: check_battery(s)                   for s in snapshots}
    observed_gen_results           = {s.entry_id: check_observed_generation(s)       for s in snapshots}
    forecast_acc_results           = {s.entry_id: check_forecast_accuracy(s)         for s in snapshots}
    charging_profile_results       = {s.entry_id: check_charging_profile(s)          for s in snapshots}
    base_load_results              = {s.entry_id: check_base_load(s)                 for s in snapshots}
    battery_runtime_results        = {s.entry_id: check_battery_runtime(s)           for s in snapshots}
    household_consumption_results  = {s.entry_id: check_household_consumption(s)     for s in snapshots}
    profitability_results          = {s.entry_id: check_profitability(s)             for s in snapshots}
    forecast_quality_results       = {s.entry_id: check_forecast_quality(s)          for s in snapshots}
    monthly_bill_results           = {s.entry_id: check_monthly_bill(s)              for s in snapshots}
    app = IntegrationCheckApp(
        report,
        forecast_results, pricing_results, calculation_results,
        schedule_results, battery_results,
        observed_gen_results, forecast_acc_results, charging_profile_results,
        base_load_results, battery_runtime_results,
        household_consumption_results, profitability_results,
        forecast_quality_results,
        monthly_bill_results,
    )
    app.run(inline=True)
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())
