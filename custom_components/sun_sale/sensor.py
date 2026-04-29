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

from .const import DOMAIN
from .coordinator import SunSaleCoordinator
from .models import Action, EVSchedule, Schedule


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
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
        EVChargingSensor(coordinator, entry),
        EVChargeCostSensor(coordinator, entry),
        ScheduleSensor(coordinator, entry),
        DashboardSensor(coordinator, entry),
        InverterModeSensor(coordinator, entry),
    ])


class _BaseSensor(CoordinatorEntity, SensorEntity):
    def __init__(
        self, coordinator: SunSaleCoordinator, entry: ConfigEntry, key: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._entry = entry

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "sunSale",
            "manufacturer": "sunSale",
        }

    @property
    def _schedule(self) -> Schedule | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("schedule")

    @property
    def _ev_schedule(self) -> EVSchedule | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("ev_schedule")

    def _current_slot(self):
        schedule = self._schedule
        if not schedule or not schedule.slots:
            return None
        now = datetime.now(timezone.utc)
        return next(
            (s for s in schedule.slots if s.start <= now < s.end),
            schedule.slots[0],
        )

    def _current_tariff(self):
        if self.coordinator.data is None:
            return None
        tariffs = self.coordinator.data.get("tariffs", [])
        if not tariffs:
            return None
        now = datetime.now(timezone.utc)
        return next(
            (t for t in tariffs if t.hour <= now < t.hour + timedelta(hours=1)),
            tariffs[0],
        )


class CurrentActionSensor(_BaseSensor):
    _attr_name = "sunSale Current Action"
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "current_action")

    @property
    def native_value(self) -> str:
        slot = self._current_slot()
        return slot.action.value if slot else Action.IDLE.value


class NextActionSensor(_BaseSensor):
    _attr_name = "sunSale Next Action"
    _attr_icon = "mdi:lightning-bolt-outline"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "next_action")

    @property
    def native_value(self) -> str:
        schedule = self._schedule
        if not schedule or not schedule.slots:
            return Action.IDLE.value
        now = datetime.now(timezone.utc)
        current = self._current_slot()
        if current is None:
            return Action.IDLE.value
        for slot in schedule.slots:
            if slot.start > now and slot.action != current.action:
                return slot.action.value
        return current.action.value


class NextActionTimeSensor(_BaseSensor):
    _attr_name = "sunSale Next Action Time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "next_action_time")

    @property
    def native_value(self) -> datetime | None:
        schedule = self._schedule
        if not schedule or not schedule.slots:
            return None
        now = datetime.now(timezone.utc)
        current = self._current_slot()
        if current is None:
            return None
        for slot in schedule.slots:
            if slot.start > now and slot.action != current.action:
                return slot.start
        return None


class ExpectedProfitSensor(_BaseSensor):
    _attr_name = "sunSale Expected Profit Today"
    _attr_native_unit_of_measurement = "EUR"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash-plus"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "expected_profit")

    @property
    def native_value(self) -> float:
        schedule = self._schedule
        if not schedule:
            return 0.0
        today = datetime.now(timezone.utc).date()
        return round(
            sum(s.expected_profit_eur for s in schedule.slots if s.start.date() == today),
            4,
        )


class DegradationCostSensor(_BaseSensor):
    _attr_name = "sunSale Degradation Cost"
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery-minus"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "degradation_cost")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return round(self.coordinator.data.get("degradation_cost", 0.0), 6)


class EstimatedCapacitySensor(_BaseSensor):
    _attr_name = "sunSale Estimated Battery Capacity"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "estimated_capacity")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return round(self.coordinator.data.get("estimated_capacity", 0.0), 2)


class CurrentBuyPriceSensor(_BaseSensor):
    _attr_name = "sunSale Current Buy Price"
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:transmission-tower-import"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "current_buy_price")

    @property
    def native_value(self) -> float | None:
        t = self._current_tariff()
        return round(t.buy_price, 4) if t else None


class CurrentSellPriceSensor(_BaseSensor):
    _attr_name = "sunSale Current Sell Price"
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:transmission-tower-export"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "current_sell_price")

    @property
    def native_value(self) -> float | None:
        t = self._current_tariff()
        return round(t.sell_price, 4) if t else None


class EVChargingSensor(_BaseSensor):
    _attr_name = "sunSale EV Charging"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "ev_charging")

    @property
    def native_value(self) -> str:
        ev_schedule = self._ev_schedule
        if not ev_schedule or not ev_schedule.slots:
            return "off"
        now = datetime.now(timezone.utc)
        current = next(
            (s for s in ev_schedule.slots if s.start <= now < s.end),
            None,
        )
        return "on" if current and current.charge_power_kw > 0 else "off"


class EVChargeCostSensor(_BaseSensor):
    _attr_name = "sunSale EV Charge Cost"
    _attr_native_unit_of_measurement = "EUR"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "ev_charge_cost")

    @property
    def native_value(self) -> float:
        ev_schedule = self._ev_schedule
        return round(ev_schedule.total_cost_eur, 4) if ev_schedule else 0.0


class ScheduleSensor(_BaseSensor):
    _attr_name = "sunSale Schedule"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "schedule")

    @property
    def native_value(self) -> str:
        schedule = self._schedule
        if not schedule or not schedule.slots:
            return Action.IDLE.value
        slot = self._current_slot()
        return slot.action.value if slot else Action.IDLE.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        schedule = self._schedule
        if not schedule:
            return {}
        return {
            "schedule": [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "action": s.action.value,
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
    """Current inverter operating mode as a string — recorded by HA for history.

    State mirrors the inverter_mode field that build_future_slots computes for
    the current 15-min slot, so the dashboard's past mode band uses real data.
    """

    _attr_name = "sunSale Inverter Mode"
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "inverter_mode")

    @property
    def native_value(self) -> str:
        if self.coordinator.data is None:
            return "idle"
        slots: list[dict] = self.coordinator.data.get("dashboard_slots", [])
        if not slots:
            return "idle"
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        slot_ms = 15 * 60 * 1000
        cur = next((s for s in slots if s["t"] <= now_ms < s["t"] + slot_ms), None)
        if cur is None:
            cur = slots[0]
        return cur.get("inverter_mode", "idle")


class DashboardSensor(_BaseSensor):
    """Exposes pre-built future slots and frozen solar forecast for the panel."""

    _attr_name = "sunSale Dashboard"
    _attr_icon = "mdi:chart-timeline-variant"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "dashboard")

    @property
    def native_value(self) -> str:
        return "ok" if self.coordinator.data else "unavailable"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        now = datetime.now(timezone.utc)
        bc = self.coordinator.battery_config
        return {
            "generated_at": now.isoformat(),
            "now_ts": int(now.timestamp() * 1000),
            "slots": self.coordinator.data.get("dashboard_slots", []),
            "solar_frozen_forecast": self.coordinator.data.get("solar_frozen_forecast", []),
            "battery_capacity_kwh": round(bc.nominal_capacity_kwh, 2) if bc else None,
        }
