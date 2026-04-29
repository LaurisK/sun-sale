"""Tests for the /api/sun_sale/debug view."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.sun_sale.debug_view import SunSaleDebugView, _coordinator_to_dict
from custom_components.sun_sale.models import (
    Action,
    BatteryState,
    EVChargerState,
    Schedule,
    ScheduleSlot,
    TariffConfig,
    TariffResult,
)

BASE = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_slot(hour_offset: int = 0, action: Action = Action.IDLE) -> ScheduleSlot:
    start = BASE + timedelta(hours=hour_offset)
    return ScheduleSlot(
        start=start,
        end=start + timedelta(hours=1),
        action=action,
        power_kw=0.0,
        expected_soc_after=0.5,
        expected_profit_eur=0.0,
        reason="test",
    )


def make_coordinator(
    *,
    automation_enabled: bool = True,
    include_schedule: bool = True,
    include_ev: bool = False,
    last_dispatched_action: str | None = "idle",
    last_dispatched_at: datetime | None = None,
) -> MagicMock:
    coord = MagicMock()
    coord.automation_enabled = automation_enabled
    coord.last_dispatched_action = last_dispatched_action
    coord.last_dispatched_at = last_dispatched_at or BASE

    tariff_cfg = TariffConfig(
        distribution_fee=0.03,
        tax_rate=0.21,
        markup=0.01,
        sell_distribution_fee=0.02,
        sell_tax_rate=0.0,
        sell_markup=0.005,
    )
    coord.tariff_config = tariff_cfg

    schedule = Schedule(
        slots=[make_slot(0, Action.CHARGE_FROM_GRID), make_slot(1, Action.DISCHARGE_TO_GRID)],
        total_expected_profit_eur=0.42,
        degradation_cost_per_kwh=0.02,
        computed_at=BASE,
    ) if include_schedule else None

    ev_state = EVChargerState(
        is_plugged_in=True,
        soc=0.4,
        target_soc=0.8,
        departure_time=BASE + timedelta(hours=6),
    ) if include_ev else None

    coord.data = {
        "schedule": schedule,
        "ev_schedule": None,
        "tariffs": [
            TariffResult(hour=BASE, spot_price=0.08, buy_price=0.12, sell_price=0.06),
        ],
        "battery_state": BatteryState(soc=0.62, estimated_capacity_kwh=9.8),
        "degradation_cost": 0.018,
        "estimated_capacity": 9.8,
        "prices": [],
        "solar_forecast": [],
        "grid_power_kw": 0.1,
        "ev_state": ev_state,
    }
    return coord


# ---------------------------------------------------------------------------
# _coordinator_to_dict shape tests
# ---------------------------------------------------------------------------

def test_required_top_level_keys_present():
    coord = make_coordinator()
    result = _coordinator_to_dict("entry_abc", coord)

    for key in ("entry_id", "timestamp", "automation_enabled", "inputs",
                "computed", "outputs", "last_dispatched_action", "last_dispatched_at"):
        assert key in result, f"missing top-level key: {key}"


def test_entry_id_matches():
    coord = make_coordinator()
    result = _coordinator_to_dict("my_entry", coord)
    assert result["entry_id"] == "my_entry"


def test_automation_enabled_propagated():
    result_on = _coordinator_to_dict("e", make_coordinator(automation_enabled=True))
    result_off = _coordinator_to_dict("e", make_coordinator(automation_enabled=False))
    assert result_on["automation_enabled"] is True
    assert result_off["automation_enabled"] is False


def test_schedule_slots_serialised():
    coord = make_coordinator(include_schedule=True)
    result = _coordinator_to_dict("e", coord)

    schedule = result["outputs"]["schedule"]
    assert schedule is not None
    assert len(schedule["slots"]) == 2

    slot = schedule["slots"][0]
    for key in ("start", "end", "action", "power_kw", "expected_profit_eur", "reason"):
        assert key in slot, f"missing slot key: {key}"

    assert slot["action"] == "charge_from_grid"
    assert isinstance(slot["start"], str)


def test_schedule_none_when_no_schedule():
    coord = make_coordinator(include_schedule=False)
    result = _coordinator_to_dict("e", coord)
    assert result["outputs"]["schedule"] is None


def test_tariffs_serialised():
    coord = make_coordinator()
    result = _coordinator_to_dict("e", coord)

    tariffs = result["computed"]["tariffs"]
    assert len(tariffs) == 1
    t = tariffs[0]
    for key in ("hour", "spot", "buy", "sell"):
        assert key in t, f"missing tariff key: {key}"
    assert abs(t["buy"] - 0.12) < 1e-9


def test_battery_state_in_inputs():
    coord = make_coordinator()
    result = _coordinator_to_dict("e", coord)
    battery = result["inputs"]["battery"]
    assert battery is not None
    assert abs(battery["soc"] - 0.62) < 1e-9
    assert battery["estimated_capacity_kwh"] is not None


def test_tariff_config_serialised():
    coord = make_coordinator()
    result = _coordinator_to_dict("e", coord)
    tc = result["inputs"]["tariff_config"]
    assert tc is not None
    assert "distribution_fee" in tc
    assert "tax_rate" in tc


def test_last_dispatched_fields():
    coord = make_coordinator(
        last_dispatched_action="charge_from_grid",
        last_dispatched_at=BASE,
    )
    result = _coordinator_to_dict("e", coord)
    assert result["last_dispatched_action"] == "charge_from_grid"
    assert result["last_dispatched_at"] == BASE.isoformat()


def test_last_dispatched_at_none_is_null():
    coord = make_coordinator(last_dispatched_at=None)
    coord.last_dispatched_at = None
    result = _coordinator_to_dict("e", coord)
    assert result["last_dispatched_at"] is None


def test_ev_state_none_gives_null_ev_inputs():
    coord = make_coordinator(include_ev=False)
    result = _coordinator_to_dict("e", coord)
    assert result["inputs"]["ev"] is None


def test_ev_state_serialised_when_present():
    coord = make_coordinator(include_ev=True)
    result = _coordinator_to_dict("e", coord)
    ev = result["inputs"]["ev"]
    assert ev is not None
    assert ev["plugged_in"] is True
    assert abs(ev["soc"] - 0.4) < 1e-9
    assert ev["departure_time"] is not None


# ---------------------------------------------------------------------------
# SunSaleDebugView: verify url / auth attributes
# ---------------------------------------------------------------------------

def test_view_url_and_auth():
    view = SunSaleDebugView()
    assert view.url == "/api/sun_sale/debug"
    assert view.requires_auth is True
    assert view.name == "api:sun_sale:debug"
