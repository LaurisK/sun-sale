"""Sensor entities for sunSale."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .contract.const import CONF_INVERTER_ENTITY_SOLAR_ENERGY, DOMAIN
from .orchestration.coordinator import SunSaleCoordinator
from .contract.models import (
    BaseLoadProfile,
    BatteryRuntimeEstimate,
    CalculationResult,
    ChargeMode,
    ChargingProfile,
    ForecastErrorSeries,
    ForecastQualityStore,
    GenerationSeries,
    InverterModeHistory,
    InverterModeReading,
    MonthlyBillResult,
    ObservedGenerationSeries,
    PriceSeries,
    PriceSlot,
    Schedule,
    StorageMode,
    SunTimes,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create and register all sunSale sensor entities for a config entry.

    Args:
        hass: Home Assistant instance.
        entry: Config entry providing the coordinator reference.
        async_add_entities: HA callback to register the entity list.
    """
    coordinator: SunSaleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        CurrentActionSensor(coordinator, entry),
        NextActionSensor(coordinator, entry),
        NextActionTimeSensor(coordinator, entry),
        ExpectedProfitSensor(coordinator, entry),
        DegradationCostSensor(coordinator, entry),
        EstimatedCapacitySensor(coordinator, entry),
        CurrentBuyPriceSensor(coordinator, entry),
        CurrentSellPriceSensor(coordinator, entry),
        ScheduleSensor(coordinator, entry),
        DashboardSensor(coordinator, entry),
        InverterModeSensor(coordinator, entry),
        PricingPipelineSensor(coordinator, entry),
        ForecastPipelineSensor(coordinator, entry),
        CalculationPipelineSensor(coordinator, entry),
        CurrentBaseloadSensor(coordinator, entry),
        BatteryRuntimeMinutesSensor(coordinator, entry),
        BatteryDrainUntilSensor(coordinator, entry),
        BaseloadConfidenceSensor(coordinator, entry),
        MonthlyBillSensor(coordinator, entry),
    ])


def _serialize_forecast_slots(series: GenerationSeries | None) -> list[dict]:
    """Convert GenerationSeries.slots to [{t, forecast_kwh, forecast_w}] for the frontend.

    Args:
        series: GenerationSeries produced by the forecast pipeline stage, or None.

    Returns:
        List of dicts with epoch-ms timestamp, kWh, and watts per slot.
    """
    if series is None:
        return []
    result = []
    for slot in series.slots:
        slot_h = (slot.end - slot.start).total_seconds() / 3600.0
        w = slot.expected_kwh / slot_h * 1000.0 if slot_h > 0 else 0.0
        result.append({
            "t": int(slot.start.timestamp() * 1000),
            "forecast_kwh": round(slot.expected_kwh, 4),
            "forecast_w": round(w),
        })
    return result


class _BaseSensor(CoordinatorEntity, SensorEntity):
    """Shared base for all sunSale sensor entities."""

    def __init__(
        self, coordinator: SunSaleCoordinator, entry: ConfigEntry, key: str
    ) -> None:
        """Initialise with coordinator, config entry, and a unique key suffix.

        Args:
            coordinator: sunSale coordinator providing data updates.
            entry: Config entry; its entry_id scopes the unique_id.
            key: Suffix appended to entry_id to form the entity unique_id.
        """
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._entry = entry

    @property
    def device_info(self) -> dict:
        """Return device info grouping all sunSale entities under one device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "sunSale",
            "manufacturer": "sunSale",
        }

    @property
    def _schedule(self) -> Schedule | None:
        """Return the current Schedule from coordinator data, or None."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("schedule")

    def _current_slot(self):
        """Return the schedule slot active at the current time, or slots[0] as fallback."""
        schedule = self._schedule
        if not schedule or not schedule.slots:
            return None
        now = datetime.now(timezone.utc)
        return next(
            (s for s in schedule.slots if s.start <= now < s.end),
            schedule.slots[0],
        )

    def _current_price_slot(self) -> PriceSlot | None:
        """Return the PriceSlot covering the current time, or slots[0] as fallback."""
        if self.coordinator.data is None:
            return None
        pricing: PriceSeries | None = self.coordinator.data.get("pricing")
        if not pricing or not pricing.slots:
            return None
        now = datetime.now(timezone.utc)
        return pricing.slot_at(now) or pricing.slots[0]


class CurrentActionSensor(_BaseSensor):
    """Sensor reporting the StorageMode scheduled for the current hour."""

    _attr_name = "sunSale Current Action"
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, coordinator, entry):
        """Initialise current-action sensor."""
        super().__init__(coordinator, entry, "current_action")

    @property
    def native_value(self) -> str:
        """Return the current schedule slot's StorageMode string."""
        slot = self._current_slot()
        return slot.mode.value if slot else StorageMode.AUTO.value


class NextActionSensor(_BaseSensor):
    """Sensor reporting the next scheduled StorageMode that differs from the current one."""

    _attr_name = "sunSale Next Action"
    _attr_icon = "mdi:lightning-bolt-outline"

    def __init__(self, coordinator, entry):
        """Initialise next-action sensor."""
        super().__init__(coordinator, entry, "next_action")

    @property
    def native_value(self) -> str:
        """Return the next schedule mode that differs from the current slot."""
        schedule = self._schedule
        if not schedule or not schedule.slots:
            return StorageMode.AUTO.value
        now = datetime.now(timezone.utc)
        current = self._current_slot()
        if current is None:
            return StorageMode.AUTO.value
        for slot in schedule.slots:
            if slot.start > now and slot.mode != current.mode:
                return slot.mode.value
        return current.mode.value


class NextActionTimeSensor(_BaseSensor):
    """Sensor reporting the start time of the next StorageMode change."""

    _attr_name = "sunSale Next Action Time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, entry):
        """Initialise next-action-time sensor."""
        super().__init__(coordinator, entry, "next_action_time")

    @property
    def native_value(self) -> datetime | None:
        """Return the start time of the next mode change."""
        schedule = self._schedule
        if not schedule or not schedule.slots:
            return None
        now = datetime.now(timezone.utc)
        current = self._current_slot()
        if current is None:
            return None
        for slot in schedule.slots:
            if slot.start > now and slot.mode != current.mode:
                return slot.start
        return None


class ExpectedProfitSensor(_BaseSensor):
    """Sensor reporting the total expected profit for today from the schedule."""

    _attr_name = "sunSale Expected Profit Today"
    _attr_native_unit_of_measurement = "EUR"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash-plus"

    def __init__(self, coordinator, entry):
        """Initialise expected-profit sensor."""
        super().__init__(coordinator, entry, "expected_profit")

    @property
    def native_value(self) -> float:
        """Return the sum of expected_profit_eur across all today's schedule slots."""
        schedule = self._schedule
        if not schedule:
            return 0.0
        today = datetime.now(timezone.utc).date()
        return round(
            sum(s.expected_profit_eur for s in schedule.slots if s.start.date() == today),
            4,
        )


class DegradationCostSensor(_BaseSensor):
    """Sensor reporting the current battery wear cost per kWh cycled."""

    _attr_name = "sunSale Degradation Cost"
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery-minus"

    def __init__(self, coordinator, entry):
        """Initialise degradation-cost sensor."""
        super().__init__(coordinator, entry, "degradation_cost")

    @property
    def native_value(self) -> float | None:
        """Return the degradation cost in EUR/kWh."""
        if self.coordinator.data is None:
            return None
        return round(self.coordinator.data.get("degradation_cost", 0.0), 6)


class EstimatedCapacitySensor(_BaseSensor):
    """Sensor reporting the learned usable battery capacity in kWh."""

    _attr_name = "sunSale Estimated Battery Capacity"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        """Initialise estimated-capacity sensor."""
        super().__init__(coordinator, entry, "estimated_capacity")

    @property
    def native_value(self) -> float | None:
        """Return the CapacityEstimator's current best-estimate in kWh."""
        if self.coordinator.data is None:
            return None
        return round(self.coordinator.data.get("estimated_capacity", 0.0), 2)


class CurrentBuyPriceSensor(_BaseSensor):
    """Sensor reporting the effective buy price for the current pricing slot."""

    _attr_name = "sunSale Current Buy Price"
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:transmission-tower-import"

    def __init__(self, coordinator, entry):
        """Initialise current-buy-price sensor."""
        super().__init__(coordinator, entry, "current_buy_price")

    @property
    def native_value(self) -> float | None:
        """Return the current slot's buy_eur_kwh."""
        slot = self._current_price_slot()
        return round(slot.buy_eur_kwh, 4) if slot else None


class CurrentSellPriceSensor(_BaseSensor):
    """Sensor reporting the effective sell price for the current pricing slot."""

    _attr_name = "sunSale Current Sell Price"
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:transmission-tower-export"

    def __init__(self, coordinator, entry):
        """Initialise current-sell-price sensor."""
        super().__init__(coordinator, entry, "current_sell_price")

    @property
    def native_value(self) -> float | None:
        """Return the current slot's sell_eur_kwh."""
        slot = self._current_price_slot()
        return round(slot.sell_eur_kwh, 4) if slot else None


class ScheduleSensor(_BaseSensor):
    """Sensor exposing the full optimized battery schedule as extra attributes."""

    _attr_name = "sunSale Schedule"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator, entry):
        """Initialise schedule sensor."""
        super().__init__(coordinator, entry, "schedule")

    @property
    def native_value(self) -> str:
        """Return the current schedule slot's StorageMode string."""
        schedule = self._schedule
        if not schedule or not schedule.slots:
            return StorageMode.AUTO.value
        slot = self._current_slot()
        return slot.mode.value if slot else StorageMode.AUTO.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the full schedule list and profit summary as attributes."""
        schedule = self._schedule
        if not schedule:
            return {}
        return {
            "schedule": [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "mode": s.mode.value,
                    "power_kw": s.power_kw,
                    "expected_soc_after": round(s.expected_soc_after, 3),
                    "expected_profit_eur": round(s.expected_profit_eur, 4),
                    "reason": s.reason,
                }
                for s in schedule.slots
            ],
            "total_expected_profit_eur": round(schedule.total_expected_profit_eur, 4),
            "degradation_cost_per_kwh": round(schedule.degradation_cost_per_kwh, 6),
            "computed_at": schedule.computed_at.isoformat(),
        }


class InverterModeSensor(_BaseSensor):
    """Current inverter operating mode derived from Schedule and solar forecast.

    Recorded by HA for history so the dashboard's past mode band uses real data.
    """

    _attr_name = "sunSale Inverter Mode"
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise inverter-mode sensor."""
        super().__init__(coordinator, entry, "inverter_mode")

    @property
    def native_value(self) -> str:
        """Derive inverter mode for the current 15-min slot from Schedule and forecast.

        Returns:
            Mode string: charge_from_grid, sell_discharge, charge_solar,
            self_use_sell, or self_use.
        """
        data = self.coordinator.data
        if not data:
            return "idle"
        schedule: Schedule | None = data.get("schedule")
        forecast: GenerationSeries | None = data.get("forecast")
        load_kw: float = data.get("household_load_kw") or 0.0

        now = datetime.now(timezone.utc)
        slot_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        slot_end = slot_start + timedelta(minutes=15)

        cur_slot = None
        if schedule:
            cur_slot = next((s for s in schedule.slots if s.start <= now < s.end), None)

        solar_kwh = forecast.energy_between(slot_start, slot_end) if forecast else 0.0
        solar_w = solar_kwh / 0.25 * 1000.0
        load_w = load_kw * 1000.0

        if cur_slot is None:
            return "self_use_sell" if solar_w > load_w else "self_use"
        if cur_slot.mode == StorageMode.GridCharge:
            return "charge_from_grid"
        if cur_slot.mode in (StorageMode.Discharge, StorageMode.FeedIn):
            return "sell_discharge"
        if cur_slot.mode in (StorageMode.SelfUse, StorageMode.NoExport):
            return "charge_solar"
        return "self_use_sell" if solar_w > load_w else "self_use"


class DashboardSensor(_BaseSensor):
    """Aggregates pipeline outputs into a single sensor attribute bundle for the panel."""

    _attr_name = "sunSale Dashboard"
    _attr_icon = "mdi:chart-timeline-variant"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise dashboard sensor."""
        super().__init__(coordinator, entry, "dashboard")

    @property
    def native_value(self) -> str:
        """Return "ok" when data is available, otherwise "unavailable"."""
        return "ok" if self.coordinator.data else "unavailable"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return serialized pipeline outputs and battery summary for the panel."""
        if self.coordinator.data is None:
            return {}
        now = datetime.now(timezone.utc)
        bc = self.coordinator.battery_config
        config = {**self._entry.data, **self._entry.options}
        err: ForecastErrorSeries | None = self.coordinator.data.get("forecast_error")
        forecast_error_slots = [
            {
                "t": int(s.start.timestamp() * 1000),
                "forecast_kwh": round(s.forecast_kwh, 4),
                "observed_kwh": round(s.observed_kwh, 4),
                "error_kwh": round(s.error_kwh, 4),
            }
            for s in (err.slots if err else ())
        ]

        profile: ChargingProfile | None = self.coordinator.data.get("charging_profile")
        charging_profile_slots: list[dict[str, Any]] = []
        charging_profile_summary: dict[str, Any] | None = None
        if profile is not None:
            total_sell_kwh = 0.0
            for s in profile.slots:
                if s.mode is ChargeMode.SELL:
                    total_sell_kwh += s.expected_kwh
                charging_profile_slots.append({
                    "t": int(s.start.timestamp() * 1000),
                    "mode": s.mode.value,
                    "expected_kwh": round(s.expected_kwh, 4),
                    "sell_eur_kwh": round(s.sell_eur_kwh, 4),
                })
            charging_profile_summary = {
                "free_capacity_kwh": round(profile.free_capacity_kwh, 3),
                "today_remaining_generation_kwh": round(profile.today_remaining_generation_kwh, 3),
                "allocated_solar_kwh": round(profile.allocated_solar_kwh, 3),
                "total_sell_kwh": round(total_sell_kwh, 3),
                "total_no_export_kwh": round(profile.total_no_export_kwh, 3),
                "solar_exceeds_capacity": profile.solar_exceeds_capacity,
                "computed_at": profile.computed_at.isoformat(),
            }

        status = self.coordinator.data.get("battery_status")
        capacity_kwh = round(bc.nominal_capacity_kwh, 2) if bc else None
        soc_pct = round(status.soc * 100.0, 1) if status else None
        remaining_kwh = round(status.remaining_capacity_kwh, 2) if status else None
        power_kw = self.coordinator.data.get("battery_power_kw", 0.0)
        if power_kw is None or abs(power_kw) < 0.05:
            battery_state = "idle"
        elif power_kw > 0:
            battery_state = "charging"
        else:
            battery_state = "discharging"

        quality: ForecastQualityStore | None = self.coordinator.data.get("forecast_quality")
        sun_times: SunTimes | None = self.coordinator.data.get("sun_times")
        forecast_quality_data = None
        if quality is not None:
            forecast_quality_data = {
                "sunrise_utc": sun_times.today_sunrise.isoformat() if (sun_times and sun_times.today_sunrise) else None,
                "sunset_utc": sun_times.today_sunset.isoformat() if (sun_times and sun_times.today_sunset) else None,
                "group1": {k: v.metrics() for k, v in quality.group1.items()},
                "group2": {k: v.metrics() for k, v in quality.group2.items()},
                "group3": {k: v.metrics() for k, v in quality.group3.items()},
            }

        gen: GenerationSeries | None = self.coordinator.data.get("forecast")
        observed: ObservedGenerationSeries | None = self.coordinator.data.get("observed_generation")
        forecast_daily_kwh: dict[str, float] | None = None
        if gen is not None:
            forecast_daily_kwh = {
                "yesterday": round(gen.total_yesterday_kwh, 3),
                "today":     round(gen.total_today_kwh, 3),
                "tomorrow":  round(gen.total_tomorrow_kwh, 3),
                "d2":        round(gen.total_d2_kwh, 3),
                "d3":        round(gen.total_d3_kwh, 3),
                "d4":        round(gen.total_d4_kwh, 3),
                "d5":        round(gen.total_d5_kwh, 3),
                "d6":        round(gen.total_d6_kwh, 3),
            }

        schedule_obj: Schedule | None = self.coordinator.data.get("schedule")
        mode_history: InverterModeHistory | None = self.coordinator.data.get(
            "inverter_mode_history"
        )
        mode_reading: InverterModeReading | None = self.coordinator.data.get(
            "inverter_mode_reading"
        )
        inverter_mode_plan = (
            [
                {
                    "t": int(s.start.timestamp() * 1000),
                    "end_t": int(s.end.timestamp() * 1000),
                    "mode": s.mode.value,
                }
                for s in schedule_obj.slots
            ]
            if schedule_obj is not None
            else []
        )
        inverter_mode_history = (
            [
                {"t": int(s.timestamp.timestamp() * 1000), "mode": s.mode.value}
                for s in mode_history.samples
            ]
            if mode_history is not None
            else []
        )
        # Resolve "now" target from the schedule slot covering this instant.
        target_mode = None
        if schedule_obj is not None:
            cur = next(
                (s for s in schedule_obj.slots if s.start <= now < s.end), None,
            )
            if cur is not None:
                target_mode = cur.mode.value
        inverter_mode_now = {
            "observed": mode_reading.mode.value if mode_reading is not None else None,
            "target": target_mode,
            "automation_enabled": self.coordinator.automation_enabled,
        }

        return {
            "generated_at": now.isoformat(),
            "now_ts": int(now.timestamp() * 1000),
            "forecast_slots": _serialize_forecast_slots(self.coordinator.data.get("forecast")),
            "forecast_error_slots": forecast_error_slots,
            "battery_capacity_kwh": capacity_kwh,
            "battery_soc_pct": soc_pct,
            "battery_remaining_kwh": remaining_kwh,
            "battery_power_kw": round(power_kw, 3) if power_kw is not None else None,
            "battery_state": battery_state,
            "solar_energy_entity_id": config.get(CONF_INVERTER_ENTITY_SOLAR_ENERGY, ""),
            "charging_profile_slots": charging_profile_slots,
            "charging_profile_summary": charging_profile_summary,
            "forecast_quality": forecast_quality_data,
            "forecast_daily_kwh": forecast_daily_kwh,
            "actual_yesterday_kwh": round(observed.total_yesterday_kwh, 3) if observed else None,
            "actual_today_kwh": round(observed.total_today_so_far_kwh, 3) if observed else None,
            "inverter_mode_plan": inverter_mode_plan,
            "inverter_mode_history": inverter_mode_history,
            "inverter_mode_now": inverter_mode_now,
        }


class PricingPipelineSensor(_BaseSensor):
    """Diagnostic sensor — exposes full PriceSeries for chart rendering."""

    _attr_name = "sunSale Pricing"
    _attr_icon = "mdi:currency-eur"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise pricing pipeline diagnostic sensor."""
        super().__init__(coordinator, entry, "pricing_pipeline")

    @property
    def native_value(self) -> int:
        """Return the number of pricing slots available.

        Returns:
            Slot count, or 0 when no pricing data is present.
        """
        pricing: PriceSeries | None = (self.coordinator.data or {}).get("pricing")
        return len(pricing.slots) if pricing else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return full pricing slot data and summary statistics.

        Returns:
            Dict with resolution, computed_at, buy/sell min/max, and per-slot detail.
        """
        pricing: PriceSeries | None = (self.coordinator.data or {}).get("pricing")
        if not pricing:
            return {}
        buy_prices = [s.buy_eur_kwh for s in pricing.slots]
        sell_prices = [s.sell_eur_kwh for s in pricing.slots]
        return {
            "resolution_s": int(pricing.resolution.total_seconds()),
            "computed_at": pricing.computed_at.isoformat(),
            "negative_sell_count": sum(1 for s in pricing.slots if s.sell_eur_kwh <= 0),
            "min_buy": round(min(buy_prices), 4) if buy_prices else None,
            "max_buy": round(max(buy_prices), 4) if buy_prices else None,
            "min_sell": round(min(sell_prices), 4) if sell_prices else None,
            "max_sell": round(max(sell_prices), 4) if sell_prices else None,
            "slots": [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "buy_eur_kwh": round(s.buy_eur_kwh, 4),
                    "sell_eur_kwh": round(s.sell_eur_kwh, 4),
                    "spot_eur_kwh": round(s.spot_eur_kwh, 4),
                }
                for s in pricing.slots
            ],
        }


class ForecastPipelineSensor(_BaseSensor):
    """Diagnostic sensor — exposes full GenerationSeries for chart rendering."""

    _attr_name = "sunSale Forecast"
    _attr_icon = "mdi:solar-panel"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise forecast pipeline diagnostic sensor."""
        super().__init__(coordinator, entry, "forecast_pipeline")

    @property
    def native_value(self) -> float:
        """Return total expected solar generation for today in kWh.

        Returns:
            Sum of expected_kwh across today's forecast slots, or 0.0 when unavailable.
        """
        gen: GenerationSeries | None = (self.coordinator.data or {}).get("forecast")
        if not gen:
            return 0.0
        today = datetime.now(timezone.utc).date()
        return round(
            sum(s.expected_kwh for s in gen.slots if s.start.date() == today),
            2,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return full forecast slot data and per-day totals.

        Returns:
            Dict with daily kWh totals and per-slot detail.
        """
        gen: GenerationSeries | None = (self.coordinator.data or {}).get("forecast")
        if not gen:
            return {}
        return {
            "total_yesterday_kwh": round(gen.total_yesterday_kwh, 4),
            "total_today_kwh": round(gen.total_today_kwh, 4),
            "total_tomorrow_kwh": round(gen.total_tomorrow_kwh, 4),
            "total_d2_kwh": round(gen.total_d2_kwh, 4),
            "total_d3_kwh": round(gen.total_d3_kwh, 4),
            "total_d4_kwh": round(gen.total_d4_kwh, 4),
            "total_d5_kwh": round(gen.total_d5_kwh, 4),
            "total_d6_kwh": round(gen.total_d6_kwh, 4),
            "slots": [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "expected_kwh": round(s.expected_kwh, 4),
                }
                for s in gen.slots
            ],
        }


class CalculationPipelineSensor(_BaseSensor):
    """Diagnostic sensor — exposes CalculationResult (lockout windows, etc.)."""

    _attr_name = "sunSale Calculation"
    _attr_icon = "mdi:calculator"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise calculation pipeline diagnostic sensor."""
        super().__init__(coordinator, entry, "calculation_pipeline")

    @property
    def native_value(self) -> int:
        """Return the number of active feed-in lockout windows.

        Returns:
            Count of lockout windows, or 0 when no calculation data is present.
        """
        calc: CalculationResult | None = (self.coordinator.data or {}).get("calculation")
        return len(calc.feed_in_lockout_windows) if calc else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return full calculation result including lockout windows and slot detail.

        Returns:
            Dict with computed_at, negative sale total, lockout windows, and per-slot data.
        """
        calc: CalculationResult | None = (self.coordinator.data or {}).get("calculation")
        if not calc:
            return {}
        return {
            "computed_at": calc.computed_at.isoformat(),
            "total_negative_sale_kwh": round(calc.total_negative_sale_kwh, 4),
            "feed_in_lockout_windows": [
                {"start": w[0].isoformat(), "end": w[1].isoformat()}
                for w in calc.feed_in_lockout_windows
            ],
            "slots": [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "expected_solar_kwh": round(s.expected_solar_kwh, 4),
                    "expected_solar_negative_sale_kwh": round(s.expected_solar_negative_sale_kwh, 4),
                    "notes": list(s.notes),
                }
                for s in calc.slots
            ],
        }


class _BaseloadSensor(_BaseSensor):
    """Mixin: pull BaseLoadProfile / BatteryRuntimeEstimate from coordinator data."""

    @property
    def _profile(self) -> BaseLoadProfile | None:
        """Return the current BaseLoadProfile from coordinator data, or None."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("base_load_profile")

    @property
    def _runtime(self) -> BatteryRuntimeEstimate | None:
        """Return the current BatteryRuntimeEstimate from coordinator data, or None."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("battery_runtime")


class CurrentBaseloadSensor(_BaseloadSensor):
    """Sensor reporting the estimated household base load at the current time slot."""

    _attr_name = "sunSale Current Baseload"
    _attr_native_unit_of_measurement = "kW"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:home-lightning-bolt"

    def __init__(self, coordinator, entry):
        """Initialise current baseload sensor."""
        super().__init__(coordinator, entry, "current_baseload")

    @property
    def native_value(self) -> float | None:
        """Return estimated household base load for the current hour in kW.

        Returns:
            Base load kW rounded to 3 dp, or None when the profile is unavailable.
        """
        profile = self._profile
        if profile is None:
            return None
        local_tz = self.coordinator._sun_sale_config.local_tz
        return round(profile.at(datetime.now(timezone.utc), local_tz), 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return full baseload profile statistics and hourly slot data.

        Returns:
            Dict with fallback_kw, percentiles, confidence, sample counts, and hourly slots.
        """
        profile = self._profile
        if profile is None:
            return {}
        return {
            "fallback_kw": round(profile.fallback_kw, 3),
            "overall_p10_kw": round(profile.overall_p10_kw, 3),
            "overall_median_kw": round(profile.overall_median_kw, 3),
            "confidence": profile.confidence,
            "sample_count": profile.sample_count,
            "distinct_days": profile.distinct_days,
            "slots": [
                {
                    "hour": s.hour,
                    "baseload_kw": round(s.baseload_kw, 3),
                    "sample_count": s.sample_count,
                    "is_fallback": s.is_fallback,
                }
                for s in profile.slots
            ],
        }


class BatteryRuntimeMinutesSensor(_BaseloadSensor):
    """Sensor reporting estimated minutes until the battery is depleted at current load."""

    _attr_name = "sunSale Battery Runtime"
    _attr_native_unit_of_measurement = "min"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer-sand"

    def __init__(self, coordinator, entry):
        """Initialise battery runtime minutes sensor."""
        super().__init__(coordinator, entry, "battery_runtime_minutes")

    @property
    def native_value(self) -> float | None:
        """Return estimated battery runtime in minutes.

        Returns:
            Runtime minutes rounded to 1 dp, or None when unavailable.
        """
        runtime = self._runtime
        if runtime is None or runtime.runtime_minutes is None:
            return None
        return round(runtime.runtime_minutes, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return battery runtime estimate detail.

        Returns:
            Dict with usable remaining kWh, average drain rate, horizon, and computed_at.
        """
        runtime = self._runtime
        if runtime is None:
            return {}
        return {
            "remaining_kwh_usable": round(runtime.remaining_kwh_usable, 3),
            "avg_drain_kw_next_hour": round(runtime.avg_drain_kw_next_hour, 3),
            "horizon_hours": runtime.horizon_hours,
            "computed_at": runtime.computed_at.isoformat(),
        }


class BatteryDrainUntilSensor(_BaseloadSensor):
    """Sensor reporting the projected timestamp at which the battery will be depleted."""

    _attr_name = "sunSale Battery Drain Until"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, entry):
        """Initialise battery drain-until timestamp sensor."""
        super().__init__(coordinator, entry, "battery_drain_until")

    @property
    def native_value(self) -> datetime | None:
        """Return the projected battery depletion timestamp.

        Returns:
            Timezone-aware datetime when the battery is expected to reach min SoC,
            or None when the estimate is unavailable.
        """
        runtime = self._runtime
        return runtime.until if runtime else None


class BaseloadConfidenceSensor(_BaseloadSensor):
    """Sensor reporting the confidence score of the current base load profile."""

    _attr_name = "sunSale Baseload Confidence"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:gauge"

    def __init__(self, coordinator, entry):
        """Initialise baseload confidence sensor."""
        super().__init__(coordinator, entry, "baseload_confidence")

    @property
    def native_value(self) -> float | None:
        """Return the baseload profile confidence score.

        Returns:
            Confidence value in [0, 1] rounded to 3 dp, or None when unavailable.
        """
        profile = self._profile
        if profile is None or profile.confidence is None:
            return None
        return round(profile.confidence, 3)


class MonthlyBillSensor(_BaseSensor):
    """Sensor reporting the net electricity bill for the current calendar month.

    The value is carry (month start → yesterday midnight) plus the live
    yday-to-now portion derived from grid power history and current prices.
    Resets to zero at the start of each new calendar month.
    """

    _attr_name = "sunSale Monthly Bill"
    _attr_native_unit_of_measurement = "EUR"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:currency-eur"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise monthly bill sensor."""
        super().__init__(coordinator, entry, "monthly_bill")

    @property
    def native_value(self) -> float | None:
        """Return the net electricity bill for the current month in EUR.

        Returns:
            Total month bill rounded to 4 dp, or None when no data is available.
        """
        result: MonthlyBillResult | None = (self.coordinator.data or {}).get("monthly_bill")
        if result is None:
            return None
        return round(result.total_month_eur, 4)

    @property
    def last_reset(self) -> datetime:
        """Return the start of the current calendar month in UTC.

        Returns:
            Timezone-aware datetime for midnight on the 1st of the current month.
        """
        now = datetime.now(timezone.utc)
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return bill breakdown: carry, yday-to-now, month, and per-slot detail.

        Returns:
            Dict with month_str, carry_eur, yday_to_now_eur, total_month_eur,
            slot_count, computed_at, and per-slot import/export/cost data.
        """
        result: MonthlyBillResult | None = (self.coordinator.data or {}).get("monthly_bill")
        if result is None:
            return {}
        return {
            "month_str": result.month_str,
            "carry_eur": round(result.carry_eur, 4),
            "yday_to_now_eur": round(result.yday_to_now_eur, 4),
            "total_month_eur": round(result.total_month_eur, 4),
            "previous_month_str": result.previous_month_str,
            "previous_month_eur": round(result.previous_month_eur, 4),
            "slot_count": len(result.slots),
            "computed_at": result.computed_at.isoformat(),
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
                for s in result.slots
            ],
        }
