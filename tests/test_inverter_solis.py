"""Tests for Solis-specific inverter dispatch in inverter.py."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.sun_sale.inverter import InverterController, InverterPlatform
from custom_components.sun_sale.models import BatteryConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_battery_config(
    nominal_voltage_v: float = 48.0,
    max_charge_kw: float = 5.0,
    max_discharge_kw: float = 5.0,
) -> BatteryConfig:
    return BatteryConfig(
        nominal_capacity_kwh=10.0,
        purchase_price_eur=5000.0,
        rated_cycle_life=6000,
        max_charge_power_kw=max_charge_kw,
        max_discharge_power_kw=max_discharge_kw,
        min_soc=0.10,
        max_soc=0.95,
        round_trip_efficiency=0.90,
        nominal_voltage_v=nominal_voltage_v,
    )


SOLIS_ENTITY_IDS = {
    "battery_soc": "sensor.solis_battery_soc",
    "battery_power": "sensor.solis_battery_power",
    "grid_power": "sensor.solis_ac_grid_port_power",
    "solis_charge_current": "number.solis_time_charging_charge_current",
    "solis_discharge_current": "number.solis_time_charging_discharge_current",
    "solis_charge_start_time_1": "time.solis_time_charging_charge_start_slot_1",
    "solis_charge_end_time_1": "time.solis_time_charging_charge_end_slot_1",
    "solis_discharge_start_time_1": "time.solis_time_charging_discharge_start_slot_1",
    "solis_discharge_end_time_1": "time.solis_time_charging_discharge_end_slot_1",
    "solis_tou_mode_switch": "switch.solis_time_of_use_mode",
    "solis_allow_grid_charge_switch": "switch.solis_allow_grid_to_charge_the_battery",
    "solis_self_use_mode_switch": "switch.solis_self_use_mode",
}


def make_controller(battery_config: BatteryConfig | None = None) -> tuple[InverterController, MagicMock]:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    if battery_config is None:
        battery_config = make_battery_config()
    return InverterController(hass, InverterPlatform.SOLIS, SOLIS_ENTITY_IDS, battery_config), hass


def _calls_for(hass: MagicMock):
    """Return all recorded async_call invocations as (domain, service, data) tuples."""
    return [
        (c.args[0], c.args[1], c.args[2])
        for c in hass.services.async_call.call_args_list
    ]


def _entity_value(calls, entity_id: str):
    """Return the value from the first number.set_value or time.set_value call to entity_id."""
    for domain, svc, data in calls:
        if svc == "set_value" and data.get("entity_id") == entity_id:
            return data.get("value", data.get("time"))
    return None


# ---------------------------------------------------------------------------
# Charge tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_charge_issues_correct_service_calls():
    controller, hass = make_controller()

    fixed_now = datetime(2026, 4, 26, 10, 17, 0, tzinfo=timezone.utc)
    with patch(
        "custom_components.sun_sale.inverter.dt_util.now",
        return_value=fixed_now,
    ):
        await controller.async_charge_from_grid(2.5)

    calls = _calls_for(hass)
    domains_svcs = [(d, s) for d, s, _ in calls]

    # charge current written
    assert ("number", "set_value") in domains_svcs
    # time slots written
    assert ("time", "set_value") in domains_svcs
    # allow-grid-charge and TOU mode turned on
    assert ("switch", "turn_on") in domains_svcs

    # amps = 2500 / 48.0
    expected_amps = 2500 / 48.0
    actual_amps = _entity_value(calls, SOLIS_ENTITY_IDS["solis_charge_current"])
    assert actual_amps is not None
    assert abs(actual_amps - expected_amps) < 0.01

    # 2 slot-time writes
    for key in ("solis_charge_start_time_1", "solis_charge_end_time_1"):
        assert _entity_value(calls, SOLIS_ENTITY_IDS[key]) is not None, f"missing write to {key}"

    # start time: hour=10, minute rounded down to 15
    assert _entity_value(calls, SOLIS_ENTITY_IDS["solis_charge_start_time_1"]) == "10:15:00"
    assert _entity_value(calls, SOLIS_ENTITY_IDS["solis_charge_end_time_1"]) == "11:00:00"

    # allow-grid-charge switched on
    turn_on_entities = [d["entity_id"] for dom, svc, d in calls if svc == "turn_on"]
    assert SOLIS_ENTITY_IDS["solis_allow_grid_charge_switch"] in turn_on_entities
    assert SOLIS_ENTITY_IDS["solis_tou_mode_switch"] in turn_on_entities

    # total calls: 1 charge_current + 2 time slots + 1 allow_grid + 1 tou = 5
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_charge_does_not_write_discharge_entities():
    controller, hass = make_controller()

    fixed_now = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
    with patch("custom_components.sun_sale.inverter.dt_util.now", return_value=fixed_now):
        await controller.async_charge_from_grid(1.0)

    calls = _calls_for(hass)
    written_entities = {d["entity_id"] for _, _, d in calls}
    assert SOLIS_ENTITY_IDS["solis_discharge_current"] not in written_entities


# ---------------------------------------------------------------------------
# Discharge tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discharge_issues_correct_service_calls():
    controller, hass = make_controller()

    fixed_now = datetime(2026, 4, 26, 14, 33, 0, tzinfo=timezone.utc)
    with patch("custom_components.sun_sale.inverter.dt_util.now", return_value=fixed_now):
        await controller.async_discharge_to_grid(2.5)

    calls = _calls_for(hass)

    expected_amps = 2500 / 48.0
    actual_amps = _entity_value(calls, SOLIS_ENTITY_IDS["solis_discharge_current"])
    assert actual_amps is not None
    assert abs(actual_amps - expected_amps) < 0.01

    # 2 discharge slot-time writes
    for key in ("solis_discharge_start_time_1", "solis_discharge_end_time_1"):
        assert _entity_value(calls, SOLIS_ENTITY_IDS[key]) is not None

    # start: hour=14, minute rounded to 30
    assert _entity_value(calls, SOLIS_ENTITY_IDS["solis_discharge_start_time_1"]) == "14:30:00"
    assert _entity_value(calls, SOLIS_ENTITY_IDS["solis_discharge_end_time_1"]) == "15:00:00"

    # TOU mode switched on
    turn_on_entities = [d["entity_id"] for _, svc, d in calls if svc == "turn_on"]
    assert SOLIS_ENTITY_IDS["solis_tou_mode_switch"] in turn_on_entities

    # no allow-grid-charge for discharge
    assert SOLIS_ENTITY_IDS["solis_allow_grid_charge_switch"] not in turn_on_entities

    # total: 1 discharge_current + 2 time slots + 1 tou = 4
    assert len(calls) == 4


# ---------------------------------------------------------------------------
# Idle tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idle_issues_correct_service_calls():
    controller, hass = make_controller()
    await controller.async_idle()

    calls = _calls_for(hass)

    # TOU turned off
    turn_off_entities = [d["entity_id"] for _, svc, d in calls if svc == "turn_off"]
    assert SOLIS_ENTITY_IDS["solis_tou_mode_switch"] in turn_off_entities

    # self-use turned on
    turn_on_entities = [d["entity_id"] for _, svc, d in calls if svc == "turn_on"]
    assert SOLIS_ENTITY_IDS["solis_self_use_mode_switch"] in turn_on_entities

    # both currents zeroed
    assert _entity_value(calls, SOLIS_ENTITY_IDS["solis_charge_current"]) == 0
    assert _entity_value(calls, SOLIS_ENTITY_IDS["solis_discharge_current"]) == 0

    # total: 1 tou_off + 1 self_use_on + 2 zero currents = 4
    assert len(calls) == 4


# ---------------------------------------------------------------------------
# Clamping tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_charge_power_above_max_is_clamped():
    # max_charge = 3.0 kW at 48 V → max amps = 3000/48 = 62.5 A
    cfg = make_battery_config(nominal_voltage_v=48.0, max_charge_kw=3.0)
    controller, hass = make_controller(cfg)

    fixed_now = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
    with patch("custom_components.sun_sale.inverter.dt_util.now", return_value=fixed_now):
        await controller.async_charge_from_grid(10.0)  # 10 kW >> 3 kW cap

    calls = _calls_for(hass)
    actual_amps = _entity_value(calls, SOLIS_ENTITY_IDS["solis_charge_current"])
    max_amps = 3000 / 48.0
    assert actual_amps <= max_amps + 1e-9
    assert abs(actual_amps - max_amps) < 0.01


@pytest.mark.asyncio
async def test_discharge_power_above_max_is_clamped():
    cfg = make_battery_config(nominal_voltage_v=48.0, max_discharge_kw=2.0)
    controller, hass = make_controller(cfg)

    fixed_now = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
    with patch("custom_components.sun_sale.inverter.dt_util.now", return_value=fixed_now):
        await controller.async_discharge_to_grid(8.0)

    calls = _calls_for(hass)
    actual_amps = _entity_value(calls, SOLIS_ENTITY_IDS["solis_discharge_current"])
    max_amps = 2000 / 48.0
    assert actual_amps <= max_amps + 1e-9
    assert abs(actual_amps - max_amps) < 0.01


# ---------------------------------------------------------------------------
# Voltage scaling tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_different_voltages_produce_different_amps_for_same_kw():
    cfg_lv = make_battery_config(nominal_voltage_v=48.0, max_charge_kw=20.0)
    cfg_hv = make_battery_config(nominal_voltage_v=400.0, max_charge_kw=20.0)

    ctrl_lv, hass_lv = make_controller(cfg_lv)
    ctrl_hv, hass_hv = make_controller(cfg_hv)

    fixed_now = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
    with patch("custom_components.sun_sale.inverter.dt_util.now", return_value=fixed_now):
        await ctrl_lv.async_charge_from_grid(2.5)
    with patch("custom_components.sun_sale.inverter.dt_util.now", return_value=fixed_now):
        await ctrl_hv.async_charge_from_grid(2.5)

    amps_lv = _entity_value(_calls_for(hass_lv), SOLIS_ENTITY_IDS["solis_charge_current"])
    amps_hv = _entity_value(_calls_for(hass_hv), SOLIS_ENTITY_IDS["solis_charge_current"])

    # 48 V bus → more amps than 400 V bus for same kW
    assert abs(amps_lv - 2500 / 48.0) < 0.01
    assert abs(amps_hv - 2500 / 400.0) < 0.01
    assert amps_lv > amps_hv


# ---------------------------------------------------------------------------
# Non-Solis platforms are unaffected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_huawei_dispatch_unchanged():
    """Huawei Solar path must still write signed watts, not amps."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    cfg = make_battery_config()
    controller = InverterController(
        hass,
        InverterPlatform.HUAWEI_SOLAR,
        {"charge_control": "number.huawei_charge_control"},
        cfg,
    )
    await controller.async_charge_from_grid(3.0)

    calls = _calls_for(hass)
    assert len(calls) == 1
    domain, svc, data = calls[0]
    assert domain == "number" and svc == "set_value"
    assert abs(data["value"] - 3000.0) < 0.01  # watts, not amps


@pytest.mark.asyncio
async def test_generic_dispatch_unchanged():
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    cfg = make_battery_config()
    controller = InverterController(
        hass,
        InverterPlatform.GENERIC,
        {"charge_control": "number.generic_control"},
        cfg,
    )
    await controller.async_discharge_to_grid(2.0)

    calls = _calls_for(hass)
    assert len(calls) == 1
    domain, svc, data = calls[0]
    assert domain == "number" and svc == "set_value"
    assert abs(data["value"] - (-2.0)) < 0.01  # signed kW, negative = discharge
