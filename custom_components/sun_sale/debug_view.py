"""HTTP diagnostic view for sunSale — exposes a JSON snapshot of all coordinator state."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN


class SunSaleDebugView(HomeAssistantView):
    url = "/api/sun_sale/debug"
    name = "api:sun_sale:debug"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        entries = [
            _coordinator_to_dict(entry_id, coordinator)
            for entry_id, coordinator in hass.data.get(DOMAIN, {}).items()
        ]
        return self.json(entries)


def _coordinator_to_dict(entry_id: str, coordinator: Any) -> dict:
    """Serialise one coordinator's full state into a JSON-safe dict."""
    data = coordinator.data or {}

    schedule = data.get("schedule")
    ev_schedule = data.get("ev_schedule")
    battery_state = data.get("battery_state")
    ev_state = data.get("ev_state")

    return {
        "entry_id": entry_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "automation_enabled": coordinator.automation_enabled,
        "inputs": {
            "nordpool_prices": [
                {"start": p.start.isoformat(), "end": p.end.isoformat(), "price": p.price_eur_kwh}
                for p in data.get("prices", [])
            ],
            "solar_forecast": [
                {"start": f.start.isoformat(), "end": f.end.isoformat(), "generation_kwh": f.generation_kwh}
                for f in data.get("solar_forecast", [])
            ],
            "battery": {
                "soc": battery_state.soc,
                "power_kw": data.get("battery_power_kw"),
                "estimated_capacity_kwh": data.get("estimated_capacity"),
            } if battery_state is not None else None,
            "grid_power_kw": data.get("grid_power_kw"),
            "tariff_config": (
                dataclasses.asdict(coordinator._tariff_config)
                if coordinator._tariff_config is not None else None
            ),
            "ev": {
                "plugged_in": ev_state.is_plugged_in,
                "soc": ev_state.soc,
                "target_soc": ev_state.target_soc,
                "departure_time": ev_state.departure_time.isoformat() if ev_state.departure_time else None,
            } if ev_state is not None else None,
        },
        "computed": {
            "tariffs": [
                {
                    "hour": t.hour.isoformat(),
                    "spot": t.spot_price,
                    "buy": t.buy_price,
                    "sell": t.sell_price,
                }
                for t in data.get("tariffs", [])
            ],
            "degradation_cost_per_kwh": data.get("degradation_cost"),
        },
        "outputs": {
            "schedule": {
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "end": s.end.isoformat(),
                        "action": s.action.value,
                        "power_kw": s.power_kw,
                        "expected_profit_eur": s.expected_profit_eur,
                        "reason": s.reason,
                    }
                    for s in schedule.slots
                ],
                "total_expected_profit_eur": schedule.total_expected_profit_eur,
            } if schedule is not None else None,
            "ev_schedule": {
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "end": s.end.isoformat(),
                        "charge_power_kw": s.charge_power_kw,
                        "cost_eur": s.cost_eur,
                    }
                    for s in ev_schedule.slots
                ],
                "total_cost_eur": ev_schedule.total_cost_eur,
                "total_energy_kwh": ev_schedule.total_energy_kwh,
            } if ev_schedule is not None else None,
        },
        "last_dispatched_action": coordinator.last_dispatched_action,
        "last_dispatched_at": (
            coordinator.last_dispatched_at.isoformat()
            if coordinator.last_dispatched_at is not None else None
        ),
    }
