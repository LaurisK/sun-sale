"""sunSale DataUpdateCoordinator.

Reads all HA state, runs the optimizer, executes inverter/EV commands,
and feeds the capacity estimator.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import battery as battery_module
from . import ev_scheduler, optimizer, tariff
from .battery import CapacityEstimator
from .const import (
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_NOMINAL_CAPACITY,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_PURCHASE_PRICE,
    CONF_BATTERY_RATED_CYCLE_LIFE,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY,
    CONF_EV_BATTERY_CAPACITY,
    CONF_EV_ENABLED,
    CONF_EV_ENTITY_CHARGER_SWITCH,
    CONF_EV_ENTITY_DEPARTURE_TIME,
    CONF_EV_ENTITY_PLUG_STATE,
    CONF_EV_ENTITY_SOC,
    CONF_EV_ENTITY_TARGET_SOC,
    CONF_EV_MAX_CHARGE_POWER,
    CONF_EV_MIN_CHARGE_POWER,
    CONF_EV_PLATFORM,
    CONF_INVERTER_ENTITY_BATTERY_POWER,
    CONF_INVERTER_ENTITY_BATTERY_SOC,
    CONF_INVERTER_ENTITY_CHARGE_CONTROL,
    CONF_INVERTER_ENTITY_GRID_POWER,
    CONF_INVERTER_PLATFORM,
    CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH,
    CONF_INVERTER_SOLIS_CHARGE_CURRENT,
    CONF_INVERTER_SOLIS_CHARGE_END_TIME_1,
    CONF_INVERTER_SOLIS_CHARGE_START_TIME_1,
    CONF_INVERTER_SOLIS_DISCHARGE_CURRENT,
    CONF_INVERTER_SOLIS_DISCHARGE_END_TIME_1,
    CONF_INVERTER_SOLIS_DISCHARGE_START_TIME_1,
    CONF_INVERTER_SOLIS_SELF_USE_MODE_SWITCH,
    CONF_INVERTER_SOLIS_TOU_MODE_SWITCH,
    CONF_NORDPOOL_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_TARIFF_DISTRIBUTION_FEE,
    CONF_TARIFF_MARKUP,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE,
    CONF_TARIFF_SELL_MARKUP,
    CONF_TARIFF_SELL_TAX_RATE,
    CONF_TARIFF_TAX_RATE,
    DEFAULT_BATTERY_NOMINAL_VOLTAGE,
    DEFAULT_EV_MIN_CHARGE_POWER_KW,
    DEFAULT_EV_TARGET_SOC,
    DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH,
    DEFAULT_SOLIS_CHARGE_CURRENT,
    DEFAULT_SOLIS_CHARGE_END_TIME_1,
    DEFAULT_SOLIS_CHARGE_START_TIME_1,
    DEFAULT_SOLIS_DISCHARGE_CURRENT,
    DEFAULT_SOLIS_DISCHARGE_END_TIME_1,
    DEFAULT_SOLIS_DISCHARGE_START_TIME_1,
    DEFAULT_SOLIS_SELF_USE_MODE_SWITCH,
    DEFAULT_SOLIS_TOU_MODE_SWITCH,
    DOMAIN,
    STORAGE_KEY_CAPACITY,
    STORAGE_VERSION,
    UPDATE_INTERVAL_MINUTES,
)
from .ev_charger import EVChargerController, EVChargerPlatform
from .inverter import InverterController, InverterPlatform
from .models import (
    Action,
    BatteryConfig,
    BatteryState,
    CapacityObservation,
    EVChargerConfig,
    EVChargerState,
    EVSchedule,
    HourlyPrice,
    Schedule,
    SolarForecast,
    TariffConfig,
)

_LOGGER = logging.getLogger(__name__)


class SunSaleCoordinator(DataUpdateCoordinator):
    """Central coordinator: reads HA state, runs optimizer, executes commands."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self._entry = config_entry
        self._store: Store | None = None
        self._inverter: InverterController | None = None
        self._ev_charger: EVChargerController | None = None
        self._tariff_config: TariffConfig | None = None
        self._battery_config: BatteryConfig | None = None
        self._ev_config: EVChargerConfig | None = None
        self._capacity_estimator: CapacityEstimator | None = None
        self._last_action: str | None = None
        self._last_battery_soc: float | None = None
        self._last_battery_power: float | None = None
        self.automation_enabled: bool = True
        self.last_dispatched_action: str | None = None
        self.last_dispatched_at: datetime | None = None

    async def async_setup(self) -> None:
        """Initialise from config entry data."""
        data = {**self._entry.data, **self._entry.options}

        self._tariff_config = TariffConfig(
            distribution_fee=data[CONF_TARIFF_DISTRIBUTION_FEE],
            tax_rate=data[CONF_TARIFF_TAX_RATE] / 100.0,
            markup=data[CONF_TARIFF_MARKUP],
            sell_distribution_fee=data[CONF_TARIFF_SELL_DISTRIBUTION_FEE],
            sell_tax_rate=data[CONF_TARIFF_SELL_TAX_RATE] / 100.0,
            sell_markup=data[CONF_TARIFF_SELL_MARKUP],
        )

        self._battery_config = BatteryConfig(
            nominal_capacity_kwh=data[CONF_BATTERY_NOMINAL_CAPACITY],
            purchase_price_eur=data[CONF_BATTERY_PURCHASE_PRICE],
            rated_cycle_life=data[CONF_BATTERY_RATED_CYCLE_LIFE],
            max_charge_power_kw=data[CONF_BATTERY_MAX_CHARGE_POWER],
            max_discharge_power_kw=data[CONF_BATTERY_MAX_DISCHARGE_POWER],
            min_soc=data[CONF_BATTERY_MIN_SOC] / 100.0,
            max_soc=data[CONF_BATTERY_MAX_SOC] / 100.0,
            round_trip_efficiency=data[CONF_BATTERY_ROUND_TRIP_EFFICIENCY] / 100.0,
            nominal_voltage_v=data.get(CONF_BATTERY_NOMINAL_VOLTAGE, DEFAULT_BATTERY_NOMINAL_VOLTAGE),
        )

        inverter_platform = InverterPlatform(data[CONF_INVERTER_PLATFORM])
        if inverter_platform == InverterPlatform.SOLIS:
            entity_ids = {
                "battery_soc": data[CONF_INVERTER_ENTITY_BATTERY_SOC],
                "battery_power": data[CONF_INVERTER_ENTITY_BATTERY_POWER],
                "grid_power": data[CONF_INVERTER_ENTITY_GRID_POWER],
                "solis_charge_current": data.get(CONF_INVERTER_SOLIS_CHARGE_CURRENT, DEFAULT_SOLIS_CHARGE_CURRENT),
                "solis_discharge_current": data.get(CONF_INVERTER_SOLIS_DISCHARGE_CURRENT, DEFAULT_SOLIS_DISCHARGE_CURRENT),
                "solis_charge_start_time_1": data.get(CONF_INVERTER_SOLIS_CHARGE_START_TIME_1, DEFAULT_SOLIS_CHARGE_START_TIME_1),
                "solis_charge_end_time_1": data.get(CONF_INVERTER_SOLIS_CHARGE_END_TIME_1, DEFAULT_SOLIS_CHARGE_END_TIME_1),
                "solis_discharge_start_time_1": data.get(CONF_INVERTER_SOLIS_DISCHARGE_START_TIME_1, DEFAULT_SOLIS_DISCHARGE_START_TIME_1),
                "solis_discharge_end_time_1": data.get(CONF_INVERTER_SOLIS_DISCHARGE_END_TIME_1, DEFAULT_SOLIS_DISCHARGE_END_TIME_1),
                "solis_tou_mode_switch": data.get(CONF_INVERTER_SOLIS_TOU_MODE_SWITCH, DEFAULT_SOLIS_TOU_MODE_SWITCH),
                "solis_allow_grid_charge_switch": data.get(CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH, DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH),
                "solis_self_use_mode_switch": data.get(CONF_INVERTER_SOLIS_SELF_USE_MODE_SWITCH, DEFAULT_SOLIS_SELF_USE_MODE_SWITCH),
            }
        else:
            entity_ids = {
                "battery_soc": data[CONF_INVERTER_ENTITY_BATTERY_SOC],
                "battery_power": data[CONF_INVERTER_ENTITY_BATTERY_POWER],
                "grid_power": data[CONF_INVERTER_ENTITY_GRID_POWER],
                "charge_control": data[CONF_INVERTER_ENTITY_CHARGE_CONTROL],
            }
        self._inverter = InverterController(
            self.hass,
            inverter_platform,
            entity_ids,
            self._battery_config,
        )

        if data.get(CONF_EV_ENABLED, False):
            ev_platform = EVChargerPlatform(data[CONF_EV_PLATFORM])
            self._ev_config = EVChargerConfig(
                max_charge_power_kw=data[CONF_EV_MAX_CHARGE_POWER],
                min_charge_power_kw=data.get(CONF_EV_MIN_CHARGE_POWER, DEFAULT_EV_MIN_CHARGE_POWER_KW),
                battery_capacity_kwh=data[CONF_EV_BATTERY_CAPACITY],
            )
            self._ev_charger = EVChargerController(
                self.hass,
                ev_platform,
                {
                    "plug_state": data.get(CONF_EV_ENTITY_PLUG_STATE, ""),
                    "soc": data.get(CONF_EV_ENTITY_SOC, ""),
                    "charger_switch": data.get(CONF_EV_ENTITY_CHARGER_SWITCH, ""),
                },
            )

        self._store = Store(self.hass, STORAGE_VERSION, STORAGE_KEY_CAPACITY)
        stored = await self._store.async_load()
        if stored:
            self._capacity_estimator = CapacityEstimator.from_dict(stored)
        else:
            self._capacity_estimator = CapacityEstimator(
                self._battery_config.nominal_capacity_kwh
            )

    async def _async_update_data(self) -> dict:
        """Main 5-minute update cycle."""
        now = datetime.now(timezone.utc)

        try:
            prices = self._read_nordpool_prices()
            solar = self._read_solar_forecast()
            soc = self._inverter.get_battery_soc()
            battery_power = self._inverter.get_battery_power()
            grid_power = self._inverter.get_grid_power()

            battery_state = BatteryState(
                soc=soc,
                estimated_capacity_kwh=self._capacity_estimator.estimated_capacity_kwh,
            )

            tariffs = tariff.compute_tariffs(prices, self._tariff_config)
            deg_cost = battery_module.degradation_cost_per_kwh(
                self._battery_config, battery_state
            )

            schedule: Schedule = optimizer.optimize_schedule(
                tariffs=tariffs,
                solar_forecast=solar,
                battery_config=self._battery_config,
                battery_state=battery_state,
                degradation_cost=deg_cost,
                now=now,
            )

            ev_state = None
            ev_schedule: EVSchedule | None = None
            if self._ev_charger is not None and self._ev_config is not None:
                ev_state = self._read_ev_state()
                ev_schedule = ev_scheduler.schedule_ev_charge(
                    tariffs=tariffs,
                    ev_config=self._ev_config,
                    ev_state=ev_state,
                    now=now,
                )

            if self.automation_enabled:
                await self._execute_current_action(schedule, now)
                if ev_schedule is not None:
                    await self._execute_current_ev_action(ev_schedule, now)

            obs = self._build_capacity_observation(soc, battery_power, now)
            if obs is not None:
                self._capacity_estimator.add_observation(obs)
                if self._store is not None:
                    await self._store.async_save(self._capacity_estimator.to_dict())

            self._last_battery_soc = soc
            self._last_battery_power = battery_power

            return {
                "schedule": schedule,
                "ev_schedule": ev_schedule,
                "tariffs": tariffs,
                "battery_state": battery_state,
                "degradation_cost": deg_cost,
                "estimated_capacity": self._capacity_estimator.estimated_capacity_kwh,
                "prices": prices,
                "solar_forecast": solar,
                "grid_power_kw": grid_power,
                "battery_power_kw": battery_power,
                "ev_state": ev_state,
            }

        except Exception as exc:
            raise UpdateFailed(f"Error updating sunSale data: {exc}") from exc

    # ------------------------------------------------------------------ #
    # State readers                                                        #
    # ------------------------------------------------------------------ #

    def _read_nordpool_prices(self) -> list[HourlyPrice]:
        """Parse Nordpool sensor today/tomorrow price attributes."""
        entity_id = self._entry.data.get(CONF_NORDPOOL_ENTITY, "")
        state = self.hass.states.get(entity_id)
        if state is None:
            _LOGGER.warning("Nordpool entity '%s' not found", entity_id)
            return []

        now = datetime.now(timezone.utc)
        prices: list[HourlyPrice] = []

        for offset, attr_key in enumerate(("today", "tomorrow")):
            raw = state.attributes.get(attr_key)
            if not isinstance(raw, list):
                continue
            base_date = (now + timedelta(days=offset)).date()
            for hour_idx, price in enumerate(raw):
                if price is None:
                    continue
                start = datetime(
                    base_date.year, base_date.month, base_date.day,
                    hour_idx, 0, 0, tzinfo=timezone.utc,
                )
                prices.append(HourlyPrice(
                    start=start,
                    end=start + timedelta(hours=1),
                    price_eur_kwh=float(price),
                ))

        return prices

    def _read_solar_forecast(self) -> list[SolarForecast]:
        """Parse Forecast.Solar / Solcast forecast attribute."""
        entity_id = self._entry.data.get(CONF_SOLAR_FORECAST_ENTITY, "")
        if not entity_id:
            return []
        state = self.hass.states.get(entity_id)
        if state is None:
            return []

        forecasts: list[SolarForecast] = []
        for entry in state.attributes.get("forecast", []):
            try:
                start = datetime.fromisoformat(entry["time"]).replace(tzinfo=timezone.utc)
                kwh = float(entry.get("pv_estimate", entry.get("energy", 0.0)))
                forecasts.append(SolarForecast(
                    start=start,
                    end=start + timedelta(hours=1),
                    generation_kwh=kwh,
                ))
            except (KeyError, ValueError):
                continue
        return forecasts

    def _read_ev_state(self) -> EVChargerState:
        """Build EVChargerState from HA entity states."""
        is_plugged = self._ev_charger.is_plugged_in()
        soc = self._ev_charger.get_ev_soc()

        target_soc = DEFAULT_EV_TARGET_SOC
        target_entity = self._entry.data.get(CONF_EV_ENTITY_TARGET_SOC, "")
        if target_entity:
            ts = self.hass.states.get(target_entity)
            if ts and ts.state not in ("unavailable", "unknown", ""):
                try:
                    val = float(ts.state)
                    target_soc = val / 100.0 if val > 1.0 else val
                except ValueError:
                    pass

        departure_time: datetime | None = None
        dep_entity = self._entry.data.get(CONF_EV_ENTITY_DEPARTURE_TIME, "")
        if dep_entity:
            ds = self.hass.states.get(dep_entity)
            if ds and ds.state not in ("unavailable", "unknown", ""):
                try:
                    departure_time = datetime.fromisoformat(ds.state).replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    pass

        return EVChargerState(
            is_plugged_in=is_plugged,
            soc=soc if soc is not None else 0.5,
            target_soc=target_soc,
            departure_time=departure_time,
        )

    # ------------------------------------------------------------------ #
    # Command executors                                                    #
    # ------------------------------------------------------------------ #

    async def _execute_current_action(self, schedule: Schedule, now: datetime) -> None:
        """Execute the action for the current hour (with deduplication)."""
        if not schedule.slots:
            return

        current_slot = next(
            (s for s in schedule.slots if s.start <= now < s.end),
            schedule.slots[0],
        )
        action_key = f"{current_slot.action.value}:{current_slot.power_kw:.3f}"
        if action_key == self._last_action:
            return

        if current_slot.action == Action.CHARGE_FROM_GRID:
            await self._inverter.async_charge_from_grid(current_slot.power_kw)
        elif current_slot.action == Action.DISCHARGE_TO_GRID:
            await self._inverter.async_discharge_to_grid(current_slot.power_kw)
        else:
            await self._inverter.async_idle()

        self._last_action = action_key
        self.last_dispatched_action = current_slot.action.value
        self.last_dispatched_at = now
        _LOGGER.info("sunSale: %s", current_slot.reason)

    async def _execute_current_ev_action(
        self, ev_schedule: EVSchedule, now: datetime
    ) -> None:
        """Execute EV charging action for the current hour."""
        if not ev_schedule.slots:
            return
        current_slot = next(
            (s for s in ev_schedule.slots if s.start <= now < s.start + timedelta(hours=1)),
            None,
        )
        if current_slot is None:
            return
        if current_slot.charge_power_kw > 0:
            await self._ev_charger.async_start_charging(current_slot.charge_power_kw)
        else:
            await self._ev_charger.async_stop_charging()

    def _build_capacity_observation(
        self,
        current_soc: float,
        current_power_kw: float,
        now: datetime,
    ) -> CapacityObservation | None:
        """Build a capacity observation from SoC change, if significant enough."""
        if self._last_battery_soc is None or self._last_battery_power is None:
            return None
        soc_delta = abs(current_soc - self._last_battery_soc)
        if soc_delta < 0.05:
            return None
        avg_power = (abs(self._last_battery_power) + abs(current_power_kw)) / 2.0
        energy_kwh = avg_power * (UPDATE_INTERVAL_MINUTES / 60.0)
        direction = "charge" if current_soc > self._last_battery_soc else "discharge"
        return CapacityObservation(
            timestamp=now,
            soc_start=self._last_battery_soc,
            soc_end=current_soc,
            energy_kwh=energy_kwh,
            direction=direction,
        )
