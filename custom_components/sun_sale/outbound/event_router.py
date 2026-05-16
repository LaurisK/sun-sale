"""Event router (Layer 4) — routes ControlEvents from DAG nodes to output adapters.

Deduplication lives here; nodes emit events freely; the router suppresses
repeated identical commands and only calls the adapter on a change.
"""
from __future__ import annotations

import logging

from ..contract.events import ControlEvent, EVActionEvent, InverterActionEvent
from .ev_charger import EVChargerController
from .inverter import InverterController
from ..contract.models import Action

_LOGGER = logging.getLogger(__name__)


class EventRouter:
    """Routes DAG ControlEvents to inverter / EV charger output adapters.

    Deduplication: inverter commands are keyed by (action, power_kw); repeated
    identical commands within a cycle are suppressed.
    """

    def __init__(
        self,
        inverter: InverterController,
        ev_charger: EVChargerController | None,
    ) -> None:
        self._inverter = inverter
        self._ev_charger = ev_charger
        self._last_inverter_key: str | None = None
        self.last_dispatched_action: str | None = None

    async def handle(self, event: ControlEvent) -> None:
        if isinstance(event, InverterActionEvent):
            await self._handle_inverter(event)
        elif isinstance(event, EVActionEvent) and self._ev_charger is not None:
            await self._handle_ev(event)

    async def _handle_inverter(self, event: InverterActionEvent) -> None:
        key = f"{event.action.value}:{event.power_kw:.3f}"
        if key == self._last_inverter_key:
            return
        if event.action == Action.CHARGE_FROM_GRID:
            await self._inverter.async_charge_from_grid(event.power_kw)
        elif event.action == Action.DISCHARGE_TO_GRID:
            await self._inverter.async_discharge_to_grid(event.power_kw)
        else:
            await self._inverter.async_idle()
        self._last_inverter_key = key
        self.last_dispatched_action = event.action.value
        _LOGGER.info("sunSale dispatch: %s @ %.3f kW", event.action.value, event.power_kw)

    async def _handle_ev(self, event: EVActionEvent) -> None:
        if event.charge_power_kw > 0:
            await self._ev_charger.async_start_charging(event.charge_power_kw)
        else:
            await self._ev_charger.async_stop_charging()
