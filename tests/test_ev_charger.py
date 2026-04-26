"""Tests for ev_charger.py — EVChargerController state reads and platform dispatch."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

from custom_components.sun_sale.ev_charger import EVChargerController, EVChargerPlatform


def make_controller(platform: EVChargerPlatform = EVChargerPlatform.GENERIC):
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    ctrl = EVChargerController(
        hass,
        platform,
        {
            "plug_state": "binary_sensor.ev_plug",
            "soc": "sensor.ev_soc",
            "charger_switch": "switch.ev_charger",
            "charge_current": "number.ev_current",
        },
    )
    return hass, ctrl


def _state(value: str) -> MagicMock:
    s = MagicMock()
    s.state = value
    return s


# ---------------------------------------------------------------------------
# is_plugged_in
# ---------------------------------------------------------------------------

def test_is_plugged_in_true():
    hass, ctrl = make_controller()
    hass.states.get.return_value = _state("on")
    assert ctrl.is_plugged_in() is True


def test_is_plugged_in_false_when_off():
    hass, ctrl = make_controller()
    hass.states.get.return_value = _state("off")
    assert ctrl.is_plugged_in() is False


def test_is_plugged_in_false_when_entity_missing():
    hass, ctrl = make_controller()
    hass.states.get.return_value = None
    assert ctrl.is_plugged_in() is False


# ---------------------------------------------------------------------------
# get_ev_soc
# ---------------------------------------------------------------------------

def test_get_ev_soc_percentage():
    hass, ctrl = make_controller()
    hass.states.get.return_value = _state("80")
    assert abs(ctrl.get_ev_soc() - 0.80) < 1e-9


def test_get_ev_soc_already_fraction():
    hass, ctrl = make_controller()
    hass.states.get.return_value = _state("0.5")
    assert abs(ctrl.get_ev_soc() - 0.5) < 1e-9


def test_get_ev_soc_none_when_unavailable():
    hass, ctrl = make_controller()
    hass.states.get.return_value = _state("unavailable")
    assert ctrl.get_ev_soc() is None


def test_get_ev_soc_none_when_unknown():
    hass, ctrl = make_controller()
    hass.states.get.return_value = _state("unknown")
    assert ctrl.get_ev_soc() is None


def test_get_ev_soc_none_when_entity_missing():
    hass, ctrl = make_controller()
    hass.states.get.return_value = None
    assert ctrl.get_ev_soc() is None


def test_get_ev_soc_none_when_no_entity_id_configured():
    hass = MagicMock()
    ctrl = EVChargerController(hass, EVChargerPlatform.GENERIC, {})
    assert ctrl.get_ev_soc() is None


def test_get_ev_soc_none_on_non_numeric_state():
    hass, ctrl = make_controller()
    hass.states.get.return_value = _state("error")
    assert ctrl.get_ev_soc() is None


# ---------------------------------------------------------------------------
# async_start_charging / async_stop_charging — GENERIC
# ---------------------------------------------------------------------------

async def test_generic_start_charging():
    hass, ctrl = make_controller(EVChargerPlatform.GENERIC)
    await ctrl.async_start_charging(7.4)
    hass.services.async_call.assert_called_once_with(
        "switch", "turn_on", {"entity_id": "switch.ev_charger"}, blocking=True,
    )


async def test_generic_stop_charging():
    hass, ctrl = make_controller(EVChargerPlatform.GENERIC)
    await ctrl.async_stop_charging()
    hass.services.async_call.assert_called_once_with(
        "switch", "turn_off", {"entity_id": "switch.ev_charger"}, blocking=True,
    )


# ---------------------------------------------------------------------------
# async_start_charging / async_stop_charging — OPENEVSE
# ---------------------------------------------------------------------------

async def test_openevse_start_charging_sets_current_then_switches_on():
    hass, ctrl = make_controller(EVChargerPlatform.OPENEVSE)
    await ctrl.async_start_charging(3.68)  # 3680 / 230 = 16 A
    assert hass.services.async_call.call_count == 2
    assert hass.services.async_call.call_args_list[0] == call(
        "number", "set_value", {"entity_id": "number.ev_current", "value": 16}, blocking=True,
    )
    assert hass.services.async_call.call_args_list[1] == call(
        "switch", "turn_on", {"entity_id": "switch.ev_charger"}, blocking=True,
    )


async def test_openevse_stop_charging_turns_switch_off():
    hass, ctrl = make_controller(EVChargerPlatform.OPENEVSE)
    await ctrl.async_stop_charging()
    hass.services.async_call.assert_called_once_with(
        "switch", "turn_off", {"entity_id": "switch.ev_charger"}, blocking=True,
    )


# ---------------------------------------------------------------------------
# async_start_charging / async_stop_charging — EASEE
# ---------------------------------------------------------------------------

async def test_easee_start_charging():
    hass, ctrl = make_controller(EVChargerPlatform.EASEE)
    await ctrl.async_start_charging(7.4)
    hass.services.async_call.assert_called_once_with(
        "easee", "start_charging", {"charger_id": "switch.ev_charger"}, blocking=True,
    )


async def test_easee_stop_charging():
    hass, ctrl = make_controller(EVChargerPlatform.EASEE)
    await ctrl.async_stop_charging()
    hass.services.async_call.assert_called_once_with(
        "easee", "stop_charging", {"charger_id": "switch.ev_charger"}, blocking=True,
    )


# ---------------------------------------------------------------------------
# async_start_charging / async_stop_charging — WALLBOX
# ---------------------------------------------------------------------------

async def test_wallbox_start_charging():
    hass, ctrl = make_controller(EVChargerPlatform.WALLBOX)
    await ctrl.async_start_charging(7.4)
    hass.services.async_call.assert_called_once_with(
        "wallbox", "start_charging", {"entity_id": "switch.ev_charger"}, blocking=True,
    )


async def test_wallbox_stop_charging():
    hass, ctrl = make_controller(EVChargerPlatform.WALLBOX)
    await ctrl.async_stop_charging()
    hass.services.async_call.assert_called_once_with(
        "wallbox", "stop_charging", {"entity_id": "switch.ev_charger"}, blocking=True,
    )
