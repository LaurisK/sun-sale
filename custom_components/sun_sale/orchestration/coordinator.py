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

try:
    from zoneinfo import ZoneInfo
except ImportError:    # pragma: no cover — Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo    # type: ignore[no-redef]

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ..pipeline.battery import CapacityEstimator
from ..contract.const import (
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_NOMINAL_CAPACITY,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_PURCHASE_PRICE,
    CONF_BATTERY_RATED_CYCLE_LIFE,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY,
    CONF_INVERTER_ENTITY_BATTERY_POWER,
    CONF_INVERTER_ENTITY_BATTERY_SOC,
    CONF_INVERTER_ENTITY_CHARGE_CONTROL,
    CONF_INVERTER_ENTITY_GRID_POWER,
    CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY,
    CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD,
    CONF_INVERTER_ENTITY_SOLAR_ENERGY,
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
    CONF_SOLAR_FORECAST_ENTITY_2,
    CONF_TARIFF_DISTRIBUTION_FEE,
    CONF_TARIFF_MARKUP,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE,
    CONF_TARIFF_SELL_MARKUP,
    CONF_TARIFF_SELL_TAX_RATE,
    CONF_TARIFF_TAX_RATE,
    CAPACITY_OBS_MIN_SOC_DELTA,
    DEFAULT_BATTERY_NOMINAL_VOLTAGE,
    HOUSEHOLD_LOAD_HISTORY_RETENTION_DAYS,
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
    GENERATION_HISTORY_RETENTION_DAYS,
    STORAGE_KEY_CAPACITY,
    STORAGE_KEY_GENERATION,
    STORAGE_KEY_HOUSEHOLD_LOAD,
    STORAGE_KEY_YESTERDAY,
    STORAGE_VERSION,
    UPDATE_INTERVAL_MINUTES,
)
from ..pipeline.dag_engine import DagEngine, run_translators
from ..outbound.event_router import EventRouter
from ..outbound.inverter import InverterController, InverterPlatform
from ..contract.models import (
    BaseLoadProfile,
    BatteryConfig,
    BatteryReading,
    BatteryRuntimeEstimate,
    BatteryState,
    BatteryStatus,
    CalculationResult,
    ChargingProfile,
    CapacityObservation,
    DashboardData,
    DegradationCost,
    EstimatedCapacity,
    ForecastErrorSeries,
    GenerationHistory,
    GenerationReading,
    GenerationSeries,
    HouseholdConsumptionReading,
    HouseholdLoadHistory,
    HouseholdLoadReading,
    HouseholdLoadSample,
    NordpoolData,
    ObservedGenerationSeries,
    PriceEntry,
    PriceSeries,
    SolarData,
    SolarEntry,
    Schedule,
    SunSaleConfig,
    TariffConfig,
    YesterdayPrices,
)
from ..pipeline.nodes import (
    BaseLoadProfileNode,
    BatteryRuntimeNode,
    BatteryStateNode,
    BatteryStatusNode,
    ChargingProfileNode,
    DashboardNode,
    DegradationNode,
    ForecastAccuracyNode,
    GenerationNode,
    LockoutNode,
    ObservedGenerationNode,
    OptimizerNode,
    PricingNode,
    make_last_ref,
)
from ..inbound.battery import BatteryTranslator
from ..inbound.forecast import SolarTranslator
from ..inbound.generation import GenerationTranslator
from ..inbound.household_consumption import HouseholdConsumptionTranslator
from ..inbound.household_load import HouseholdLoadTranslator
from ..inbound.pricing import NordpoolTranslator

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
        self._yesterday_store: Store | None = None
        self._yesterday_nordpool: list[PriceEntry] = []
        self._yesterday_solar: list[SolarEntry] = []
        self._yesterday_stored_date: str | None = None  # date string the stored entries represent
        self._generation_store: Store | None = None
        self._generation_samples: list[GenerationReading] = []
        self._household_load_store: Store | None = None
        self._household_load_samples: list[HouseholdLoadSample] = []
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

        local_tz = self._resolve_local_tz()
        self._sun_sale_config = SunSaleConfig(
            tariff=tariff_config, battery=battery_config,
            local_tz=local_tz,
        )

        self._translators = [
            NordpoolTranslator(
                entity_id=data.get(CONF_NORDPOOL_ENTITY, ""),
            ),
            SolarTranslator(
                entity_1=data.get(CONF_SOLAR_FORECAST_ENTITY, ""),
                entity_2=data.get(CONF_SOLAR_FORECAST_ENTITY_2, ""),
            ),
            BatteryTranslator(
                inverter=inverter,
                household_load_entity=data.get(CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD, ""),
            ),
            GenerationTranslator(
                entity_id=data.get(CONF_INVERTER_ENTITY_SOLAR_ENERGY, ""),
            ),
            HouseholdLoadTranslator(
                entity_id=data.get(CONF_INVERTER_ENTITY_HOUSEHOLD_LOAD, ""),
            ),
            HouseholdConsumptionTranslator(
                entity_id=data.get(CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY, ""),
            ),
        ]

        inverter_last_ref = make_last_ref()
        nodes = [
            PricingNode(),
            BatteryStateNode(),
            BatteryStatusNode(),
            BaseLoadProfileNode(),
            GenerationNode(),
            ObservedGenerationNode(),
            DegradationNode(),
            ChargingProfileNode(),
            BatteryRuntimeNode(),
            ForecastAccuracyNode(),
            LockoutNode(),
            OptimizerNode(last_inverter_action_ref=inverter_last_ref),
            DashboardNode(),
        ]

        self._engine = DagEngine(nodes)
        self._event_router = EventRouter(inverter=inverter)

        self._store = Store(self.hass, STORAGE_VERSION, STORAGE_KEY_CAPACITY)
        stored = await self._store.async_load()
        self._capacity_estimator = (
            CapacityEstimator.from_dict(stored)
            if stored
            else CapacityEstimator(battery_config.nominal_capacity_kwh)
        )

        self._generation_store = Store(self.hass, STORAGE_VERSION, STORAGE_KEY_GENERATION)
        generation_stored = await self._generation_store.async_load()
        if generation_stored:
            self._generation_samples = [
                GenerationReading(
                    today_total_kwh=s["kwh"],
                    timestamp=datetime.fromisoformat(s["ts"]),
                )
                for s in generation_stored.get("samples", [])
            ]

        self._household_load_store = Store(
            self.hass, STORAGE_VERSION, STORAGE_KEY_HOUSEHOLD_LOAD,
        )
        household_stored = await self._household_load_store.async_load()
        if household_stored:
            self._household_load_samples = [
                HouseholdLoadSample(
                    timestamp=datetime.fromisoformat(s["ts"]),
                    load_kw=s["kw"],
                )
                for s in household_stored.get("samples", [])
            ]

        self._yesterday_store = Store(self.hass, STORAGE_VERSION, STORAGE_KEY_YESTERDAY)
        yesterday_stored = await self._yesterday_store.async_load()
        if yesterday_stored:
            self._yesterday_stored_date = yesterday_stored.get("date")
            self._yesterday_nordpool = [
                PriceEntry(
                    start=datetime.fromisoformat(e["start"]),
                    end=datetime.fromisoformat(e["end"]),
                    price_eur_kwh=e["price"],
                )
                for e in yesterday_stored.get("nordpool", [])
            ]
            self._yesterday_solar = [
                SolarEntry(
                    start=datetime.fromisoformat(e["start"]),
                    end=datetime.fromisoformat(e["end"]),
                    expected_kwh=e["kwh"],
                    source=e["source"],
                )
                for e in yesterday_stored.get("solar", [])
            ]

    async def _async_update_data(self) -> dict:
        """One DAG cycle: translate → capacity update → DAG → event routing."""
        now = datetime.now(timezone.utc)

        try:
            primary = await run_translators(
                self._translators, self.hass, self._sun_sale_config, self._config, now
            )

            today_str = now.date().isoformat()
            yesterday_str = (now.date() - timedelta(days=1)).isoformat()

            nordpool_data: NordpoolData | None = primary.get(NordpoolData)
            solar_data: SolarData | None = primary.get(SolarData)

            # Pricing: pass yesterday in via a primary input; inbound.pricing
            # owns the 72h yesterday→today→tomorrow assembly. Stored data older
            # than yesterday is treated as empty.
            yesterday_pricing_entries = (
                tuple(self._yesterday_nordpool)
                if self._yesterday_stored_date == yesterday_str
                else ()
            )
            primary[YesterdayPrices] = YesterdayPrices(entries=yesterday_pricing_entries)

            if self._yesterday_stored_date == yesterday_str and solar_data is not None:
                solar_data.entries = self._yesterday_solar + solar_data.entries

            # Save today's entries to the yesterday store (will be valid as yesterday tomorrow)
            if nordpool_data is not None and solar_data is not None:
                today_nordpool = [e for e in nordpool_data.entries if e.start.date().isoformat() == today_str]
                today_solar = [e for e in solar_data.entries if e.start.date().isoformat() == today_str]
                await self._yesterday_store.async_save({
                    "date": today_str,
                    "nordpool": [
                        {"start": e.start.isoformat(), "end": e.end.isoformat(), "price": e.price_eur_kwh}
                        for e in today_nordpool
                    ],
                    "solar": [
                        {"start": e.start.isoformat(), "end": e.end.isoformat(), "kwh": e.expected_kwh, "source": e.source}
                        for e in today_solar
                    ],
                })
                self._yesterday_stored_date = today_str
                self._yesterday_nordpool = today_nordpool
                self._yesterday_solar = today_solar

            current_generation: GenerationReading | None = primary.get(GenerationReading)
            if current_generation is not None:
                await self._append_generation_sample(current_generation, now)
            primary[GenerationHistory] = GenerationHistory(samples=tuple(self._generation_samples))

            current_load: HouseholdLoadReading | None = primary.get(HouseholdLoadReading)
            if current_load is not None:
                await self._append_household_load_sample(current_load, now)
            primary[HouseholdLoadHistory] = HouseholdLoadHistory(
                samples=tuple(self._household_load_samples),
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

    async def _append_generation_sample(
        self, reading: GenerationReading, now: datetime
    ) -> None:
        """Append a new sample, trim history older than retention, persist."""
        cutoff = now - timedelta(days=GENERATION_HISTORY_RETENTION_DAYS)
        kept = [s for s in self._generation_samples if s.timestamp >= cutoff]
        kept.append(reading)
        self._generation_samples = kept
        if self._generation_store is not None:
            await self._generation_store.async_save({
                "samples": [
                    {"ts": s.timestamp.isoformat(), "kwh": s.today_total_kwh}
                    for s in self._generation_samples
                ],
            })

    async def _append_household_load_sample(
        self, reading: HouseholdLoadReading, now: datetime
    ) -> None:
        """Append a new sample, trim by retention, persist."""
        cutoff = now - timedelta(days=HOUSEHOLD_LOAD_HISTORY_RETENTION_DAYS)
        kept = [s for s in self._household_load_samples if s.timestamp >= cutoff]
        kept.append(HouseholdLoadSample(timestamp=now, load_kw=reading.load_kw))
        self._household_load_samples = kept
        if self._household_load_store is not None:
            await self._household_load_store.async_save({
                "samples": [
                    {"ts": s.timestamp.isoformat(), "kw": s.load_kw}
                    for s in self._household_load_samples
                ],
            })

    def _resolve_local_tz(self):
        """Return the local timezone for the integration.

        Reads `hass.config.time_zone` (e.g. "Europe/Riga"); falls back to UTC
        when unset, unknown, or unparseable. Baseload bucketing depends on
        this — see docs/base_load_missing.md §9.
        """
        tz_name = getattr(self.hass.config, "time_zone", None)
        if not tz_name:
            return timezone.utc
        try:
            return ZoneInfo(tz_name)
        except Exception:    # ZoneInfoNotFoundError + anything weird from HA mocks
            return timezone.utc

    def _build_sensor_dict(self, primary: dict, secondary: dict) -> dict:
        """Map typed DAG outputs to the string-keyed dict that sensors read."""
        reading: BatteryReading | None = primary.get(BatteryReading)
        nordpool: NordpoolData | None = primary.get(NordpoolData)
        dashboard: DashboardData | None = secondary.get(DashboardData)
        deg: DegradationCost | None = secondary.get(DegradationCost)
        consumption: HouseholdConsumptionReading | None = primary.get(
            HouseholdConsumptionReading,
        )

        return {
            "pricing": secondary.get(PriceSeries),
            "forecast": secondary.get(GenerationSeries),
            "observed_generation": secondary.get(ObservedGenerationSeries),
            "forecast_error": secondary.get(ForecastErrorSeries),
            "calculation": secondary.get(CalculationResult),
            "schedule": secondary.get(Schedule),
            "battery_state": secondary.get(BatteryState),
            "battery_status": secondary.get(BatteryStatus),
            "charging_profile": secondary.get(ChargingProfile),
            "degradation_cost": deg.value_kwh if deg else 0.0,
            "estimated_capacity": self._capacity_estimator.estimated_capacity_kwh,
            "prices": nordpool.entries if nordpool else [],
            "grid_power_kw": reading.grid_power_kw if reading else 0.0,
            "battery_power_kw": reading.power_kw if reading else 0.0,
            "dashboard_slots": dashboard.future_slots if dashboard else [],
            "solar_frozen_forecast": dashboard.solar_frozen_forecast if dashboard else [],
            "base_load_profile": secondary.get(BaseLoadProfile),
            "battery_runtime": secondary.get(BatteryRuntimeEstimate),
            "consumption_today_kwh": (
                consumption.today_total_kwh if consumption else None
            ),
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
