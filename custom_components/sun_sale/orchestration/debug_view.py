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
        """Handle GET /api/sun_sale/debug; return JSON snapshot of all coordinators.

        Args:
            request: Incoming aiohttp request.

        Returns:
            JSON response containing a list of coordinator state dicts.
        """
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
    battery_state = data.get("battery_state")
    pricing = data.get("pricing")
    forecast = data.get("forecast")
    calculation = data.get("calculation")
    observed_gen = data.get("observed_generation")
    forecast_err = data.get("forecast_error")
    charging_prof = data.get("charging_profile")
    base_load_prof = data.get("base_load_profile")
    batt_status = data.get("battery_status")
    batt_runtime = data.get("battery_runtime")
    profitability = data.get("profitability_score")
    forecast_quality = data.get("forecast_quality")
    sun_times = data.get("sun_times")
    monthly_bill = data.get("monthly_bill")
    grid_power_history = data.get("grid_power_history")

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
            "grid_power_history": {
                "sample_count": len(grid_power_history.samples),
                "samples": [
                    {"timestamp": s.timestamp.isoformat(), "power_kw": round(s.power_kw, 4)}
                    for s in grid_power_history.samples
                ],
            } if grid_power_history is not None else None,
            "tariff_config": (
                dataclasses.asdict(coordinator.tariff_config)
                if coordinator.tariff_config is not None else None
            ),
            "consumption_today_kwh": data.get("consumption_today_kwh"),
            "yesterday_solar": {
                "date": getattr(coordinator, "_yesterday_stored_date", None),
                "entries": [
                    {
                        "start": e.start.isoformat(),
                        "end": e.end.isoformat(),
                        "kwh": e.expected_kwh,
                    }
                    for e in getattr(coordinator, "_yesterday_solar", [])
                ],
            },
        },
        "pipeline": {
            "pricing": {
                "slot_count": len(pricing.slots),
                "resolution_s": int(pricing.resolution.total_seconds()),
                "computed_at": pricing.computed_at.isoformat(),
                "negative_sell_count": sum(1 for s in pricing.slots if s.sell_eur_kwh <= 0),
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "buy": round(s.buy_eur_kwh, 4),
                        "sell": round(s.sell_eur_kwh, 4),
                        "spot": round(s.spot_eur_kwh, 4),
                    }
                    for s in pricing.slots
                ],
            } if pricing is not None else None,
            "forecast": {
                "slot_count": len(forecast.slots),
                "total_yesterday_kwh": round(forecast.total_yesterday_kwh, 4),
                "total_today_kwh": round(forecast.total_today_kwh, 4),
                "total_tomorrow_kwh": round(forecast.total_tomorrow_kwh, 4),
                "today_remaining_kwh": round(forecast.today_remaining_kwh, 4),
                "total_d2_kwh": round(forecast.total_d2_kwh, 4),
                "total_d3_kwh": round(forecast.total_d3_kwh, 4),
                "total_d4_kwh": round(forecast.total_d4_kwh, 4),
                "total_d5_kwh": round(forecast.total_d5_kwh, 4),
                "total_d6_kwh": round(forecast.total_d6_kwh, 4),
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "expected_kwh": round(s.expected_kwh, 4),
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
            "observed_generation": {
                "slot_count": len(observed_gen.slots),
                "total_yesterday_kwh": round(observed_gen.total_yesterday_kwh, 4),
                "total_today_so_far_kwh": round(observed_gen.total_today_so_far_kwh, 4),
                "computed_at": observed_gen.computed_at.isoformat(),
                "slots": [
                    {"start": s.start.isoformat(), "generated_kwh": round(s.generated_kwh, 4)}
                    for s in observed_gen.slots
                ],
            } if observed_gen is not None else None,
            "forecast_error": {
                "slot_count": len(forecast_err.slots),
                "total_forecast_kwh": round(forecast_err.total_forecast_kwh, 4),
                "total_observed_kwh": round(forecast_err.total_observed_kwh, 4),
                "total_error_kwh": round(forecast_err.total_error_kwh, 4),
                "mean_absolute_error_kwh": round(forecast_err.mean_absolute_error_kwh, 4),
                "bias_kwh": round(forecast_err.bias_kwh, 4),
                "mean_absolute_percentage_error": (
                    round(forecast_err.mean_absolute_percentage_error, 4)
                    if forecast_err.mean_absolute_percentage_error is not None else None
                ),
                "computed_at": forecast_err.computed_at.isoformat(),
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "forecast_kwh": round(s.forecast_kwh, 4),
                        "observed_kwh": round(s.observed_kwh, 4),
                        "error_kwh": round(s.error_kwh, 4),
                        "relative_error": (
                            round(s.relative_error, 4) if s.relative_error is not None else None
                        ),
                    }
                    for s in forecast_err.slots
                ],
            } if forecast_err is not None else None,
            "charging_profile": {
                "slot_count": len(charging_prof.slots),
                "free_capacity_kwh": round(charging_prof.free_capacity_kwh, 4),
                "today_remaining_generation_kwh": round(charging_prof.today_remaining_generation_kwh, 4),
                "solar_exceeds_capacity": charging_prof.solar_exceeds_capacity,
                "allocated_solar_kwh": round(charging_prof.allocated_solar_kwh, 4),
                "total_no_export_kwh": round(charging_prof.total_no_export_kwh, 4),
                "computed_at": charging_prof.computed_at.isoformat(),
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "mode": s.mode.value,
                        "expected_kwh": round(s.expected_kwh, 4),
                        "sell_eur_kwh": round(s.sell_eur_kwh, 4),
                    }
                    for s in charging_prof.slots
                ],
            } if charging_prof is not None else None,
            "base_load_profile": {
                "fallback_kw": round(base_load_prof.fallback_kw, 4),
                "overall_p10_kw": round(base_load_prof.overall_p10_kw, 4),
                "overall_median_kw": round(base_load_prof.overall_median_kw, 4),
                "confidence": (
                    round(base_load_prof.confidence, 4)
                    if base_load_prof.confidence is not None else None
                ),
                "sample_count": base_load_prof.sample_count,
                "distinct_days": base_load_prof.distinct_days,
                "computed_at": base_load_prof.computed_at.isoformat(),
                "slots": [
                    {
                        "hour": s.hour,
                        "baseload_kw": round(s.baseload_kw, 4),
                        "sample_count": s.sample_count,
                        "is_fallback": s.is_fallback,
                    }
                    for s in base_load_prof.slots
                ],
            } if base_load_prof is not None else None,
            "battery_status": {
                "total_capacity_kwh": round(batt_status.total_capacity_kwh, 4),
                "max_charge_power_kw": round(batt_status.max_charge_power_kw, 4),
                "max_discharge_power_kw": round(batt_status.max_discharge_power_kw, 4),
                "soc": round(batt_status.soc, 4),
                "remaining_capacity_kwh": round(batt_status.remaining_capacity_kwh, 4),
            } if batt_status is not None else None,
            "battery_runtime": {
                "remaining_kwh_usable": round(batt_runtime.remaining_kwh_usable, 4),
                "avg_drain_kw_next_hour": round(batt_runtime.avg_drain_kw_next_hour, 4),
                "runtime_minutes": (
                    round(batt_runtime.runtime_minutes, 1)
                    if batt_runtime.runtime_minutes is not None else None
                ),
                "until": batt_runtime.until.isoformat() if batt_runtime.until is not None else None,
                "horizon_hours": batt_runtime.horizon_hours,
                "computed_at": batt_runtime.computed_at.isoformat(),
            } if batt_runtime is not None else None,
            "profitability_score": {
                "score": (
                    round(profitability.score, 4)
                    if profitability.score is not None else None
                ),
                "today_peak_eur_kwh": round(profitability.today_peak_eur_kwh, 4),
                "today_class": profitability.today_class.value,
                "class_medians": {
                    k.value: round(v, 4)
                    for k, v in profitability.class_medians.items()
                },
                "window_days": profitability.window_days,
                "computed_at": profitability.computed_at.isoformat(),
            } if profitability is not None else None,
            "forecast_quality": {
                "sunrise_utc": sun_times.today_sunrise.isoformat() if (sun_times and sun_times.today_sunrise) else None,
                "sunset_utc": sun_times.today_sunset.isoformat() if (sun_times and sun_times.today_sunset) else None,
                "group1": {k: v.metrics() for k, v in forecast_quality.group1.items()},
                "group2": {k: v.metrics() for k, v in forecast_quality.group2.items()},
                "group3": {k: v.metrics() for k, v in forecast_quality.group3.items()},
                "group3_pending_count": len(forecast_quality.group3_pending),
            } if forecast_quality is not None else None,
            "monthly_bill": {
                "slot_count": len(monthly_bill.slots),
                "carry_eur": round(monthly_bill.carry_eur, 4),
                "yday_to_now_eur": round(monthly_bill.yday_to_now_eur, 4),
                "total_month_eur": round(monthly_bill.total_month_eur, 4),
                "month_str": monthly_bill.month_str,
                "previous_month_str": monthly_bill.previous_month_str,
                "previous_month_eur": round(monthly_bill.previous_month_eur, 4),
                "computed_at": monthly_bill.computed_at.isoformat(),
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "end": s.end.isoformat(),
                        "imported_kwh": round(s.imported_kwh, 4),
                        "exported_kwh": round(s.exported_kwh, 4),
                        "buy_eur_kwh": round(s.buy_eur_kwh, 4),
                        "sell_eur_kwh": round(s.sell_eur_kwh, 4),
                        "net_cost_eur": round(s.net_cost_eur, 6),
                    }
                    for s in monthly_bill.slots
                ],
            } if monthly_bill is not None else None,
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
        },
        "last_dispatched_action": coordinator.last_dispatched_action,
        "last_dispatched_at": (
            coordinator.last_dispatched_at.isoformat()
            if coordinator.last_dispatched_at is not None else None
        ),
    }
