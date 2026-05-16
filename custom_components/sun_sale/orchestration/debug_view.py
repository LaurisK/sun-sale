"""HTTP diagnostic view for sunSale — exposes a JSON snapshot of all coordinator state."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from ..contract.const import CONF_SOLAR_FORECAST_ENTITY, CONF_SOLAR_FORECAST_ENTITY_2, CONF_NORDPOOL_ENTITY, DOMAIN


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
    pricing = data.get("pricing")
    forecast = data.get("forecast")
    calculation = data.get("calculation")

    cfg = coordinator._config  # noqa: SLF001
    return {
        "entry_id": entry_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "automation_enabled": coordinator.automation_enabled,
        "config": {
            "nordpool_entity": cfg.get(CONF_NORDPOOL_ENTITY, ""),
            "solar_forecast_entity": cfg.get(CONF_SOLAR_FORECAST_ENTITY, ""),
            "solar_forecast_entity_2": cfg.get(CONF_SOLAR_FORECAST_ENTITY_2, ""),
        },
        "inputs": {
            "nordpool_prices": [
                {"start": p.start.isoformat(), "end": p.end.isoformat(), "price": p.price_eur_kwh}
                for p in data.get("prices", [])
            ],
            "battery": {
                "soc": battery_state.soc,
                "power_kw": data.get("battery_power_kw"),
                "estimated_capacity_kwh": data.get("estimated_capacity"),
            } if battery_state is not None else None,
            "grid_power_kw": data.get("grid_power_kw"),
            "tariff_config": (
                dataclasses.asdict(coordinator.tariff_config)
                if coordinator.tariff_config is not None else None
            ),
            "ev": {
                "plugged_in": ev_state.is_plugged_in,
                "soc": ev_state.soc,
                "target_soc": ev_state.target_soc,
                "departure_time": ev_state.departure_time.isoformat() if ev_state.departure_time else None,
            } if ev_state is not None else None,
        },
        "pipeline": {
            "pricing": {
                "slot_count": len(pricing.slots),
                "resolution_s": int(pricing.resolution.total_seconds()),
                "computed_at": pricing.computed_at.isoformat(),
                "negative_sell_count": sum(1 for s in pricing.slots if not s.sell_allowed),
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "buy": round(s.buy_eur_kwh, 4),
                        "sell": round(s.sell_eur_kwh, 4),
                        "spot": round(s.spot_eur_kwh, 4),
                        "sell_allowed": s.sell_allowed,
                    }
                    for s in pricing.slots
                ],
            } if pricing is not None else None,
            "forecast": {
                "slot_count": len(forecast.slots),
                "primary": forecast.primary,
                "overlays": list(forecast.overlays),
                "computed_at": forecast.computed_at.isoformat(),
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "expected_kwh": round(s.expected_kwh, 4),
                        "source": s.source,
                        "confidence": s.confidence,
                    }
                    for s in forecast.slots
                ],
            } if forecast is not None else None,
            "calculation": {
                "slot_count": len(calculation.slots),
                "total_negative_sale_kwh": round(calculation.total_negative_sale_kwh, 4),
                "computed_at": calculation.computed_at.isoformat(),
                "feed_in_lockout_windows": [
                    {"start": w[0].isoformat(), "end": w[1].isoformat()}
                    for w in calculation.feed_in_lockout_windows
                ],
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "sell_allowed": s.sell_allowed,
                        "expected_solar_kwh": round(s.expected_solar_kwh, 4),
                        "expected_solar_negative_sale_kwh": round(s.expected_solar_negative_sale_kwh, 4),
                        "notes": list(s.notes),
                    }
                    for s in calculation.slots
                ],
            } if calculation is not None else None,
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
