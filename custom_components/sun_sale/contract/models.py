"""Shared data structures for sunSale. No logic, no HA imports."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from enum import Enum


class Action(Enum):
    """What the system should do in a given hour slot."""
    IDLE = "idle"
    CHARGE_FROM_GRID = "charge_from_grid"
    DISCHARGE_TO_GRID = "discharge_to_grid"
    CHARGE_FROM_SOLAR = "charge_from_solar"


@dataclass(frozen=True)
class PriceEntry:
    """Nordpool spot price for one time slot."""
    start: datetime
    end: datetime
    price_eur_kwh: float


@dataclass(frozen=True)
class TariffConfig:
    """User-configured tariff formula parameters."""
    distribution_fee: float       # EUR/kWh, grid distribution component when buying
    tax_rate: float               # Fractional, e.g. 0.21 for 21% VAT on buy
    markup: float                 # EUR/kWh, retailer margin when buying
    sell_distribution_fee: float  # EUR/kWh, distribution deducted when selling
    sell_tax_rate: float          # Fractional tax deducted from sell revenue
    sell_markup: float            # EUR/kWh, retailer margin deducted when selling


@dataclass(frozen=True)
class TariffResult:
    """Computed effective buy/sell prices for one hour."""
    hour: datetime
    spot_price: float
    buy_price: float   # Total cost to buy 1 kWh from grid (EUR/kWh)
    sell_price: float  # Total revenue from selling 1 kWh to grid (EUR/kWh)


@dataclass(frozen=True)
class BatteryConfig:
    """User-configured battery parameters (immutable)."""
    nominal_capacity_kwh: float
    purchase_price_eur: float
    rated_cycle_life: int
    max_charge_power_kw: float
    max_discharge_power_kw: float
    min_soc: float               # Minimum SoC to maintain (0.0–1.0)
    max_soc: float               # Maximum SoC to charge to (0.0–1.0)
    round_trip_efficiency: float  # e.g. 0.90 for 90%
    nominal_voltage_v: float = 48.0  # DC bus voltage; used for kW→A conversion on Solis


@dataclass
class BatteryState:
    """Current observed battery state."""
    soc: float                       # 0.0–1.0
    estimated_capacity_kwh: float    # Learned usable capacity


@dataclass(frozen=True)
class BatteryStatus:
    """Normalised battery snapshot: configured limits + current telemetry.

    Produced by `inbound/battery.py` from BatteryConfig + BatteryReading.
    Total capacity is the configured nominal value; remaining capacity is
    derived from the observed SoC.
    """
    total_capacity_kwh: float
    max_charge_power_kw: float
    max_discharge_power_kw: float
    soc: float                       # 0.0–1.0
    remaining_capacity_kwh: float    # soc * total_capacity_kwh


@dataclass(frozen=True)
class SolarForecast:
    """Predicted solar generation for one hour."""
    start: datetime
    end: datetime
    generation_kwh: float


@dataclass(frozen=True)
class ScheduleSlot:
    """One hour of the optimized battery schedule."""
    start: datetime
    end: datetime
    action: Action
    power_kw: float              # Energy exchanged in this slot (kWh at 1h resolution)
    expected_soc_after: float    # Predicted battery SoC at end of slot
    expected_profit_eur: float   # Profit (negative = cost) from this action
    reason: str                  # Human-readable explanation


@dataclass
class Schedule:
    """Complete battery optimization result."""
    slots: list[ScheduleSlot]
    total_expected_profit_eur: float
    degradation_cost_per_kwh: float
    computed_at: datetime


class ChargeMode(Enum):
    """Per-slot disposition of today's remaining solar generation."""
    SOLAR_CHARGE = "solar_charge"   # store solar in battery
    SELL = "sell"                    # export to grid (sell_eur_kwh > 0)
    NO_EXPORT = "no_export"          # excess solar but sell_eur_kwh <= 0 → curtail
    IDLE = "idle"                    # no generation this slot


@dataclass(frozen=True)
class ChargingProfileSlot:
    """One slot of today's charging profile."""
    start: datetime
    end: datetime
    mode: ChargeMode
    expected_kwh: float
    sell_eur_kwh: float          # for traceability


@dataclass(frozen=True)
class ChargingProfile:
    """Today's solar disposition: which slots go to battery, sell, or curtail."""
    slots: tuple[ChargingProfileSlot, ...]   # today's remaining slots only
    free_capacity_kwh: float                  # (max_soc - soc) * total_capacity_kwh
    today_remaining_generation_kwh: float
    solar_exceeds_capacity: bool              # case 1 (False) vs case 2 (True)
    allocated_solar_kwh: float                # sum of SOLAR_CHARGE slot kWh
    total_no_export_kwh: float                # sum of NO_EXPORT slot kWh
    computed_at: datetime


@dataclass(frozen=True)
class CapacityObservation:
    """One charge/discharge observation for capacity learning."""
    timestamp: datetime
    soc_start: float
    soc_end: float
    energy_kwh: float   # Measured energy throughput
    direction: str      # "charge" or "discharge"


# ---------------------------------------------------------------------------
# Pipeline stage data contracts (Pricing → Forecast → Calculator → Schedule)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PriceSlot:
    """Effective buy/sell prices for one time slot."""
    start: datetime
    end: datetime
    buy_eur_kwh: float    # effective buy price (spot + fees + tax)
    sell_eur_kwh: float   # effective sell price — can be NEGATIVE
    spot_eur_kwh: float   # raw nordpool, kept for provenance
    sources: tuple[str, ...]  # ("nordpool", "tariff") — for diagnostics


@dataclass(frozen=True)
class PriceSeries:
    """Normalised price stream for all known slots."""
    slots: tuple[PriceSlot, ...]
    resolution: timedelta
    computed_at: datetime

    def slot_at(self, t: datetime) -> PriceSlot | None:
        """Return the slot covering time t, or None if outside the series.

        Args:
            t: Timezone-aware datetime to look up.

        Returns:
            Matching PriceSlot, or None.
        """
        return next((s for s in self.slots if s.start <= t < s.end), None)

    def window(self, t1: datetime, t2: datetime) -> tuple[PriceSlot, ...]:
        """Return all slots that overlap the half-open interval [t1, t2).

        Args:
            t1: Start of the query window (inclusive).
            t2: End of the query window (exclusive).

        Returns:
            Tuple of overlapping PriceSlots, may be empty.
        """
        return tuple(s for s in self.slots if s.end > t1 and s.start < t2)


@dataclass(frozen=True)
class GenerationSlot:
    """One price-grid-aligned forecast slot.

    start/end mirror the pricing grid (1h or 15-min). expected_kwh is overlap-weighted
    when the raw forecast resolution differs from the grid.
    """
    start: datetime
    end: datetime
    expected_kwh: float


@dataclass(frozen=True)
class SolarEntry:
    """Solar generation for one time slot, any source."""
    start: datetime
    end: datetime
    expected_kwh: float
    source: str


@dataclass(frozen=True)
class GenerationSeries:
    """Output of the forecast stage. Deliverables:

    - slots: price-grid-aligned GenerationSlots covering yesterday 00:00 → tomorrow 23:59,
      one per pricing slot. Consumed by calculator (energy_between, per-slot expected solar
      for lockout logic) and charging_profile (today's remaining slots to decide store vs sell).
    - today_remaining_kwh: sum of expected_kwh for today's slots with start >= now.
      Consumed by charging_profile to select case 1 (fits in battery) vs case 2 (sell excess).
    - total_yesterday_kwh, total_today_kwh, total_tomorrow_kwh: full-day sums over the slot
      grid, sensor attributes only.
    - total_d2_kwh … total_d6_kwh: daily totals for days 2–6 ahead, summed directly from raw
      HA entity watts (outside the price grid, no per-slot data). Sensor attributes only.
    """
    slots: tuple[GenerationSlot, ...]
    total_yesterday_kwh: float = 0.0
    total_today_kwh: float = 0.0
    total_tomorrow_kwh: float = 0.0
    today_remaining_kwh: float = 0.0
    total_d2_kwh: float = 0.0
    total_d3_kwh: float = 0.0
    total_d4_kwh: float = 0.0
    total_d5_kwh: float = 0.0
    total_d6_kwh: float = 0.0

    def energy_between(self, t1: datetime, t2: datetime) -> float:
        """Return expected kWh between t1 and t2 via proportional overlap.

        Args:
            t1: Start of query window (tz-aware).
            t2: End of query window (tz-aware).

        Returns:
            Sum of pro-rated expected_kwh across all overlapping slots.
        """
        total = 0.0
        for s in self.slots:
            overlap_start = max(s.start, t1)
            overlap_end = min(s.end, t2)
            if overlap_start >= overlap_end:
                continue
            slot_secs = (s.end - s.start).total_seconds()
            overlap_secs = (overlap_end - overlap_start).total_seconds()
            total += s.expected_kwh * (overlap_secs / slot_secs)
        return total


@dataclass(frozen=True)
class ObservedGenerationSlot:
    """Observed (measured) solar generation for one time slot."""
    start: datetime
    end: datetime
    generated_kwh: float
    source: str           # "inverter"


@dataclass(frozen=True)
class ObservedGenerationSeries:
    """Per-slot observed generation aligned to PriceSeries resolution.

    Covers yesterday 00:00 → now. Slots whose start is in the future of the
    last sample are simply absent (or zero where partial overlap occurs).
    """
    slots: tuple[ObservedGenerationSlot, ...]
    computed_at: datetime
    total_yesterday_kwh: float = 0.0
    total_today_so_far_kwh: float = 0.0


@dataclass(frozen=True)
class ForecastErrorSlot:
    """Per-slot forecast vs. observed solar generation delta.

    `error_kwh = observed_kwh - forecast_kwh`. Positive means the forecast
    under-predicted (actual generation exceeded the forecast); negative means
    it over-predicted. `relative_error` is `error_kwh / forecast_kwh` when
    the forecast is non-zero; None when the forecast was zero (relative error
    is undefined and would otherwise blow up the series-level MAPE).
    """
    start: datetime
    end: datetime
    forecast_kwh: float
    observed_kwh: float
    error_kwh: float
    relative_error: float | None


@dataclass(frozen=True)
class ForecastErrorSeries:
    """Aligned forecast/observed error series over the overlap window.

    Slots are those for which both a forecast and an observation exist (the
    observed series covers yesterday 00:00 → now, so future slots are absent).
    Statistics summarise the slots in this series and are intended to feed a
    future calibration/correction stage that minimises forecast error.
    """
    slots: tuple[ForecastErrorSlot, ...]
    total_forecast_kwh: float
    total_observed_kwh: float
    total_error_kwh: float           # sum of signed errors
    mean_absolute_error_kwh: float    # MAE per slot (mean of |error_kwh|)
    bias_kwh: float                    # mean signed error per slot
    mean_absolute_percentage_error: float | None    # MAPE (forecast-weighted); None when total_forecast_kwh == 0
    computed_at: datetime


@dataclass(frozen=True)
class SlotDecision:
    """Per-slot decision flags produced by the Calculator stage."""
    start: datetime
    end: datetime
    sell_allowed: bool              # False if sell_eur_kwh <= 0
    expected_solar_kwh: float       # expected generation this slot
    expected_solar_negative_sale_kwh: float  # production during negative-sale (reported only)
    notes: tuple[str, ...]          # e.g. ("battery_full_during_lockout", "paid_to_charge")


@dataclass(frozen=True)
class CalculationResult:
    """Output of the Calculator stage."""
    slots: tuple[SlotDecision, ...]
    feed_in_lockout_windows: tuple[tuple[datetime, datetime], ...]
    total_negative_sale_kwh: float  # sum across locked-out slots — reported, no decision
    computed_at: datetime


# ---------------------------------------------------------------------------
# Translation-layer primary types (HA state → domain, produced by translators)
# ---------------------------------------------------------------------------

@dataclass
class NordpoolData:
    """Primary data: Nordpool prices for today + tomorrow as read from HA."""
    entries: list[PriceEntry]    # sorted by start; auto-detected resolution
    resolution: timedelta        # 15min or 1h, detected from data


@dataclass(frozen=True)
class YesterdayPrices:
    """Primary data: yesterday's Nordpool prices loaded from persistent storage.

    Combined with NordpoolData by inbound/pricing to form the 72h PriceSeries.
    """
    entries: tuple[PriceEntry, ...]  # sorted by start; empty on first cycle


@dataclass
class SolarData:
    """Primary data: unified solar forecast for yesterday + today + tomorrow."""
    entries: list[SolarEntry]        # sorted by start, all panels combined
    total_today_kwh: float           # sum of expected_kwh for today's slots
    today_remaining_kwh: float       # sum of today's slots with start >= now
    primary_source: str              # "open_meteo" | "forecast_solar" | "none"


@dataclass(frozen=True)
class BatteryReading:
    """Primary data: raw inverter telemetry for one cycle."""
    soc: float                  # 0.0–1.0
    power_kw: float             # positive = charging, negative = discharging
    grid_power_kw: float        # positive = importing
    household_load_kw: float    # current load in kW (0.2 kW default if unavailable)


@dataclass(frozen=True)
class GenerationReading:
    """Primary data: one snapshot of the inverter's today-total kWh counter.

    The counter is cumulative and resets at local midnight; per-slot energy is
    derived by differencing consecutive samples in `GenerationHistory`.
    """
    today_total_kwh: float
    timestamp: datetime


@dataclass(frozen=True)
class GenerationHistory:
    """Primary data: ordered samples of the inverter today-total counter.

    Coordinator appends each cycle's `GenerationReading` and persists the
    rolling list (≥ 2 days) so the inbound module can difference samples
    across yesterday → now.
    """
    samples: tuple[GenerationReading, ...]   # sorted by timestamp ascending


@dataclass(frozen=True)
class EstimatedCapacity:
    """Primary data: current CapacityEstimator result, set by coordinator pre-DAG."""
    value_kwh: float


@dataclass(frozen=True)
class HouseholdConsumptionReading:
    """Primary data: one snapshot of the inverter's household-consumption today-total kWh counter.

    The counter is cumulative and resets at local midnight; this is just the
    most recent observed value, so consumers can show "consumption so far
    today" without re-deriving it from instantaneous load samples.
    """
    today_total_kwh: float
    timestamp: datetime


@dataclass(frozen=True)
class HouseholdLoadReading:
    """Primary data: one snapshot of measured household load (per cycle).

    Distinct from `BatteryReading.household_load_kw`: that field carries a
    0.2 kW stub when the sensor is unavailable, so downstream calculator/
    dashboard always have a number. This reading returns None on absence
    so the persisted history isn't polluted (see docs/base_load_missing.md §8).
    """
    timestamp: datetime
    load_kw: float


@dataclass(frozen=True)
class HouseholdLoadSample:
    """One persisted sample of measured household load (in HouseholdLoadHistory).

    Same shape as HouseholdLoadReading; separate type so the persisted-storage
    schema can evolve independently of the per-cycle primary type.
    """
    timestamp: datetime
    load_kw: float


@dataclass(frozen=True)
class HouseholdLoadHistory:
    """Primary data: rolling history of household-load samples.

    Coordinator appends each cycle (when the sensor was available) and
    persists ~45 days. Consumed by `BaseLoadProfileNode`.
    """
    samples: tuple[HouseholdLoadSample, ...]   # sorted by timestamp ascending


class DayClass(Enum):
    """Bucket for daily peak prices, ordered cheapest → most expensive on average."""
    WEEKEND = "weekend"     # Sat/Sun (and any holiday falling on these)
    HOLIDAY = "holiday"     # weekday public holiday
    WEEKDAY = "weekday"     # normal working day


@dataclass(frozen=True)
class DailyPeak:
    """One day's peak Nordpool spot price + its day-class bucket."""
    day: date
    peak_eur_kwh: float
    day_class: DayClass


@dataclass(frozen=True)
class PriceHistory:
    """Primary data: rolling history of daily peaks (≤ ~90 days)."""
    peaks: tuple[DailyPeak, ...]    # sorted by day ascending


@dataclass(frozen=True)
class ProfitabilityScore:
    """Secondary data: today's profitability relative to recent history.

    `score` is None when history is too sparse to be meaningful
    (< MIN_HISTORY_DAYS days in the rolling window).
    """
    score: float | None             # 0.0–1.0, percentile rank; None when sparse
    today_peak_eur_kwh: float
    today_class: DayClass
    class_medians: dict             # DayClass → median peak in long window
    window_days: int                # days actually used for percentile rank
    computed_at: datetime


# ---------------------------------------------------------------------------
# DAG secondary output wrapper types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DegradationCost:
    """Cost per kWh of cycling the battery (secondary output of DegradationNode)."""
    value_kwh: float


@dataclass(frozen=True)
class BaseLoadSlot:
    """Per-hour baseload floor for one local hour-of-day bucket.

    `is_fallback` is True when the bucket had fewer than MIN_BUCKET_SAMPLES
    observations and the value came from the profile's cross-bucket fallback.
    """
    hour: int                    # 0..23 in local time
    baseload_kw: float
    sample_count: int
    is_fallback: bool


@dataclass(frozen=True)
class BaseLoadProfile:
    """24-hour (local time) profile of estimated minimum household draw.

    Indexed by local hour 0..23. `slots` is always length 24 — sparse buckets
    receive `fallback_kw` so callers never have to handle None. `confidence`
    is None when total history covers fewer than MIN_HISTORY_DAYS local days.
    """
    slots: tuple[BaseLoadSlot, ...]    # length 24, indexed by hour
    fallback_kw: float                  # used for sparse buckets and as a stub
    overall_p10_kw: float               # diagnostic: P10 of all samples
    overall_median_kw: float            # diagnostic
    confidence: float | None            # 0..1, distinct_days / window_days; None when sparse
    sample_count: int                   # total samples in window
    distinct_days: int                  # distinct local-date days in window
    computed_at: datetime

    def at(self, t: datetime, local_tz) -> float:
        """Return the baseload floor kW for the local hour containing t.

        Args:
            t: Any tz-aware datetime.
            local_tz: Timezone used to map t to a local hour-of-day bucket.

        Returns:
            Estimated minimum household draw in kW for that hour.
        """
        return self.slots[t.astimezone(local_tz).hour].baseload_kw


@dataclass(frozen=True)
class BatteryRuntimeEstimate:
    """How long the battery can sustain household baseload before hitting min_soc.

    Forward-simulates net drain (baseload − forecast solar) from `now` over
    `horizon_hours`. Intentionally does NOT account for the optimizer's
    scheduled discharge/charge — the output is a "baseload-only reserve".
    `runtime_minutes` and `until` are None when the battery never drains
    within the horizon (e.g. forecast solar covers baseload).
    """
    remaining_kwh_usable: float         # max(0, (soc − min_soc) * total_capacity)
    avg_drain_kw_next_hour: float       # mean net_drain over the first simulated hour
    runtime_minutes: float | None
    until: datetime | None              # now + runtime_minutes, tz-aware
    horizon_hours: int
    computed_at: datetime


# ---------------------------------------------------------------------------
# Structured integration config (used by DAG engine and nodes)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SunSaleConfig:
    """All user configuration, structured for DAG nodes.

    `local_tz` defaults to UTC for tests / installs without HA's timezone
    set; the coordinator populates it from `hass.config.time_zone` at setup.
    """
    tariff: TariffConfig
    battery: BatteryConfig
    local_tz: tzinfo = field(default=timezone.utc)
