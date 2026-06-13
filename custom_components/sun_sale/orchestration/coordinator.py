"""sunSale DataUpdateCoordinator — thin orchestrator for the DAG pipeline.

Responsibilities:
  1. Build translators, DAG nodes, and engine from config.
  2. Manage CapacityEstimator state across cycles (pre-DAG update + persistence).
  3. Run translators (parallel) → deposit EstimatedCapacity → run engine.
  4. Drive the inverter control module's dispatch tick from the DAG's Schedule.
  5. Map typed DAG outputs to the string-keyed coordinator.data dict for sensors.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
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
    CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY,
    CONF_INVERTER_ENTITY_INVERTER_CLOCK,
    CONF_NORDPOOL_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY_2,
    CONF_TARIFF_DISTRIBUTION_FEE,
    CONF_TARIFF_MARKUP,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE,
    CONF_TARIFF_SELL_MARKUP,
    CONF_TARIFF_SELL_TAX_RATE,
    CONF_TARIFF_TAX_RATE,
    CAPACITY_OBS_MAX_INTERVAL_S,
    CAPACITY_OBS_MIN_INTERVAL_S,
    CAPACITY_OBS_MIN_SOC_DELTA,
    COUNTER_SNAPSHOT_HISTORY_RETENTION_DAYS,
    DEFAULT_BATTERY_NOMINAL_VOLTAGE,
    GRID_POWER_HISTORY_RETENTION_DAYS,
    DEFAULT_SCHEDULE_ALLOW_DISCHARGE_TO_GRID,
    DEFAULT_SCHEDULE_ALLOW_FEED_IN,
    DEFAULT_SCHEDULE_ALLOW_GRID_CHARGING,
    DEFAULT_SCHEDULE_MAX_DISCHARGE_TO_GRID_KW,
    DEFAULT_SCHEDULE_MODE_CHANGE_PENALTY_EUR_PER_KWH,
    DEFAULT_SCHEDULE_PROFITABILITY_TILT_ALPHA,
    DEFAULT_SCHEDULE_TERMINAL_VALUE_DISCOUNT,
    DEFAULT_SCHEDULE_USE_STANDBY,
    DOMAIN,
    SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MAX,
    SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MIN,
    SCHEDULE_MODE_CHANGE_PENALTY_MAX,
    SCHEDULE_MODE_CHANGE_PENALTY_MIN,
    SCHEDULE_PROFITABILITY_TILT_ALPHA_MAX,
    SCHEDULE_PROFITABILITY_TILT_ALPHA_MIN,
    SCHEDULE_TERMINAL_VALUE_DISCOUNT_MAX,
    SCHEDULE_TERMINAL_VALUE_DISCOUNT_MIN,
    PRICE_HISTORY_RETENTION_DAYS,
    STORAGE_KEY_CAPACITY,
    STORAGE_KEY_FORECAST_QUALITY,
    STORAGE_KEY_BAKED_OBSERVED,
    STORAGE_KEY_COUNTER_SNAPSHOT,
    STORAGE_KEY_DERIVED_POWER,
    STORAGE_KEY_CONSUMPTION_DAILY,
    STORAGE_KEY_MODE_HISTORY,
    STORAGE_KEY_MONTHLY_BILL,
    STORAGE_KEY_PRICE_HISTORY,
    STORAGE_KEY_YESTERDAY,
    STORAGE_VERSION,
    UPDATE_INTERVAL_MINUTES,
)
from ..pipeline.dag_engine import DagEngine, run_translators
from ..outbound.inverter import InverterController, normalize_power_to_kw
from ..outbound.inverter_control_module import InverterControlModule
from ..contract.models import (
    BaseLoadProfile,
    BatteryConfig,
    BatteryReading,
    BatteryRuntimeEstimate,
    BatteryState,
    BatteryStatus,
    CalculationResult,
    CapacityObservation,
    DailyPeak,
    DayClass,
    BakedDayRecord,
    BakedObservedHistory,
    ConsumptionDailyBuckets,
    ConsumptionDayRecord,
    CounterSnapshotHistory,
    CounterSnapshotRecord,
    DegradationCost,
    EstimatedCapacity,
    ForecastAccuracyResult,
    ForecastQualityStore,
    GenerationReading,
    GenerationSeries,
    GridExportPowerHistory,
    GridExportPowerReading,
    GridExportTodayReading,
    GridImportPowerHistory,
    GridImportPowerReading,
    GridImportTodayReading,
    InverterModeChange,
    InverterModeHistory,
    InverterModeReading,
    InverterTimeReading,
    MonthlyBillResult,
    MonthlyBillState,
    PvPowerHistory,
    PvPowerReading,
    HouseholdConsumptionReading,
    AcPortPowerReading,
    BackupPowerReading,
    DerivedPowerHistory,
    NordpoolData,
    ObservedConsumptionSeries,
    ObservedGenerationSeries,
    ObservedGridSeries,
    ObservedLossesSeries,
    PriceEntry,
    PriceHistory,
    PriceSeries,
    ProfitabilityScore,
    SchedulePolicy,
    SlotKwh,
    SolarData,
    SolarEntry,
    Schedule,
    StorageMode,
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
    DegradationNode,
    ForecastAccuracyNode,
    GenerationNode,
    LockoutNode,
    MonthlyBillNode,
    ObservedConsumptionNode,
    ObservedGenerationNode,
    ObservedGridNode,
    ObservedLossesNode,
    ScheduleNode,
    PricingNode,
    ProfitabilityNode,
)
from ..pipeline import forecast_accuracy as forecast_accuracy_module
from ..pipeline import profitability as profitability_module
from .persistent_store import PersistentStore
from .history_stores import (
    ALL_HISTORY_SPECS,
    DERIVED_POWER_SPEC,
    GRID_EXPORT_POWER_SPEC,
    GRID_IMPORT_POWER_SPEC,
    SAMPLE_HISTORY_SPECS,
    append_and_inject,
)
from ..inbound.battery import BatteryTranslator
from ..inbound.inverter_entity_resolver import resolve_inverter_entities
from ..inbound.forecast import SolarTranslator
from ..inbound.observer.generation import (
    GENERATION_SIDE_ID,
    GenerationTranslator,
    PvPowerTranslator,
    build_generation_engine,
)
from ..inbound.consumption_daily import (
    backfill_from_derived_history,
    try_finalise_yesterday_consumption,
)
from ..inbound.household_consumption import HouseholdConsumptionTranslator
from ..inbound.observer.grid import (
    GRID_EXPORT_SIDE_ID,
    GRID_IMPORT_SIDE_ID,
    GridExportPowerObserver,
    GridExportTotalTranslator,
    GridImportPowerObserver,
    GridImportTotalTranslator,
    build_grid_engine,
)
from ..inbound.observer.derived import (
    AcPortPowerTranslator,
    BackupPowerTranslator,
    build_derived_power_sample,
)
from ..inbound.inverter_mode import InverterModeTranslator
from ..inbound.inverter_time import (
    InverterTimeHistory,
    InverterTimeTranslator,
    current_skew_seconds,
    empty_history as empty_inverter_time_history,
    update_history as update_inverter_time_history,
)
from ..inbound.observer.bake_in import try_bake_yesterday
from ..inbound.pre_rollover_snapshot import maybe_capture_snapshots
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


def _clamp(value: float, lo: float, hi: float) -> float:
    """Return ``value`` clamped into ``[lo, hi]`` and coerced to ``float``.

    Args:
        value: User-set knob value (already coerced from the Number entity).
        lo: Inclusive lower bound.
        hi: Inclusive upper bound.

    Returns:
        ``value`` clamped to the closed interval; NaN inputs collapse to ``lo``.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return lo
    if v != v:    # NaN check — NaN != NaN by IEEE-754
        return lo
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


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
        """Serialise a list of Nordpool price entries to dicts."""
        return [{"start": e.start.isoformat(), "end": e.end.isoformat(), "price": e.price_eur_kwh} for e in xs]

    def _ser_solar(xs: list[SolarEntry]) -> list[dict]:
        """Serialise a list of solar forecast entries to dicts."""
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
# The rolling sample-history stores (generation, PV power, the two grid-power
# directions, the two grid today-totals, and the derived sample) are described
# declaratively in ``history_stores.py``; only the irregular stores below carry
# bespoke serialisers.

def _serialize_consumption_daily(buckets: ConsumptionDailyBuckets) -> dict:
    """Serialise the rolling per-day hour-bucket consumption history."""
    return {
        "records": [
            {
                "date":  r.local_date.isoformat(),
                "kwh":   list(r.hour_kwh),
                "cov":   list(r.hour_completeness),
                "at":    r.finalised_at.isoformat(),
            }
            for r in buckets.records
        ]
    }


def _deserialize_consumption_daily(d: dict) -> ConsumptionDailyBuckets:
    """Deserialise the rolling per-day hour-bucket consumption history.

    Malformed records are skipped silently so a single bad row cannot block
    startup. Records that don't carry a full 24-tuple for either ``kwh`` or
    ``cov`` are dropped — the builder requires fixed shape.
    """
    records: list[ConsumptionDayRecord] = []
    for r in d.get("records", []):
        try:
            kwh = tuple(float(x) for x in r["kwh"])
            cov = tuple(float(x) for x in r["cov"])
            if len(kwh) != 24 or len(cov) != 24:
                continue
            records.append(
                ConsumptionDayRecord(
                    local_date=date.fromisoformat(r["date"]),
                    hour_kwh=kwh,
                    hour_completeness=cov,
                    finalised_at=datetime.fromisoformat(r["at"]),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return ConsumptionDailyBuckets(records=tuple(records))


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


def _serialize_counter_snapshot(history: CounterSnapshotHistory) -> dict:
    """Serialise the rolling pre-rollover counter snapshot history."""
    return {
        "records": [
            {
                "side": r.side_id,
                "ts":   r.captured_at.isoformat(),
                "kwh":  r.today_total_kwh,
            }
            for r in history.records
        ]
    }


def _deserialize_counter_snapshot(d: dict) -> CounterSnapshotHistory:
    """Deserialise the rolling pre-rollover counter snapshot history."""
    records: list[CounterSnapshotRecord] = []
    for r in d.get("records", []):
        try:
            records.append(
                CounterSnapshotRecord(
                    side_id=r["side"],
                    captured_at=datetime.fromisoformat(r["ts"]),
                    today_total_kwh=float(r["kwh"]),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return CounterSnapshotHistory(records=tuple(records))


def _serialize_baked_observed(history: BakedObservedHistory) -> dict:
    """Serialise the rolling baked-observed history (one record per (date, side))."""
    return {
        "records": [
            {
                "date":   r.date_str,
                "side":   r.side_id,
                "ctotal": r.counter_total_used,
                "src":    r.source_kind,
                "slots":  [
                    {"s": s.start.isoformat(), "e": s.end.isoformat(), "kwh": s.kwh}
                    for s in r.baked_slots
                ],
                "sum":    r.baked_sum,
                "at":     r.baked_at.isoformat(),
            }
            for r in history.records
        ]
    }


def _deserialize_baked_observed(d: dict) -> BakedObservedHistory:
    """Deserialise the rolling baked-observed history.

    Malformed entries are skipped silently so a single bad row cannot block
    startup. Slot lists with non-parseable timestamps are dropped wholesale.
    """
    records: list[BakedDayRecord] = []
    for r in d.get("records", []):
        try:
            slots = tuple(
                SlotKwh(
                    start=datetime.fromisoformat(s["s"]),
                    end=datetime.fromisoformat(s["e"]),
                    kwh=float(s["kwh"]),
                )
                for s in r["slots"]
            )
            records.append(
                BakedDayRecord(
                    date_str=r["date"],
                    side_id=r["side"],
                    counter_total_used=float(r["ctotal"]),
                    source_kind=r["src"],
                    baked_slots=slots,
                    baked_sum=float(r["sum"]),
                    baked_at=datetime.fromisoformat(r["at"]),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return BakedObservedHistory(records=tuple(records))


def _serialize_mode_history(history: InverterModeHistory) -> dict:
    """Serialise the rolling inverter-mode-change history."""
    return {
        "samples": [
            {"ts": s.timestamp.isoformat(), "mode": s.mode.value, "reg": s.reg_43110_value}
            for s in history.samples
        ]
    }


def _deserialize_mode_history(d: dict) -> InverterModeHistory:
    """Deserialise the rolling inverter-mode-change history.

    Unknown mode strings (e.g. from older versions) are coerced to UNKNOWN so
    the integration starts cleanly after a release that retired a mode value.
    """
    samples: list[InverterModeChange] = []
    for s in d.get("samples", []):
        try:
            mode = StorageMode(s["mode"])
        except ValueError:
            mode = StorageMode.UNKNOWN
        samples.append(
            InverterModeChange(
                timestamp=datetime.fromisoformat(s["ts"]),
                mode=mode,
                reg_43110_value=int(s["reg"]),
            )
        )
    return InverterModeHistory(samples=tuple(samples))


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


async def _backfill_directional_power_from_recorder(
    hass: HomeAssistant,
    entity_id: str,
    reading_cls: type,
    existing: list,
    start: datetime,
    end: datetime,
) -> list:
    """Return existing samples merged with recorder state changes for ``entity_id``.

    Used on coordinator setup so the monthly bill's yday→now slots have data
    even on the first run after the integration is installed/upgraded.
    Recorder state values are normalised to kW via the entity's
    ``unit_of_measurement`` attribute, matching the live read path. Negative
    values are clamped to 0 — directional magnitudes are non-negative by
    contract.

    Args:
        hass: Home Assistant instance.
        entity_id: Power sensor entity ID; empty disables backfill.
        reading_cls: Reading dataclass to construct (e.g.
            ``GridImportPowerReading``). Must accept ``power_kw`` and
            ``timestamp`` keyword args.
        existing: Samples already loaded from the persistent store.
        start: Earliest UTC timestamp to backfill.
        end: Latest UTC timestamp to backfill.

    Returns:
        Combined, time-sorted list of ``reading_cls`` samples deduplicated by
        timestamp. Returns ``existing`` unchanged if the recorder is
        unavailable or the entity_id is empty.
    """
    if not entity_id:
        return existing

    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import (
            state_changes_during_period,
        )
    except ImportError:
        return existing

    try:
        instance = get_instance(hass)
        states_dict = await instance.async_add_executor_job(
            state_changes_during_period,
            hass,
            start,
            end,
            entity_id,
        )
    except Exception:    # pragma: no cover — recorder may be unavailable
        _LOGGER.warning(
            "Directional power recorder backfill failed for %s", entity_id, exc_info=True,
        )
        return existing

    states = states_dict.get(entity_id, []) if states_dict else []
    backfilled: list = []
    for s in states:
        raw = getattr(s, "state", None)
        if raw in (None, "unavailable", "unknown", ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        unit = str((getattr(s, "attributes", {}) or {}).get("unit_of_measurement") or "").strip()
        kw = max(0.0, normalize_power_to_kw(value, unit))
        ts = getattr(s, "last_updated", None) or getattr(s, "last_changed", None)
        if ts is None:
            continue
        backfilled.append(reading_cls(power_kw=kw, timestamp=ts))

    if not backfilled:
        return existing

    seen: set[datetime] = set()
    merged: list = []
    for sample in (*existing, *backfilled):
        if sample.timestamp in seen:
            continue
        seen.add(sample.timestamp)
        merged.append(sample)
    merged.sort(key=lambda x: x.timestamp)
    return merged


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
        self._control_module: InverterControlModule | None = None
        self._last_battery_reading: BatteryReading | None = None
        self._last_battery_reading_at: datetime | None = None
        self._yesterday_store: PersistentStore[_YesterdayBuckets] | None = None
        # Rolling sample-history stores keyed by storage key — populated in
        # async_setup from ``history_stores.ALL_HISTORY_SPECS``. See
        # orchestration/history_stores.py.
        self._history_stores: dict[str, PersistentStore] = {}
        self._consumption_daily_store: PersistentStore[ConsumptionDailyBuckets] | None = None
        self._price_history_store: PersistentStore[list[DailyPeak]] | None = None
        self._forecast_quality_store: PersistentStore[ForecastQualityStore] | None = None
        self._grid_import_power_entity_id: str = ""
        self._grid_export_power_entity_id: str = ""
        self._monthly_bill_store: PersistentStore[MonthlyBillState] | None = None
        self._mode_history_store: PersistentStore[InverterModeHistory] | None = None
        self._counter_snapshot_store: PersistentStore[CounterSnapshotHistory] | None = None
        self._baked_observed_store: PersistentStore[BakedObservedHistory] | None = None
        self._inverter_time_history: InverterTimeHistory = empty_inverter_time_history()
        self.automation_enabled: bool = False
        self.use_standby: bool = DEFAULT_SCHEDULE_USE_STANDBY
        self.allow_grid_charging: bool = DEFAULT_SCHEDULE_ALLOW_GRID_CHARGING
        self.allow_feed_in: bool = DEFAULT_SCHEDULE_ALLOW_FEED_IN
        self.allow_discharge_to_grid: bool = DEFAULT_SCHEDULE_ALLOW_DISCHARGE_TO_GRID
        self.mode_change_penalty_eur_per_kwh: float = (
            DEFAULT_SCHEDULE_MODE_CHANGE_PENALTY_EUR_PER_KWH
        )
        self.profitability_tilt_alpha: float = DEFAULT_SCHEDULE_PROFITABILITY_TILT_ALPHA
        self.terminal_value_discount: float = DEFAULT_SCHEDULE_TERMINAL_VALUE_DISCOUNT
        self.max_discharge_to_grid_kw: float | None = DEFAULT_SCHEDULE_MAX_DISCHARGE_TO_GRID_KW
        # Manual override for the dispatched StorageMode. When set, the control
        # module forwards this to the inverter regardless of the scheduler's
        # current-slot choice AND regardless of ``automation_enabled`` —
        # operator intent always reaches the inverter. ``None`` (default)
        # keeps sunSale's scheduled choice (the "sunsale" option in the UI).
        self.mode_override: StorageMode | None = None
        self.last_dispatched_action: str | None = None
        self.last_dispatched_at: datetime | None = None
        # Phase 0 visibility: per-tick dispatch outcome surfaced on the
        # ObservedInverterModeSensor. ``last_dispatched_action`` reflects only
        # successful writes; these fields reflect the latest tick regardless
        # of outcome (no_target, no_spec, automation_disabled, ok, reconcile,
        # holding).
        self.last_dispatch_outcome: str | None = None
        self.last_dispatch_target: str | None = None
        self.last_dispatch_tick_at: datetime | None = None
        self.automation_enabled_at_dispatch: bool | None = None
        # Phase 2 visibility: commanded-mode + verify-loop state, mirrored
        # from the InverterControlModule after each tick. See
        # ``inverter_control_module.py`` for the verify-state vocabulary.
        self.last_commanded_mode: str | None = None
        self.last_commanded_at: datetime | None = None
        self.verify_state: str | None = None
        self.last_verify_at: datetime | None = None
        self.last_verify_observed_reg: int | None = None
        # Per-register desired-vs-observed comparison for the last-commanded
        # mode, mirrored from the control module. Drives the panel's
        # green/amber/red register readout.
        self.register_status: list[dict] = []

    @property
    def battery_config(self) -> BatteryConfig | None:
        """Return the configured BatteryConfig, or None before async_setup completes."""
        return self._sun_sale_config.battery if self._sun_sale_config else None

    async def force_verify_inverter_mode(self) -> None:
        """Run an inverter-mode verify cycle immediately.

        Backs the ``sun_sale.force_verify_inverter_mode`` service. Delegates
        to ``InverterControlModule.force_verify_now``; when the control
        module isn't ready yet (during early ``async_setup``), the call is
        a no-op so the service handler doesn't have to special-case startup.
        """
        if self._control_module is None:
            return
        await self._control_module.force_verify_now()

    async def dispatch_mode_override(self) -> None:
        """Push the current ``mode_override`` to the inverter immediately.

        Lightweight alternative to ``async_request_refresh`` for the
        mode-override select: a button press dispatches through the control
        module's ``dispatch_override`` entry point, skipping the full DAG
        refresh (14 translators, 16 nodes, the store saves) that
        ``async_request_refresh`` would incur just to reach the dispatcher.
        The override (or, when released to ``sunsale``, the current slot from
        the last computed Schedule) reaches the inverter at once; the next
        regular tick reconciles the rest of the pipeline.

        Mirrors the resulting dispatch + verify state and notifies listeners so
        the panel's badge and per-register colours update instantly. A no-op
        before the control module is built (early ``async_setup``).
        """
        if self._control_module is None:
            return
        now = datetime.now(timezone.utc)
        schedule = self.data.get("schedule") if isinstance(self.data, dict) else None
        await self._control_module.dispatch_override(
            now=now,
            schedule=schedule,
            mode_override=self.mode_override,
            automation_enabled=self.automation_enabled,
        )
        self._mirror_control_module_state()
        self.async_update_listeners()

    def _mirror_control_module_state(self) -> None:
        """Copy the control module's dispatch + verify state onto the coordinator.

        Pulls every module-derived diagnostic field (dispatch outcome, the
        commanded-mode truth, the verify-loop state, and the per-register
        comparison) into the coordinator's public attributes so the
        ``ObservedInverterModeSensor`` and panel read a single, current
        snapshot. Tick-local fields (``last_dispatched_action`` / ``_at``) are
        owned by ``_async_update_data`` and not touched here.
        """
        module = self._control_module
        if module is None:
            return
        self.last_dispatch_outcome = module.last_dispatch_outcome
        module_target = module.last_dispatch_target
        self.last_dispatch_target = (
            module_target.value if module_target is not None else None
        )
        self.last_dispatch_tick_at = module.last_dispatch_at
        self.automation_enabled_at_dispatch = (
            module.automation_enabled_at_last_dispatch
        )
        commanded = module.last_commanded_mode
        self.last_commanded_mode = (
            commanded.value if commanded is not None else None
        )
        self.last_commanded_at = module.last_commanded_at
        self.verify_state = module.verify_state
        self.last_verify_at = module.last_verify_at
        self.last_verify_observed_reg = module.last_verify_observed_reg
        self.register_status = module.register_status

    def _on_control_state_change(self) -> None:
        """Refresh entity state when the verify loop mutates outside a tick.

        Wired into ``InverterControlModule`` as ``on_state_change``. The verify
        loop runs on ``async_call_later`` between coordinator cycles, so without
        this push the engagement badge and per-register colours would lag up to
        one 5-minute cycle. Mirrors the fresh module state, then notifies the
        coordinator's listeners so ``ObservedInverterModeSensor`` re-renders.
        """
        self._mirror_control_module_state()
        self.async_update_listeners()

    async def async_shutdown(self) -> None:
        """Tear down the coordinator and its control module on entry unload.

        Extends ``DataUpdateCoordinator.async_shutdown`` (which cancels the
        scheduled refresh) by also shutting down the control module, so its
        pending verify-tick cannot fire after unload and issue ghost Modbus
        writes against the still-valid solis_modbus entities.
        """
        if self._control_module is not None:
            self._control_module.shutdown()
        await super().async_shutdown()

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

        # Entity-ID resolution (platform branch, Solis auto-detect, observer
        # entity merge, yesterday-total mapping) lives in
        # inbound/inverter_entity_resolver.py. It mutates ``data`` in place to
        # add any auto-detected yesterday-total entities.
        resolved = resolve_inverter_entities(self.hass, data)
        inverter = InverterController(
            self.hass, resolved.platform, resolved.inverter_entity_ids, battery_config,
        )
        self._inverter_entity_ids = dict(resolved.inverter_entity_ids)
        self._grid_import_power_entity_id = resolved.grid_import_power
        self._grid_export_power_entity_id = resolved.grid_export_power
        self._pv_power_entity_id = resolved.pv_power
        self._solar_energy_today_entity_id = resolved.solar_energy_today
        self._ac_port_power_entity_id = resolved.ac_port_power
        self._backup_power_entity_id = resolved.backup_power

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
                # Legacy: kept so existing configs that mapped a single
                # household-load sensor still feed BatteryReading.household_load_kw
                # for the dashboard. New configs leave this empty → 0.2 kW stub.
                household_load_entity=data.get("inverter_entity_household_load", ""),
            ),
            GridImportPowerObserver(
                entity_id=self._grid_import_power_entity_id,
                signed_entity_id=resolved.signed_grid_power,
            ),
            GridExportPowerObserver(
                entity_id=self._grid_export_power_entity_id,
                signed_entity_id=resolved.signed_grid_power,
            ),
            GridImportTotalTranslator(entity_id=resolved.grid_import_total),
            GridExportTotalTranslator(entity_id=resolved.grid_export_total),
            GenerationTranslator(entity_id=resolved.solar_energy_today),
            PvPowerTranslator(entity_id=resolved.pv_power),
            HouseholdConsumptionTranslator(
                entity_id=data.get(CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY, ""),
            ),
            AcPortPowerTranslator(entity_id=self._ac_port_power_entity_id),
            BackupPowerTranslator(entity_id=self._backup_power_entity_id),
            InverterModeTranslator(inverter=inverter),
            InverterTimeTranslator(
                entity_id=data.get(CONF_INVERTER_ENTITY_INVERTER_CLOCK, ""),
                local_tz=local_tz,
            ),
        ]

        nodes = [
            PricingNode(),
            BatteryStateNode(),
            BatteryStatusNode(),
            BaseLoadProfileNode(),
            GenerationNode(),
            ObservedGenerationNode(),
            ObservedGridNode(),
            ObservedConsumptionNode(),
            ObservedLossesNode(),
            DegradationNode(),
            MonthlyBillNode(),
            BatteryRuntimeNode(),
            ForecastAccuracyNode(),
            ProfitabilityNode(),
            LockoutNode(),
            ScheduleNode(),
        ]

        self._engine = DagEngine(nodes)
        self._control_module = InverterControlModule(
            inverter=inverter,
            battery_config=battery_config,
            local_tz=local_tz,
            hass=self.hass,
            on_state_change=self._on_control_state_change,
        )

        self._capacity_store = PersistentStore(
            self.hass, STORAGE_VERSION, STORAGE_KEY_CAPACITY,
            serialize=lambda e: e.to_dict(),
            deserialize=CapacityEstimator.from_dict,
        )
        self._capacity_estimator = (
            await self._capacity_store.load()
            or CapacityEstimator(battery_config.nominal_capacity_kwh)
        )

        # Rolling sample-history stores — one PersistentStore per spec, all
        # sharing the spec-driven serialise/deserialise. See
        # orchestration/history_stores.py.
        for spec in ALL_HISTORY_SPECS:
            store = PersistentStore(
                self.hass, STORAGE_VERSION, spec.storage_key,
                serialize=spec.serialize,
                deserialize=spec.deserialize,
            )
            await store.load()
            self._history_stores[spec.storage_key] = store

        # Backfill the directional grid-power stores from the recorder so the
        # monthly bill's yday→now slots have data on the first run after the
        # integration is installed/upgraded.
        backfill_now = datetime.now(timezone.utc)
        backfill_start = backfill_now - timedelta(days=GRID_POWER_HISTORY_RETENTION_DAYS)
        for spec, entity_id in (
            (GRID_IMPORT_POWER_SPEC, self._grid_import_power_entity_id),
            (GRID_EXPORT_POWER_SPEC, self._grid_export_power_entity_id),
        ):
            store = self._history_stores[spec.storage_key]
            existing_samples = list(store.value or [])
            merged_samples = await _backfill_directional_power_from_recorder(
                self.hass, entity_id, spec.reading_type,
                existing_samples, backfill_start, backfill_now,
            )
            if len(merged_samples) != len(existing_samples):
                await store.save(merged_samples)

        self._consumption_daily_store = PersistentStore(
            self.hass, STORAGE_VERSION, STORAGE_KEY_CONSUMPTION_DAILY,
            serialize=_serialize_consumption_daily,
            deserialize=_deserialize_consumption_daily,
        )
        await self._consumption_daily_store.load()

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

        self._mode_history_store = PersistentStore(
            self.hass, STORAGE_VERSION, STORAGE_KEY_MODE_HISTORY,
            serialize=_serialize_mode_history,
            deserialize=_deserialize_mode_history,
        )
        await self._mode_history_store.load()

        self._counter_snapshot_store = PersistentStore(
            self.hass, STORAGE_VERSION, STORAGE_KEY_COUNTER_SNAPSHOT,
            serialize=_serialize_counter_snapshot,
            deserialize=_deserialize_counter_snapshot,
        )
        await self._counter_snapshot_store.load()

        self._baked_observed_store = PersistentStore(
            self.hass, STORAGE_VERSION, STORAGE_KEY_BAKED_OBSERVED,
            serialize=_serialize_baked_observed,
            deserialize=_deserialize_baked_observed,
        )
        await self._baked_observed_store.load()

        # Backfill the consumption-daily store from any complete local days
        # already present in the derived-power history. With the standard
        # 2-day derived retention this seeds 1–2 records (typically
        # yesterday + day-before-yesterday) so the per-hour P15 builder has
        # something to chew on before the regular per-cycle finalise hook
        # accumulates fresh days. Idempotent — skipped for dates already
        # recorded.
        derived_store = self._history_stores.get(STORAGE_KEY_DERIVED_POWER)
        if (
            self._consumption_daily_store is not None
            and derived_store is not None
        ):
            local_tz = self._sun_sale_config.local_tz
            existing = (
                self._consumption_daily_store.value
                or ConsumptionDailyBuckets(records=())
            )
            derived_history = DerivedPowerHistory(
                samples=tuple(derived_store.value or []),
            )
            after = backfill_from_derived_history(
                derived_history=derived_history,
                existing=existing,
                local_tz=local_tz,
                now=datetime.now(timezone.utc),
            )
            if after is not existing:
                await self._consumption_daily_store.save(after)

    @contextmanager
    def _guarded(self, label: str):
        """Swallow and log any exception from a best-effort bookkeeping block.

        Isolates a per-cycle accounting / persistence side-effect so a single
        failure (a corrupt stored record, a try_bake_yesterday edge case) logs
        and the cycle continues — keeping sensor data fresh and, critically,
        never blocking the inverter dispatch. The ``with`` body may ``await``;
        exceptions raised inside it propagate here and are contained.

        Args:
            label: Human-readable name of the guarded step, used in the log.

        Yields:
            None; control returns to the ``with`` body.
        """
        try:
            yield
        except Exception:    # best-effort isolation by design — never re-raised
            _LOGGER.warning(
                "sunSale bookkeeping step '%s' failed; continuing", label,
                exc_info=True,
            )

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
            # Best-effort: the pricing/solar primaries above are already
            # assembled, so a rotation or save failure must not abort the cycle.
            with self._guarded("yesterday-bucket rotation"):
                if nordpool_data is not None and solar_data is not None and self._yesterday_store is not None:
                    _local = self._sun_sale_config.local_tz
                    today_nordpool = [e for e in nordpool_data.entries
                                       if e.start.astimezone(_local).date().isoformat() == today_str]
                    today_solar = [e for e in solar_data.entries
                                    if e.start.astimezone(_local).date().isoformat() == today_str]
                    new_buckets = _rotate_yesterday_buckets(buckets, today_str, today_nordpool, today_solar)
                    await self._yesterday_store.save(new_buckets)

            # Rolling sample histories — append each cycle's reading to its
            # store (trimming to the spec's retention) and inject the window
            # as the spec's *History primary. See history_stores.py.
            for spec in SAMPLE_HISTORY_SPECS:
                store = self._history_stores.get(spec.storage_key)
                # Seed the primary with the un-appended window first so that if
                # the append (a disk write) fails, the DAG still receives this
                # key and the cycle — including the inverter dispatch — proceeds.
                primary[spec.history_type] = spec.build_history(store)
                with self._guarded(f"history append {spec.storage_key}"):
                    await append_and_inject(
                        spec, store, primary, primary.get(spec.reading_type), now,
                    )

            # Derived-power cross-stream sample: composes AC port + backup +
            # battery (carries battery_power + grid_net_signed) + PV into one
            # synchronised tuple. Persisted as a rolling history so the
            # consumption + losses observers can power-average per slot. The
            # composer returns None when any source is unavailable this cycle —
            # partial samples would bias the per-slot mean asymmetrically.
            derived_store = self._history_stores.get(STORAGE_KEY_DERIVED_POWER)
            primary[DERIVED_POWER_SPEC.history_type] = DERIVED_POWER_SPEC.build_history(
                derived_store,
            )
            with self._guarded("derived-power append"):
                derived_sample = build_derived_power_sample(
                    now=now,
                    ac_port=primary.get(AcPortPowerReading),
                    backup=primary.get(BackupPowerReading),
                    battery=primary.get(BatteryReading),
                    pv=primary.get(PvPowerReading),
                )
                await append_and_inject(
                    DERIVED_POWER_SPEC, derived_store, primary, derived_sample, now,
                )

            # Inverter clock skew tracker — drives the snapshot window shift
            # below so the capture aligns with INVERTER-local midnight when
            # the two clocks have drifted apart. Returns ``None`` (no shift)
            # until enough samples have accumulated for confidence.
            current_inverter_time: InverterTimeReading | None = primary.get(
                InverterTimeReading,
            )
            clock_skew = None
            with self._guarded("inverter-time tracking"):
                self._inverter_time_history = update_inverter_time_history(
                    self._inverter_time_history, current_inverter_time,
                )
                clock_skew = current_skew_seconds(self._inverter_time_history)

            # Pre-rollover snapshot: capture today_total per side within the
            # late-evening window so the next-day bake-in has an authoritative
            # value when no dedicated yesterday-total sensor is mapped.
            with self._guarded("pre-rollover snapshot capture"):
                if self._counter_snapshot_store is not None:
                    current_snapshots = (
                        self._counter_snapshot_store.value
                        or CounterSnapshotHistory(records=())
                    )
                    updated_snapshots = maybe_capture_snapshots(
                        snapshot_history=current_snapshots,
                        sources=[
                            (GENERATION_SIDE_ID, primary.get(GenerationReading)),
                            (GRID_IMPORT_SIDE_ID, primary.get(GridImportTodayReading)),
                            (GRID_EXPORT_SIDE_ID, primary.get(GridExportTodayReading)),
                        ],
                        now=now,
                        local_tz=self._sun_sale_config.local_tz,
                        retention_days=COUNTER_SNAPSHOT_HISTORY_RETENTION_DAYS,
                        clock_skew_seconds=clock_skew,
                    )
                    if updated_snapshots is not current_snapshots:
                        await self._counter_snapshot_store.save(updated_snapshots)
            # Default to an empty history when the store exists but holds no
            # value yet (fresh install / first cycle). The DAG node consumes
            # these via ``ctx.require`` so an explicit empty fallback is
            # required — ``ctx.get`` returns the primary value whenever the key
            # is present, so a stored ``None`` would raise
            # ``MissingDependencyError``.
            current_snapshots = (
                self._counter_snapshot_store.value
                if self._counter_snapshot_store else None
            ) or CounterSnapshotHistory(records=())
            primary[CounterSnapshotHistory] = current_snapshots

            current_baked = (
                self._baked_observed_store.value
                if self._baked_observed_store else None
            ) or BakedObservedHistory(records=())
            primary[BakedObservedHistory] = current_baked

            primary[MonthlyBillState] = (
                self._monthly_bill_store.value if self._monthly_bill_store else None
            )

            # Consumption-daily rollup: finalise yesterday once per local date.
            # The hook is idempotent — when yesterday's record already exists
            # only trimming runs, so calling every cycle is fine. The primary
            # is populated from the (possibly just-updated) store value so the
            # BaseLoadProfile node sees the latest rolling window.
            with self._guarded("consumption-daily finalise"):
                if (
                    self._consumption_daily_store is not None
                    and derived_store is not None
                    and self._sun_sale_config is not None
                ):
                    existing_buckets = (
                        self._consumption_daily_store.value
                        or ConsumptionDailyBuckets(records=())
                    )
                    derived_for_finalise = primary.get(DerivedPowerHistory)
                    if derived_for_finalise is None:
                        derived_for_finalise = DerivedPowerHistory(
                            samples=tuple(derived_store.value or []),
                        )
                    updated_buckets = try_finalise_yesterday_consumption(
                        derived_history=derived_for_finalise,
                        existing=existing_buckets,
                        local_tz=self._sun_sale_config.local_tz,
                        now=now,
                    )
                    if updated_buckets is not existing_buckets:
                        await self._consumption_daily_store.save(updated_buckets)
            primary[ConsumptionDailyBuckets] = (
                self._consumption_daily_store.value
                if self._consumption_daily_store is not None
                else None
            ) or ConsumptionDailyBuckets(records=())

            primary[PriceHistory] = PriceHistory(
                peaks=tuple((self._price_history_store.value or []) if self._price_history_store else []),
            )
            primary[ForecastQualityStore] = (
                self._forecast_quality_store.value if self._forecast_quality_store else None
            ) or ForecastQualityStore()
            primary[SunTimes] = self._read_sun_times(now)

            current_reading: BatteryReading | None = primary.get(BatteryReading)
            with self._guarded("capacity observation"):
                if current_reading is not None:
                    obs = self._build_capacity_observation(current_reading, now)
                    if obs is not None:
                        self._capacity_estimator.add_observation(obs)
                        if self._capacity_store is not None:
                            await self._capacity_store.save(self._capacity_estimator)
                    self._last_battery_reading = current_reading
                    self._last_battery_reading_at = now

            primary[EstimatedCapacity] = EstimatedCapacity(
                value_kwh=self._capacity_estimator.estimated_capacity_kwh
            )

            primary[SchedulePolicy] = SchedulePolicy(
                use_standby=self.use_standby,
                allow_grid_charging=self.allow_grid_charging,
                allow_feed_in=self.allow_feed_in,
                allow_discharge_to_grid=self.allow_discharge_to_grid,
                mode_change_penalty_eur_per_kwh=_clamp(
                    self.mode_change_penalty_eur_per_kwh,
                    SCHEDULE_MODE_CHANGE_PENALTY_MIN,
                    SCHEDULE_MODE_CHANGE_PENALTY_MAX,
                ),
                profitability_tilt_alpha=_clamp(
                    self.profitability_tilt_alpha,
                    SCHEDULE_PROFITABILITY_TILT_ALPHA_MIN,
                    SCHEDULE_PROFITABILITY_TILT_ALPHA_MAX,
                ),
                terminal_value_discount=_clamp(
                    self.terminal_value_discount,
                    SCHEDULE_TERMINAL_VALUE_DISCOUNT_MIN,
                    SCHEDULE_TERMINAL_VALUE_DISCOUNT_MAX,
                ),
                max_discharge_to_grid_kw=(
                    _clamp(
                        self.max_discharge_to_grid_kw,
                        SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MIN,
                        SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MAX,
                    )
                    if self.max_discharge_to_grid_kw is not None
                    else None
                ),
            )

            secondary = await self._engine.run(primary, self._sun_sale_config, now)

            # --- Inverter actuation, ahead of the persistence bookkeeping.
            #     A dispatch failure is logged but never blanks the sensor set,
            #     and the accounting below can never block the inverter write
            #     (it ran already). The verify loop recovers a failed write. ---
            try:
                await self._dispatch_inverter_mode(primary, secondary, now)
            except Exception:    # keep sensors available; verify loop recovers
                _LOGGER.error("sunSale inverter dispatch failed", exc_info=True)

            # --- Persistence bookkeeping: best-effort, never fatal to the cycle
            #     nor to the dispatch above. ---
            await self._persist_secondary_outputs(primary, secondary, now)

            return self._build_sensor_dict(primary, secondary)

        except Exception as exc:
            raise UpdateFailed(f"Error updating sunSale data: {exc}") from exc

    async def _dispatch_inverter_mode(
        self, primary: dict, secondary: dict, now: datetime,
    ) -> None:
        """Run the control-module tick and surface its dispatch/verify state.

        Pulled out of ``_async_update_data`` and run *before* the persistence
        bookkeeping so an accounting failure can never block the inverter
        write. The caller guards this call, so a dispatch failure is logged
        without blanking the sensor set. Mutates ``primary[InverterModeHistory]``
        in place with the updated history.

        Args:
            primary: The cycle's primary-input dict; supplies the current
                ``InverterModeReading`` and receives the updated mode history.
            secondary: DAG outputs; supplies the current ``Schedule``.
            now: Current cycle timestamp.
        """
        reading: InverterModeReading | None = primary.get(InverterModeReading)
        if (
            self._control_module is None
            or reading is None
            or self._mode_history_store is None
        ):
            return

        schedule: Schedule | None = secondary.get(Schedule)
        history_before = (
            self._mode_history_store.value or InverterModeHistory(samples=())
        )
        updated_history = await self._control_module.tick(
            now=now,
            schedule=schedule,
            reading=reading,
            history=history_before,
            automation_enabled=self.automation_enabled,
            mode_override=self.mode_override,
        )
        if updated_history.samples != history_before.samples:
            await self._mode_history_store.save(updated_history)
        target = self._control_module.current_target(
            now, schedule, mode_override=self.mode_override,
        )
        # Surface module-level dispatch + verify state for the diagnostic
        # sensor — set every cycle regardless of write success so a stale
        # value never masks the current state.
        self._mirror_control_module_state()
        if self.automation_enabled and target is not None:
            self.last_dispatched_action = target.value
            self.last_dispatched_at = now
        primary[InverterModeHistory] = updated_history

    async def _persist_secondary_outputs(
        self, primary: dict, secondary: dict, now: datetime,
    ) -> None:
        """Persist DAG-derived bookkeeping: bake-ins and forecast/bill/price history.

        Best-effort and order-independent. Each block is guarded on its own so
        a single failure (a corrupt record, a ``try_bake_yesterday`` edge case)
        logs and is swallowed rather than aborting the cycle. Runs *after* the
        inverter dispatch, so a failure here can never block the inverter write.

        Args:
            primary: The cycle's primary-input dict (observed power histories,
                snapshot/baked stores).
            secondary: DAG outputs (``PriceSeries``, accuracy/bill results).
            now: Current cycle timestamp.
        """
        # Bake-in: idempotent per (date, side). Runs once the post-DAG
        # PriceSeries is available; updates take effect downstream on the
        # next coordinator cycle. No-op when no source can be resolved
        # before the hard cutoff.
        with self._guarded("observed bake-in"):
            pricing_for_bake: PriceSeries | None = secondary.get(PriceSeries)
            if (
                pricing_for_bake is not None
                and self._baked_observed_store is not None
            ):
                local_tz = self._sun_sale_config.local_tz
                baked_before = (
                    self._baked_observed_store.value
                    or BakedObservedHistory(records=())
                )
                snap_history = (
                    self._counter_snapshot_store.value
                    if self._counter_snapshot_store else None
                ) or CounterSnapshotHistory(records=())
                pv_samples = primary.get(PvPowerHistory)
                grid_import_samples = primary.get(GridImportPowerHistory)
                grid_export_samples = primary.get(GridExportPowerHistory)

                baked_after = baked_before
                if pv_samples is not None:
                    baked_after = try_bake_yesterday(
                        engine=build_generation_engine(local_tz),
                        samples_by_side={GENERATION_SIDE_ID: pv_samples.samples},
                        price_slots=pricing_for_bake.slots,
                        baked_history=baked_after,
                        snapshot_history=snap_history,
                        hass=self.hass,
                        raw_config=self._config,
                        now=now,
                        local_tz=local_tz,
                    )
                if grid_import_samples is not None or grid_export_samples is not None:
                    baked_after = try_bake_yesterday(
                        engine=build_grid_engine(local_tz),
                        samples_by_side={
                            GRID_IMPORT_SIDE_ID: (
                                grid_import_samples.samples if grid_import_samples else ()
                            ),
                            GRID_EXPORT_SIDE_ID: (
                                grid_export_samples.samples if grid_export_samples else ()
                            ),
                        },
                        price_slots=pricing_for_bake.slots,
                        baked_history=baked_after,
                        snapshot_history=snap_history,
                        hass=self.hass,
                        raw_config=self._config,
                        now=now,
                        local_tz=local_tz,
                    )

                if baked_after is not baked_before:
                    await self._baked_observed_store.save(baked_after)

        with self._guarded("forecast-quality save"):
            acc_result: ForecastAccuracyResult | None = secondary.get(ForecastAccuracyResult)
            if acc_result is not None and self._forecast_quality_store is not None:
                await self._forecast_quality_store.save(acc_result.quality)

        with self._guarded("monthly-bill save"):
            bill_result: MonthlyBillResult | None = secondary.get(MonthlyBillResult)
            if bill_result is not None and self._monthly_bill_store is not None:
                await self._monthly_bill_store.save(bill_result.updated_state)

        with self._guarded("price-history save"):
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
        imp_power: GridImportPowerReading | None = primary.get(GridImportPowerReading)
        exp_power: GridExportPowerReading | None = primary.get(GridExportPowerReading)
        # Signed grid power = import − export. Used by the dashboard sensor;
        # downstream pipeline reads each direction's history directly.
        grid_power_kw_signed = (
            (imp_power.power_kw if imp_power else 0.0)
            - (exp_power.power_kw if exp_power else 0.0)
        ) if (imp_power is not None or exp_power is not None) else 0.0
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
            "observed_grid": secondary.get(ObservedGridSeries),
            "observed_consumption": secondary.get(ObservedConsumptionSeries),
            "observed_losses": secondary.get(ObservedLossesSeries),
            "forecast_error": _acc.error_series if _acc else None,
            "calculation": secondary.get(CalculationResult),
            "schedule": secondary.get(Schedule),
            "battery_state": secondary.get(BatteryState),
            "battery_status": secondary.get(BatteryStatus),
            "degradation_cost": deg.value_kwh if deg else 0.0,
            "estimated_capacity": self._capacity_estimator.estimated_capacity_kwh,
            "prices": nordpool.entries if nordpool else [],
            "grid_power_kw": grid_power_kw_signed,
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
            "grid_import_power_history": primary.get(GridImportPowerHistory),
            "grid_export_power_history": primary.get(GridExportPowerHistory),
            "pv_power_history": primary.get(PvPowerHistory),
            "derived_power_history": primary.get(DerivedPowerHistory),
            "inverter_mode_history": primary.get(InverterModeHistory),
            "inverter_mode_reading": primary.get(InverterModeReading),
            "baked_observed_history": primary.get(BakedObservedHistory),
            "counter_snapshot_history": primary.get(CounterSnapshotHistory),
            "consumption_daily_buckets": primary.get(ConsumptionDailyBuckets),
            "today_generation_live_kwh": (
                primary.get(GenerationReading).today_total_kwh
                if primary.get(GenerationReading) is not None else None
            ),
            "today_imported_live_kwh": (
                primary.get(GridImportTodayReading).today_total_kwh
                if primary.get(GridImportTodayReading) is not None else None
            ),
            "today_exported_live_kwh": (
                primary.get(GridExportTodayReading).today_total_kwh
                if primary.get(GridExportTodayReading) is not None else None
            ),
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
            """Read a sun.sun datetime attribute as UTC-aware, or None when missing."""
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
            """Map a sun.sun "next_*" event to today's instance of that event."""
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

        Energy is integrated over the *real* elapsed time between the two readings,
        not the nominal update interval: off-cycle refreshes (mode-override changes,
        force_recalculate, startup) can land seconds apart, which would otherwise
        overstate energy by the ratio of nominal-to-actual interval.

        Args:
            current: Most recent battery reading.
            now: Cycle timestamp used as the observation timestamp and the interval
                endpoint.

        Returns:
            CapacityObservation when |soc_delta| >= CAPACITY_OBS_MIN_SOC_DELTA and the
            elapsed interval lies within
            [CAPACITY_OBS_MIN_INTERVAL_S, CAPACITY_OBS_MAX_INTERVAL_S]; None on the
            first cycle, when the delta is too small, or when the interval is
            implausibly short (off-cycle refresh) or long (stalled coordinator).
        """
        if self._last_battery_reading is None or self._last_battery_reading_at is None:
            return None
        soc_delta = abs(current.soc - self._last_battery_reading.soc)
        if soc_delta < CAPACITY_OBS_MIN_SOC_DELTA:
            return None
        interval_s = (now - self._last_battery_reading_at).total_seconds()
        if not CAPACITY_OBS_MIN_INTERVAL_S <= interval_s <= CAPACITY_OBS_MAX_INTERVAL_S:
            return None
        avg_power = (abs(self._last_battery_reading.power_kw) + abs(current.power_kw)) / 2.0
        energy_kwh = avg_power * (interval_s / 3600.0)
        direction = "charge" if current.soc > self._last_battery_reading.soc else "discharge"
        return CapacityObservation(
            timestamp=now,
            soc_start=self._last_battery_reading.soc,
            soc_end=current.soc,
            energy_kwh=energy_kwh,
            direction=direction,
        )
