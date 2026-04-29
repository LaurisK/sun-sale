"""Shared data structures for sunSale. No logic, no HA imports."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
