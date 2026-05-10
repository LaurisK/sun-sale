"""sunSale DataUpdateCoordinator — thin orchestrator for the DAG pipeline.

Responsibilities:
  1. Build translators, DAG nodes, engine, and event router from config.
  2. Manage CapacityEstimator state across cycles (pre-DAG update + persistence).
  3. Run translators (parallel) → deposit EstimatedCapacity → run engine.
  4. Route emitted ControlEvents to output adapters when automation is enabled.
  5. Map typed DAG outputs to the string-keyed coordinator.data dict for sensors.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

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
    CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD,
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
    CONF_NORDPOOL_RESOLUTION,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY_2,
    CONF_TARIFF_DISTRIBUTION_FEE,
    CONF_TARIFF_MARKUP,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE,
    CONF_TARIFF_SELL_MARKUP,
    CONF_TARIFF_SELL_TAX_RATE,
    CONF_TARIFF_TAX_RATE,
    CAPACITY_OBS_MIN_SOC_DELTA,
    DEFAULT_BATTERY_NOMINAL_VOLTAGE,
    DEFAULT_EV_MIN_CHARGE_POWER_KW,
    DEFAULT_NORDPOOL_RESOLUTION,
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
from .dag_engine import DagEngine, SunSaleConfig, run_translators
from .event_router import EventRouter
from .ev_charger import EVChargerController, EVChargerPlatform
from .inverter import InverterController, InverterPlatform
from .models import (
    BatteryConfig,
    BatteryReading,
    BatteryState,
    CalculationResult,
    CapacityObservation,
    DashboardData,
    DegradationCost,
    EstimatedCapacity,
    EVChargerConfig,
    EVChargerState,
    EVSchedule,
    GenerationSeries,
    NordpoolPrices,
    PriceSeries,
    RawSolarData,
    Schedule,
    TariffConfig,
)
from .nodes import (
    BatteryStateNode,
    DashboardNode,
    DegradationNode,
    EVSchedulerNode,
    GenerationNode,
    LockoutNode,
    OptimizerNode,
    PricingNode,
    make_last_ref,
)
from .translators import (
    BatteryTranslator,
    EVTranslator,
    NordpoolTranslator,
    SolarTranslator,
)

_LOGGER = logging.getLogger(__name__)


class SunSaleCoordinator(DataUpdateCoordinator):
    """Thin orchestrator: translators → capacity update → DAG → event routing."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self._entry = config_entry
        self._config: dict = {}
        self._sun_sale_config: SunSaleConfig | None = None
        self._store: Store | None = None
        self._capacity_estimator: CapacityEstimator | None = None
        self._engine: DagEngine | None = None
        self._translators: list = []
        self._event_router: EventRouter | None = None
        self._last_battery_reading: BatteryReading | None = None
        self.automation_enabled: bool = False
        self.last_dispatched_action: str | None = None
        self.last_dispatched_at: datetime | None = None

    @property
    def battery_config(self) -> BatteryConfig | None:
        return self._sun_sale_config.battery if self._sun_sale_config else None

    @property
    def tariff_config(self) -> TariffConfig | None:
        return self._sun_sale_config.tariff if self._sun_sale_config else None

    async def async_setup(self) -> None:
        """Build config objects, translators, DAG nodes, engine, and event router."""
        data = {**self._entry.data, **self._entry.options}
        self._config = data

        tariff_config = TariffConfig(
            distribution_fee=data[CONF_TARIFF_DISTRIBUTION_FEE],
            tax_rate=data[CONF_TARIFF_TAX_RATE] / 100.0,
            markup=data[CONF_TARIFF_MARKUP],
            sell_distribution_fee=data[CONF_TARIFF_SELL_DISTRIBUTION_FEE],
            sell_tax_rate=data[CONF_TARIFF_SELL_TAX_RATE] / 100.0,
            sell_markup=data[CONF_TARIFF_SELL_MARKUP],
        )

        battery_config = BatteryConfig(
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
            inverter_entity_ids = {
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
            inverter_entity_ids = {
                "battery_soc": data[CONF_INVERTER_ENTITY_BATTERY_SOC],
                "battery_power": data[CONF_INVERTER_ENTITY_BATTERY_POWER],
                "grid_power": data[CONF_INVERTER_ENTITY_GRID_POWER],
                "charge_control": data[CONF_INVERTER_ENTITY_CHARGE_CONTROL],
            }
        inverter = InverterController(self.hass, inverter_platform, inverter_entity_ids, battery_config)

        ev_config: EVChargerConfig | None = None
        ev_charger: EVChargerController | None = None
        ev_translator: EVTranslator | None = None
        ev_scheduler_node: EVSchedulerNode | None = None

        if data.get(CONF_EV_ENABLED, False):
            ev_platform = EVChargerPlatform(data[CONF_EV_PLATFORM])
            ev_config = EVChargerConfig(
                max_charge_power_kw=data[CONF_EV_MAX_CHARGE_POWER],
                min_charge_power_kw=data.get(CONF_EV_MIN_CHARGE_POWER, DEFAULT_EV_MIN_CHARGE_POWER_KW),
                battery_capacity_kwh=data[CONF_EV_BATTERY_CAPACITY],
            )
            ev_charger = EVChargerController(
                self.hass, ev_platform,
                {
                    "plug_state": data.get(CONF_EV_ENTITY_PLUG_STATE, ""),
                    "soc": data.get(CONF_EV_ENTITY_SOC, ""),
                    "charger_switch": data.get(CONF_EV_ENTITY_CHARGER_SWITCH, ""),
                },
            )
            ev_translator = EVTranslator(
                ev_charger=ev_charger,
                target_soc_entity=data.get(CONF_EV_ENTITY_TARGET_SOC, ""),
                departure_entity=data.get(CONF_EV_ENTITY_DEPARTURE_TIME, ""),
            )
            ev_scheduler_node = EVSchedulerNode(last_ev_action_ref=make_last_ref())

        self._sun_sale_config = SunSaleConfig(tariff=tariff_config, battery=battery_config, ev=ev_config)

        self._translators = [
            NordpoolTranslator(
                entity_id=data.get(CONF_NORDPOOL_ENTITY, ""),
                resolution=data.get(CONF_NORDPOOL_RESOLUTION, DEFAULT_NORDPOOL_RESOLUTION),
            ),
            SolarTranslator(
                entity_1=data.get(CONF_SOLAR_FORECAST_ENTITY, ""),
                entity_2=data.get(CONF_SOLAR_FORECAST_ENTITY_2, ""),
            ),
            BatteryTranslator(
                inverter=inverter,
                household_load_entity=data.get(CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD, ""),
            ),
        ]
        if ev_translator is not None:
            self._translators.append(ev_translator)

        inverter_last_ref = make_last_ref()
        nodes = [
            PricingNode(),
            BatteryStateNode(),
            GenerationNode(),
            DegradationNode(),
            LockoutNode(),
            OptimizerNode(last_inverter_action_ref=inverter_last_ref),
            DashboardNode(),
        ]
        if ev_scheduler_node is not None:
            nodes.append(ev_scheduler_node)

        self._engine = DagEngine(nodes)
        self._event_router = EventRouter(inverter=inverter, ev_charger=ev_charger)

        self._store = Store(self.hass, STORAGE_VERSION, STORAGE_KEY_CAPACITY)
        stored = await self._store.async_load()
        self._capacity_estimator = (
            CapacityEstimator.from_dict(stored)
            if stored
            else CapacityEstimator(battery_config.nominal_capacity_kwh)
        )

    async def _async_update_data(self) -> dict:
        """One DAG cycle: translate → capacity update → DAG → event routing."""
        now = datetime.now(timezone.utc)

        try:
            primary = await run_translators(
                self._translators, self.hass, self._sun_sale_config, self._config, now
            )

            current_reading: BatteryReading | None = primary.get(BatteryReading)
            if current_reading is not None:
                obs = self._build_capacity_observation(current_reading, now)
                if obs is not None:
                    self._capacity_estimator.add_observation(obs)
                    if self._store is not None:
                        await self._store.async_save(self._capacity_estimator.to_dict())
                self._last_battery_reading = current_reading

            primary[EstimatedCapacity] = EstimatedCapacity(
                value_kwh=self._capacity_estimator.estimated_capacity_kwh
            )

            secondary, events = await self._engine.run(primary, self._sun_sale_config, now)

            if self.automation_enabled and self._event_router is not None:
                for event in events:
                    await self._event_router.handle(event)
                if self._event_router.last_dispatched_action:
                    self.last_dispatched_action = self._event_router.last_dispatched_action
                    self.last_dispatched_at = now

            return self._build_sensor_dict(primary, secondary)

        except Exception as exc:
            raise UpdateFailed(f"Error updating sunSale data: {exc}") from exc

    def _build_sensor_dict(self, primary: dict, secondary: dict) -> dict:
        """Map typed DAG outputs to the string-keyed dict that sensors read."""
        reading: BatteryReading | None = primary.get(BatteryReading)
        nordpool: NordpoolPrices | None = primary.get(NordpoolPrices)
        dashboard: DashboardData | None = secondary.get(DashboardData)
        deg: DegradationCost | None = secondary.get(DegradationCost)

        return {
            "pricing": secondary.get(PriceSeries),
            "forecast": secondary.get(GenerationSeries),
            "calculation": secondary.get(CalculationResult),
            "schedule": secondary.get(Schedule),
            "ev_schedule": secondary.get(EVSchedule),
            "battery_state": secondary.get(BatteryState),
            "degradation_cost": deg.value_kwh if deg else 0.0,
            "estimated_capacity": self._capacity_estimator.estimated_capacity_kwh,
            "prices": nordpool.slots if nordpool else [],
            "grid_power_kw": reading.grid_power_kw if reading else 0.0,
            "battery_power_kw": reading.power_kw if reading else 0.0,
            "ev_state": primary.get(EVChargerState),
            "dashboard_slots": dashboard.future_slots if dashboard else [],
            "solar_frozen_forecast": dashboard.solar_frozen_forecast if dashboard else [],
        }

    def _build_capacity_observation(
        self,
        current: BatteryReading,
        now: datetime,
    ) -> CapacityObservation | None:
        if self._last_battery_reading is None:
            return None
        soc_delta = abs(current.soc - self._last_battery_reading.soc)
        if soc_delta < CAPACITY_OBS_MIN_SOC_DELTA:
            return None
        avg_power = (abs(self._last_battery_reading.power_kw) + abs(current.power_kw)) / 2.0
        energy_kwh = avg_power * (UPDATE_INTERVAL_MINUTES / 60.0)
        direction = "charge" if current.soc > self._last_battery_reading.soc else "discharge"
        return CapacityObservation(
            timestamp=now,
            soc_start=self._last_battery_reading.soc,
            soc_end=current.soc,
            energy_kwh=energy_kwh,
            direction=direction,
        )
