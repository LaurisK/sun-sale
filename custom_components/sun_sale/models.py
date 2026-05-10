"""Shared data structures for sunSale. No logic, no HA imports."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class Action(Enum):
    """What the system should do in a given hour slot."""
    IDLE = "idle"
    CHARGE_FROM_GRID = "charge_from_grid"
    DISCHARGE_TO_GRID = "discharge_to_grid"
    CHARGE_FROM_SOLAR = "charge_from_solar"


@dataclass(frozen=True)
class HourlyPrice:
    """Nordpool spot price for one hour."""
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


@dataclass(frozen=True)
class EVChargerConfig:
    """User-configured EV charger parameters."""
    max_charge_power_kw: float
    min_charge_power_kw: float
    battery_capacity_kwh: float   # EV battery size


@dataclass
class EVChargerState:
    """Current observed EV charger state."""
    is_plugged_in: bool
    soc: float                         # 0.0–1.0, current EV SoC
    target_soc: float                  # 0.0–1.0, desired SoC by departure
    departure_time: datetime | None = None


@dataclass(frozen=True)
class EVChargeSlot:
    """One hour of EV charge schedule."""
    start: datetime
    end: datetime
    charge_power_kw: float   # 0 = don't charge this hour
    cost_eur: float


@dataclass
class EVSchedule:
    """Complete EV charge plan."""
    slots: list[EVChargeSlot]
    total_cost_eur: float
    total_energy_kwh: float
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
    sell_allowed: bool    # sell_eur_kwh > 0
    sources: tuple[str, ...]  # ("nordpool", "tariff") — for diagnostics


@dataclass(frozen=True)
class PriceSeries:
    """Normalised price stream for all known slots."""
    slots: tuple[PriceSlot, ...]
    resolution: timedelta
    computed_at: datetime

    def slot_at(self, t: datetime) -> PriceSlot | None:
        return next((s for s in self.slots if s.start <= t < s.end), None)

    def window(self, t1: datetime, t2: datetime) -> tuple[PriceSlot, ...]:
        return tuple(s for s in self.slots if s.end > t1 and s.start < t2)


@dataclass(frozen=True)
class GenerationSlot:
    """Predicted solar generation for one time slot."""
    start: datetime
    end: datetime
    expected_kwh: float
    source: str           # "open_meteo" | "forecast_solar" | "frozen_morning"
    confidence: float | None  # 0..1 if source provides it, else None


@dataclass(frozen=True)
class GenerationSeries:
    """Normalised generation forecast from one or more sources."""
    slots: tuple[GenerationSlot, ...]
    primary: str          # which source the calculator should consume by default
    overlays: tuple[str, ...]  # other sources kept for chart overlay
    computed_at: datetime

    def energy_between(self, t1: datetime, t2: datetime) -> float:
        """Return expected kWh from the primary source between t1 and t2."""
        total = 0.0
        for s in self.slots:
            if s.source != self.primary:
                continue
            overlap_start = max(s.start, t1)
            overlap_end = min(s.end, t2)
            if overlap_start >= overlap_end:
                continue
            slot_secs = (s.end - s.start).total_seconds()
            overlap_secs = (overlap_end - overlap_start).total_seconds()
            total += s.expected_kwh * (overlap_secs / slot_secs)
        return total


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
class NordpoolPrices:
    """Primary data: Nordpool prices at configured resolution + raw 15-min dict."""
    slots: list[HourlyPrice]            # at configured resolution (for PricingNode)
    raw_15min: dict[datetime, float]    # always 15-min spot prices EUR/kWh (for DashboardNode)


@dataclass
class RawSolarData:
    """Primary data: raw solar forecast from HA entities."""
    watts: dict[datetime, float]        # Open Meteo: {slot_utc: W}
    forecast_slots: list[dict]          # Forecast.Solar: [{time, pv_estimate/energy, ...}]


@dataclass(frozen=True)
class BatteryReading:
    """Primary data: raw inverter telemetry for one cycle."""
    soc: float                  # 0.0–1.0
    power_kw: float             # positive = charging, negative = discharging
    grid_power_kw: float        # positive = importing
    household_load_kw: float    # current load in kW (0.2 kW default if unavailable)


@dataclass(frozen=True)
class EstimatedCapacity:
    """Primary data: current CapacityEstimator result, set by coordinator pre-DAG."""
    value_kwh: float


# ---------------------------------------------------------------------------
# DAG secondary output wrapper types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DegradationCost:
    """Cost per kWh of cycling the battery (secondary output of DegradationNode)."""
    value_kwh: float


@dataclass
class DashboardData:
    """Presentation data produced by DashboardNode (sink output)."""
    future_slots: list[dict]
    solar_frozen_forecast: list[dict]


# ---------------------------------------------------------------------------
# Structured integration config (used by DAG engine and nodes)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SunSaleConfig:
    """All user configuration, structured for DAG nodes."""
    tariff: TariffConfig
    battery: BatteryConfig
    ev: EVChargerConfig | None
