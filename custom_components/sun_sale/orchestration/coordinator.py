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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:    # pragma: no cover — Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo    # type: ignore[no-redef]

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
    CONF_SOLIS_CONFIG_ENTRY_ID,
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
    GRID_POWER_HISTORY_RETENTION_DAYS,
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
    CONF_INVERTER_ENTITY_PV_POWER,
    GENERATION_HISTORY_RETENTION_DAYS,
    PRICE_HISTORY_RETENTION_DAYS,
    PV_POWER_HISTORY_RETENTION_DAYS,
    STORAGE_KEY_CAPACITY,
    STORAGE_KEY_FORECAST_QUALITY,
    STORAGE_KEY_GENERATION,
    STORAGE_KEY_GRID_POWER,
    STORAGE_KEY_HOUSEHOLD_LOAD,
    STORAGE_KEY_MONTHLY_BILL,
    STORAGE_KEY_PRICE_HISTORY,
    STORAGE_KEY_PV_POWER,
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
    DailyPeak,
    DayClass,
    DegradationCost,
    EstimatedCapacity,
    ForecastAccuracyResult,
    ForecastQualityStore,
    GenerationHistory,
    GenerationReading,
    GenerationSeries,
    GridPowerHistory,
    GridPowerReading,
    MonthlyBillResult,
    MonthlyBillState,
    PvPowerHistory,
    PvPowerReading,
    HouseholdConsumptionReading,
    HouseholdLoadHistory,
    HouseholdLoadReading,
    HouseholdLoadSample,
    NordpoolData,
    ObservedGenerationSeries,
    PriceEntry,
    PriceHistory,
    PriceSeries,
    ProfitabilityScore,
    SolarData,
    SolarEntry,
    Schedule,
    SunSaleConfig,
    SunTimes,
    TariffConfig,
    YesterdayPrices,
)
from ..pipeline.nodes import (
    BaseLoadProfileNode,
    BatteryRuntimeNode,
    BatteryStateNode,
    BatteryStatusNode,
    ChargingProfileNode,
    DegradationNode,
    ForecastAccuracyNode,
    GenerationNode,
    LockoutNode,
    MonthlyBillNode,
    ObservedGenerationNode,
    ScheduleNode,
    PricingNode,
    ProfitabilityNode,
    make_last_ref,
)
from ..pipeline import forecast_accuracy as forecast_accuracy_module
from ..pipeline import profitability as profitability_module
from .persistent_store import PersistentStore
from ..inbound.battery import BatteryTranslator
from ..inbound.solis_entity_resolver import resolve_solis_entities
from ..inbound.forecast import SolarTranslator
from ..inbound.generation import GenerationTranslator, PvPowerTranslator
from ..inbound.household_consumption import HouseholdConsumptionTranslator
from ..inbound.household_load import HouseholdLoadTranslator
from ..inbound.pricing import NordpoolTranslator

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Yesterday/today two-bucket state
# ---------------------------------------------------------------------------

@dataclass
class _YesterdayBuckets:
    """In-memory state for the two-bucket yesterday/today price + solar store."""

    yesterday_date: str | None = None
    yesterday_nordpool: list[PriceEntry] = field(default_factory=list)
    yesterday_solar: list[SolarEntry] = field(default_factory=list)
    today_date: str | None = None
    today_nordpool: list[PriceEntry] = field(default_factory=list)
    today_solar: list[SolarEntry] = field(default_factory=list)


def _parse_nordpool_entries(payload: dict) -> list[PriceEntry]:
    """Deserialise a list of price entries from a stored payload dict."""
    return [
        PriceEntry(
            start=datetime.fromisoformat(e["start"]),
            end=datetime.fromisoformat(e["end"]),
            price_eur_kwh=e["price"],
        )
        for e in payload.get("nordpool", [])
    ]


def _parse_solar_entries(payload: dict) -> list[SolarEntry]:
    """Deserialise a list of solar entries from a stored payload dict."""
    return [
        SolarEntry(
            start=datetime.fromisoformat(e["start"]),
            end=datetime.fromisoformat(e["end"]),
            expected_kwh=e["kwh"],
            source=e["source"],
        )
        for e in payload.get("solar", [])
    ]


def _serialize_yesterday(buckets: _YesterdayBuckets) -> dict:
    """Serialise yesterday buckets to the two-bucket storage layout."""
    def _ser_nordpool(xs: list[PriceEntry]) -> list[dict]:
        return [{"start": e.start.isoformat(), "end": e.end.isoformat(), "price": e.price_eur_kwh} for e in xs]

    def _ser_solar(xs: list[SolarEntry]) -> list[dict]:
        return [{"start": e.start.isoformat(), "end": e.end.isoformat(), "kwh": e.expected_kwh, "source": e.source} for e in xs]

    return {
        "yesterday": {
            "date":     buckets.yesterday_date,
            "nordpool": _ser_nordpool(buckets.yesterday_nordpool),
            "solar":    _ser_solar(buckets.yesterday_solar),
        },
        "today": {
            "date":     buckets.today_date,
            "nordpool": _ser_nordpool(buckets.today_nordpool),
            "solar":    _ser_solar(buckets.today_solar),
        },
    }


def _deserialize_yesterday(d: dict) -> _YesterdayBuckets:
    """Deserialise yesterday buckets, handling the legacy single-bucket layout."""
    if "yesterday" in d or "today" in d:
        y = d.get("yesterday") or {}
        t = d.get("today") or {}
        return _YesterdayBuckets(
            yesterday_date=y.get("date"),
            yesterday_nordpool=_parse_nordpool_entries(y),
            yesterday_solar=_parse_solar_entries(y),
            today_date=t.get("date"),
            today_nordpool=_parse_nordpool_entries(t),
            today_solar=_parse_solar_entries(t),
        )
    # Legacy single-bucket layout {"date","nordpool","solar"} — treat as yesterday.
    return _YesterdayBuckets(
        yesterday_date=d.get("date"),
        yesterday_nordpool=_parse_nordpool_entries(d),
        yesterday_solar=_parse_solar_entries(d),
    )


def _rotate_yesterday_buckets(
    buckets: _YesterdayBuckets,
    today_str: str,
    today_nordpool: list[PriceEntry],
    today_solar: list[SolarEntry],
) -> _YesterdayBuckets:
    """Apply day-rollover rotation and replace the today slice.

    On the first save of a new local day the previous today bucket becomes
    yesterday; within the same day only the today slice is overwritten.

    Args:
        buckets: Current bucket state.
        today_str: ISO date string for the current local day.
        today_nordpool: Today's price entries to store.
        today_solar: Today's solar entries to store.

    Returns:
        Updated _YesterdayBuckets.
    """
    if buckets.today_date is not None and buckets.today_date != today_str:
        return _YesterdayBuckets(
            yesterday_date=buckets.today_date,
            yesterday_nordpool=buckets.today_nordpool,
            yesterday_solar=buckets.today_solar,
            today_date=today_str,
            today_nordpool=today_nordpool,
            today_solar=today_solar,
        )
    return _YesterdayBuckets(
        yesterday_date=buckets.yesterday_date,
        yesterday_nordpool=buckets.yesterday_nordpool,
        yesterday_solar=buckets.yesterday_solar,
        today_date=today_str,
        today_nordpool=today_nordpool,
        today_solar=today_solar,
    )


# ---------------------------------------------------------------------------
# Per-store serialisation helpers
# ---------------------------------------------------------------------------

def _serialize_generation(samples: list[GenerationReading]) -> dict:
    """Serialise a list of generation readings."""
    return {"samples": [{"ts": s.timestamp.isoformat(), "kwh": s.today_total_kwh} for s in samples]}


def _deserialize_generation(d: dict) -> list[GenerationReading]:
    """Deserialise a list of generation readings."""
    return [
        GenerationReading(
            today_total_kwh=s["kwh"],
            timestamp=datetime.fromisoformat(s["ts"]),
        )
        for s in d.get("samples", [])
    ]


def _serialize_pv_power(samples: list[PvPowerReading]) -> dict:
    """Serialise a list of PV power readings."""
    return {"samples": [{"ts": s.timestamp.isoformat(), "w": s.power_w} for s in samples]}


def _deserialize_pv_power(d: dict) -> list[PvPowerReading]:
    """Deserialise a list of PV power readings."""
    return [
        PvPowerReading(
            power_w=s["w"],
            timestamp=datetime.fromisoformat(s["ts"]),
        )
        for s in d.get("samples", [])
    ]


def _serialize_household_load(samples: list[HouseholdLoadSample]) -> dict:
    """Serialise a list of household load samples."""
    return {"samples": [{"ts": s.timestamp.isoformat(), "kw": s.load_kw} for s in samples]}


def _deserialize_household_load(d: dict) -> list[HouseholdLoadSample]:
    """Deserialise a list of household load samples."""
    return [
        HouseholdLoadSample(
            timestamp=datetime.fromisoformat(s["ts"]),
            load_kw=s["kw"],
        )
        for s in d.get("samples", [])
    ]


def _serialize_price_history(peaks: list[DailyPeak]) -> dict:
    """Serialise a list of daily peaks."""
    return {
        "peaks": [{"day": p.day.isoformat(), "peak": p.peak_eur_kwh, "class": p.day_class.value} for p in peaks]
    }


def _deserialize_price_history(d: dict) -> list[DailyPeak]:
    """Deserialise a list of daily peaks, silently skipping malformed entries."""
    result: list[DailyPeak] = []
    for p in d.get("peaks", []):
        try:
            result.append(DailyPeak(
                day=date.fromisoformat(p["day"]),
                peak_eur_kwh=p["peak"],
                day_class=DayClass(p["class"]),
            ))
        except (KeyError, ValueError):
            continue
    return result


def _serialize_grid_power(samples: list[GridPowerReading]) -> dict:
    """Serialise a list of grid power readings."""
    return {"samples": [{"ts": s.timestamp.isoformat(), "kw": s.power_kw} for s in samples]}


def _deserialize_grid_power(d: dict) -> list[GridPowerReading]:
    """Deserialise a list of grid power readings."""
    return [
        GridPowerReading(
            power_kw=s["kw"],
            timestamp=datetime.fromisoformat(s["ts"]),
        )
        for s in d.get("samples", [])
    ]


def _serialize_monthly_bill(state: MonthlyBillState) -> dict:
    """Serialise the monthly bill state."""
    return {
        "month_str": state.month_str,
        "carry_eur": state.carry_eur,
        "yday_str": state.yday_str,
        "previous_month_str": state.previous_month_str,
        "previous_month_eur": state.previous_month_eur,
    }


def _deserialize_monthly_bill(d: dict) -> MonthlyBillState:
    """Deserialise the monthly bill state.

    Tolerates legacy entries that still carry the removed `last_yday_total_eur`
    key by ignoring it; previous_month_* default to empty/zero when absent.
    """
    return MonthlyBillState(
        month_str=d["month_str"],
        carry_eur=d["carry_eur"],
        yday_str=d["yday_str"],
        previous_month_str=d.get("previous_month_str", ""),
        previous_month_eur=d.get("previous_month_eur", 0.0),
    )


class SunSaleCoordinator(DataUpdateCoordinator):
    """Thin orchestrator: translators → capacity update → DAG → event routing."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialise coordinator with lazy setup state; call async_setup() before use.

        Args:
            hass: Home Assistant instance.
            config_entry: HA config entry containing user configuration.
        """
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self._entry = config_entry
        self._config: dict = {}
        self._sun_sale_config: SunSaleConfig | None = None
        self._capacity_store: PersistentStore[CapacityEstimator] | None = None
        self._capacity_estimator: CapacityEstimator | None = None
        self._engine: DagEngine | None = None
        self._translators: list = []
        self._event_router: EventRouter | None = None
        self._last_battery_reading: BatteryReading | None = None
        self._yesterday_store: PersistentStore[_YesterdayBuckets] | None = None
        self._generation_store: PersistentStore[list[GenerationReading]] | None = None
        self._pv_power_store: PersistentStore[list[PvPowerReading]] | None = None
        self._household_load_store: PersistentStore[list[HouseholdLoadSample]] | None = None
        self._price_history_store: PersistentStore[list[DailyPeak]] | None = None
        self._forecast_quality_store: PersistentStore[ForecastQualityStore] | None = None
        self._grid_power_store: PersistentStore[list[GridPowerReading]] | None = None
        self._monthly_bill_store: PersistentStore[MonthlyBillState] | None = None
        self.automation_enabled: bool = False
        self.last_dispatched_action: str | None = None
        self.last_dispatched_at: datetime | None = None

    @property
    def battery_config(self) -> BatteryConfig | None:
        """Return the configured BatteryConfig, or None before async_setup completes."""
        return self._sun_sale_config.battery if self._sun_sale_config else None

    @property
    def tariff_config(self) -> TariffConfig | None:
        """Return the configured TariffConfig, or None before async_setup completes."""
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
            solis_entry_id = data.get(CONF_SOLIS_CONFIG_ENTRY_ID)
            if solis_entry_id:
                # Auto-detected path: resolve all entity IDs from the entity registry.
                inverter_entity_ids = resolve_solis_entities(self.hass, solis_entry_id)
            else:
                # Legacy path: entity IDs stored directly in config entry data.
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
            PvPowerTranslator(
                entity_id=data.get(CONF_INVERTER_ENTITY_PV_POWER, ""),
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
            MonthlyBillNode(),
            ChargingProfileNode(),
            BatteryRuntimeNode(),
            ForecastAccuracyNode(),
            ProfitabilityNode(),
            LockoutNode(),
            ScheduleNode(last_inverter_action_ref=inverter_last_ref),
        ]

        self._engine = DagEngine(nodes)
        self._event_router = EventRouter(inverter=inverter)

        self._capacity_store = PersistentStore(
            self.hass, STORAGE_VERSION, STORAGE_KEY_CAPACITY,
            serialize=lambda e: e.to_dict(),
            deserialize=CapacityEstimator.from_dict,
        )
        self._capacity_estimator = (
            await self._capacity_store.load()
            or CapacityEstimator(battery_config.nominal_capacity_kwh)
        )

        self._generation_store = PersistentStore(
            self.hass, STORAGE_VERSION, STORAGE_KEY_GENERATION,
            serialize=_serialize_generation,
            deserialize=_deserialize_generation,
        )
        await self._generation_store.load()

        self._pv_power_store = PersistentStore(
            self.hass, STORAGE_VERSION, STORAGE_KEY_PV_POWER,
            serialize=_serialize_pv_power,
            deserialize=_deserialize_pv_power,
        )
        await self._pv_power_store.load()

        self._household_load_store = PersistentStore(
            self.hass, STORAGE_VERSION, STORAGE_KEY_HOUSEHOLD_LOAD,
            serialize=_serialize_household_load,
            deserialize=_deserialize_household_load,
        )
        await self._household_load_store.load()

        self._price_history_store = PersistentStore(
            self.hass, STORAGE_VERSION, STORAGE_KEY_PRICE_HISTORY,
            serialize=_serialize_price_history,
            deserialize=_deserialize_price_history,
        )
        await self._price_history_store.load()

        self._forecast_quality_store = PersistentStore(
            self.hass, STORAGE_VERSION, STORAGE_KEY_FORECAST_QUALITY,
            serialize=forecast_accuracy_module.store_to_dict,
            deserialize=forecast_accuracy_module.store_from_dict,
        )
        await self._forecast_quality_store.load()

        self._grid_power_store = PersistentStore(
            self.hass, STORAGE_VERSION, STORAGE_KEY_GRID_POWER,
            serialize=_serialize_grid_power,
            deserialize=_deserialize_grid_power,
        )
        await self._grid_power_store.load()

        self._monthly_bill_store = PersistentStore(
            self.hass, STORAGE_VERSION, STORAGE_KEY_MONTHLY_BILL,
            serialize=_serialize_monthly_bill,
            deserialize=_deserialize_monthly_bill,
        )
        await self._monthly_bill_store.load()

        self._yesterday_store = PersistentStore(
            self.hass, STORAGE_VERSION, STORAGE_KEY_YESTERDAY,
            serialize=_serialize_yesterday,
            deserialize=_deserialize_yesterday,
        )
        await self._yesterday_store.load()

    async def _async_update_data(self) -> dict:
        """One DAG cycle: translate → capacity update → DAG → event routing."""
        now = datetime.now(timezone.utc)

        try:
            primary = await run_translators(
                self._translators, self.hass, self._sun_sale_config, self._config, now
            )

            # Day boundaries must be LOCAL so the chart's yesterday/today/tomorrow
            # axis aligns with the persisted yesterday store. Computing from
            # now.date() (UTC) leaves a UTC-offset-sized window after local
            # midnight where the store has "today's UTC date" but the lookup
            # asks for "yesterday LOCAL" — they never match, yesterday silently
            # disappears from the chart.
            local_now     = now.astimezone(self._sun_sale_config.local_tz)
            today_str     = local_now.date().isoformat()
            yesterday_str = (local_now.date() - timedelta(days=1)).isoformat()

            nordpool_data: NordpoolData | None = primary.get(NordpoolData)
            solar_data: SolarData | None = primary.get(SolarData)

            buckets = (self._yesterday_store.value if self._yesterday_store else None) or _YesterdayBuckets()

            # Pricing: pass yesterday in via a primary input; inbound.pricing
            # owns the 72h yesterday→today→tomorrow assembly. Stored data older
            # than yesterday is treated as empty.
            yesterday_pricing_entries = (
                tuple(buckets.yesterday_nordpool)
                if buckets.yesterday_date == yesterday_str
                else ()
            )
            primary[YesterdayPrices] = YesterdayPrices(entries=yesterday_pricing_entries)

            if buckets.yesterday_date == yesterday_str and solar_data is not None:
                solar_data.entries = buckets.yesterday_solar + solar_data.entries

            # Persist today's slice; rotate yesterday at LOCAL date rollover.
            if nordpool_data is not None and solar_data is not None and self._yesterday_store is not None:
                _local = self._sun_sale_config.local_tz
                today_nordpool = [e for e in nordpool_data.entries
                                   if e.start.astimezone(_local).date().isoformat() == today_str]
                today_solar = [e for e in solar_data.entries
                                if e.start.astimezone(_local).date().isoformat() == today_str]
                new_buckets = _rotate_yesterday_buckets(buckets, today_str, today_nordpool, today_solar)
                await self._yesterday_store.save(new_buckets)

            current_generation: GenerationReading | None = primary.get(GenerationReading)
            if current_generation is not None and self._generation_store is not None:
                cutoff = now - timedelta(days=GENERATION_HISTORY_RETENTION_DAYS)
                await self._generation_store.append_and_trim(
                    current_generation, cutoff, lambda s: s.timestamp,
                )
            primary[GenerationHistory] = GenerationHistory(
                samples=tuple((self._generation_store.value or []) if self._generation_store else []),
            )

            current_pv_power: PvPowerReading | None = primary.get(PvPowerReading)
            if current_pv_power is not None and self._pv_power_store is not None:
                cutoff = now - timedelta(days=PV_POWER_HISTORY_RETENTION_DAYS)
                await self._pv_power_store.append_and_trim(
                    current_pv_power, cutoff, lambda s: s.timestamp,
                )
            primary[PvPowerHistory] = PvPowerHistory(
                samples=tuple((self._pv_power_store.value or []) if self._pv_power_store else []),
            )

            current_reading_for_grid: BatteryReading | None = primary.get(BatteryReading)
            if current_reading_for_grid is not None and self._grid_power_store is not None:
                grid_sample = GridPowerReading(
                    power_kw=current_reading_for_grid.grid_power_kw,
                    timestamp=now,
                )
                cutoff = now - timedelta(days=GRID_POWER_HISTORY_RETENTION_DAYS)
                await self._grid_power_store.append_and_trim(
                    grid_sample, cutoff, lambda s: s.timestamp,
                )
            primary[GridPowerHistory] = GridPowerHistory(
                samples=tuple((self._grid_power_store.value or []) if self._grid_power_store else []),
            )
            primary[MonthlyBillState] = (
                self._monthly_bill_store.value if self._monthly_bill_store else None
            )

            current_load: HouseholdLoadReading | None = primary.get(HouseholdLoadReading)
            if current_load is not None and self._household_load_store is not None:
                sample = HouseholdLoadSample(timestamp=now, load_kw=current_load.load_kw)
                cutoff = now - timedelta(days=HOUSEHOLD_LOAD_HISTORY_RETENTION_DAYS)
                await self._household_load_store.append_and_trim(
                    sample, cutoff, lambda s: s.timestamp,
                )
            primary[HouseholdLoadHistory] = HouseholdLoadHistory(
                samples=tuple((self._household_load_store.value or []) if self._household_load_store else []),
            )

            primary[PriceHistory] = PriceHistory(
                peaks=tuple((self._price_history_store.value or []) if self._price_history_store else []),
            )
            primary[ForecastQualityStore] = (
                self._forecast_quality_store.value if self._forecast_quality_store else None
            ) or ForecastQualityStore()
            primary[SunTimes] = self._read_sun_times(now)

            current_reading: BatteryReading | None = primary.get(BatteryReading)
            if current_reading is not None:
                obs = self._build_capacity_observation(current_reading, now)
                if obs is not None:
                    self._capacity_estimator.add_observation(obs)
                    if self._capacity_store is not None:
                        await self._capacity_store.save(self._capacity_estimator)
                self._last_battery_reading = current_reading

            primary[EstimatedCapacity] = EstimatedCapacity(
                value_kwh=self._capacity_estimator.estimated_capacity_kwh
            )

            secondary, events = await self._engine.run(primary, self._sun_sale_config, now)

            acc_result: ForecastAccuracyResult | None = secondary.get(ForecastAccuracyResult)
            if acc_result is not None and self._forecast_quality_store is not None:
                await self._forecast_quality_store.save(acc_result.quality)

            bill_result: MonthlyBillResult | None = secondary.get(MonthlyBillResult)
            if bill_result is not None and self._monthly_bill_store is not None:
                await self._monthly_bill_store.save(bill_result.updated_state)

            pricing_result: PriceSeries | None = secondary.get(PriceSeries)
            if pricing_result is not None and self._price_history_store is not None:
                today_utc = now.date()
                today_peak_val = profitability_module.today_peak_from_price_series(
                    pricing_result, today_utc
                )
                if today_peak_val is not None:
                    new_peak = DailyPeak(
                        day=today_utc,
                        peak_eur_kwh=today_peak_val,
                        day_class=profitability_module.classify_day(today_utc),
                    )
                    peaks = [p for p in (self._price_history_store.value or []) if p.day != today_utc]
                    peaks.append(new_peak)
                    peaks.sort(key=lambda p: p.day)
                    cutoff_day = today_utc - timedelta(days=PRICE_HISTORY_RETENTION_DAYS)
                    peaks = [p for p in peaks if p.day >= cutoff_day]
                    await self._price_history_store.save(peaks)

            if self.automation_enabled and self._event_router is not None:
                for event in events:
                    await self._event_router.handle(event)
                if self._event_router.last_dispatched_action:
                    self.last_dispatched_action = self._event_router.last_dispatched_action
                    self.last_dispatched_at = now

            return self._build_sensor_dict(primary, secondary)

        except Exception as exc:
            raise UpdateFailed(f"Error updating sunSale data: {exc}") from exc

    def _resolve_local_tz(self):
        """Return the HA-configured local timezone, falling back to UTC.

        Reads hass.config.time_zone (e.g. "Europe/Riga"). Falls back to UTC
        when unset, unknown, or unparseable; baseload bucketing depends on this.

        Returns:
            tzinfo for the HA installation's local timezone.
        """
        tz_name = getattr(self.hass.config, "time_zone", None)
        if not tz_name:
            return timezone.utc
        try:
            return ZoneInfo(tz_name)
        except Exception:    # ZoneInfoNotFoundError + anything weird from HA mocks
            return timezone.utc

    def _build_sensor_dict(self, primary: dict, secondary: dict) -> dict:
        """Map typed DAG outputs to the string-keyed dict consumed by sensor entities.

        Args:
            primary: Translation-layer outputs keyed by type.
            secondary: DAG node outputs keyed by type.

        Returns:
            Dict with string keys matching what each sensor entity reads from
            coordinator.data.
        """
        reading: BatteryReading | None = primary.get(BatteryReading)
        nordpool: NordpoolData | None = primary.get(NordpoolData)
        deg: DegradationCost | None = secondary.get(DegradationCost)
        consumption: HouseholdConsumptionReading | None = primary.get(
            HouseholdConsumptionReading,
        )

        _acc: ForecastAccuracyResult | None = secondary.get(ForecastAccuracyResult)
        return {
            "pricing": secondary.get(PriceSeries),
            "forecast": secondary.get(GenerationSeries),
            "observed_generation": secondary.get(ObservedGenerationSeries),
            "forecast_error": _acc.error_series if _acc else None,
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
            "household_load_kw": reading.household_load_kw if reading else 0.0,
            "base_load_profile": secondary.get(BaseLoadProfile),
            "battery_runtime": secondary.get(BatteryRuntimeEstimate),
            "profitability_score": secondary.get(ProfitabilityScore),
            "consumption_today_kwh": (
                consumption.today_total_kwh if consumption else None
            ),
            "forecast_quality": _acc.quality if _acc else None,
            "sun_times": primary.get(SunTimes),
            "monthly_bill": secondary.get(MonthlyBillResult),
        }

    def _read_sun_times(self, now: datetime) -> SunTimes:
        """Read today's sunrise and sunset from the sun.sun HA entity.

        HA only exposes *next* rising/setting. If the next event is today
        (sun hasn't risen yet) it is this cycle's sunrise; if it's tomorrow,
        today's sunrise was approximately one sidereal day earlier.

        Args:
            now: Current cycle UTC timestamp for computing today's local date.

        Returns:
            SunTimes with today_sunrise and today_sunset in UTC, or None fields
            when the sun.sun entity is unavailable.
        """
        local_today = now.astimezone(self._sun_sale_config.local_tz).date()

        def _parse(attr: str) -> datetime | None:
            state = self.hass.states.get("sun.sun")
            if state is None:
                return None
            raw = state.attributes.get(attr)
            if not raw:
                return None
            try:
                dt = datetime.fromisoformat(raw)
                return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                return None

        def _today_event(next_event: datetime | None) -> datetime | None:
            if next_event is None:
                return None
            local_date = next_event.astimezone(self._sun_sale_config.local_tz).date()
            if local_date == local_today:
                return next_event
            # next_event is tomorrow → today's event was ~1 day ago
            return next_event - timedelta(days=1)

        next_rising  = _parse("next_rising")
        next_setting = _parse("next_setting")
        return SunTimes(
            today_sunrise=_today_event(next_rising),
            today_sunset=_today_event(next_setting),
        )

    def _build_capacity_observation(
        self,
        current: BatteryReading,
        now: datetime,
    ) -> CapacityObservation | None:
        """Build a CapacityObservation from consecutive battery readings if SoC delta is significant.

        Args:
            current: Most recent battery reading.
            now: Cycle timestamp used as the observation timestamp.

        Returns:
            CapacityObservation when |soc_delta| >= CAPACITY_OBS_MIN_SOC_DELTA,
            or None on the first cycle or when the delta is too small.
        """
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
