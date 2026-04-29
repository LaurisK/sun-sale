"""Battery degradation model and capacity estimator.

Pure Python — no Home Assistant imports.
"""
from __future__ import annotations

from datetime import datetime

from .const import CAPACITY_OBS_MIN_SOC_DELTA
from .models import BatteryConfig, BatteryState, CapacityObservation


def degradation_cost_per_kwh(config: BatteryConfig, state: BatteryState) -> float:
    """Cost of cycling 1 kWh through the battery.

    = purchase_price / (rated_cycle_life * estimated_capacity_kwh * 2)
    The *2 accounts for one full cycle = one charge + one discharge.
    """
    return config.purchase_price_eur / (
        config.rated_cycle_life * state.estimated_capacity_kwh * 2.0
    )


def trade_profit_per_kwh(
    buy_tariff: float,
    sell_tariff: float,
    deg_cost: float,
    efficiency: float,
) -> float:
    """Net profit per kWh stored: sell_revenue - buy_cost - degradation.

    Degradation is counted twice (once charging, once discharging).
    Efficiency reduces the kWh available to sell.
    """
    return sell_tariff * efficiency - buy_tariff - deg_cost * 2.0


class CapacityEstimator:
    """Learns actual usable battery capacity from observed charge/discharge cycles.

    Uses an exponentially-decayed weighted average: recent observations have
    higher weight (DECAY^0 = 1.0) while older ones decay by DECAY per position.
    """

    DECAY = 0.9  # Weight of each observation relative to the next newer one

    def __init__(
        self,
        nominal_capacity_kwh: float,
        observations: list[CapacityObservation] | None = None,
    ) -> None:
        self._nominal = nominal_capacity_kwh
        self._observations: list[CapacityObservation] = list(observations or [])

    def add_observation(self, obs: CapacityObservation) -> None:
        """Record one charge/discharge observation. Discards noisy small-delta data."""
        if abs(obs.soc_end - obs.soc_start) < CAPACITY_OBS_MIN_SOC_DELTA:
            return
        self._observations.append(obs)

    @property
    def estimated_capacity_kwh(self) -> float:
        """Current best estimate of usable capacity in kWh."""
        implied = [
            obs.energy_kwh / abs(obs.soc_end - obs.soc_start)
            for obs in self._observations
            if abs(obs.soc_end - obs.soc_start) >= CAPACITY_OBS_MIN_SOC_DELTA
        ]
        if not implied:
            return self._nominal

        n = len(implied)
        weighted_sum = 0.0
        total_weight = 0.0
        for i, cap in enumerate(implied):
            w = self.DECAY ** (n - 1 - i)  # Newer observations (higher i) get weight closer to 1
            weighted_sum += w * cap
            total_weight += w

        return weighted_sum / total_weight

    def to_dict(self) -> dict:
        """Serialize for HA persistent storage."""
        return {
            "nominal_capacity_kwh": self._nominal,
            "observations": [
                {
                    "timestamp": obs.timestamp.isoformat(),
                    "soc_start": obs.soc_start,
                    "soc_end": obs.soc_end,
                    "energy_kwh": obs.energy_kwh,
                    "direction": obs.direction,
                }
                for obs in self._observations
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CapacityEstimator":
        """Deserialize from HA persistent storage."""
        observations = [
            CapacityObservation(
                timestamp=datetime.fromisoformat(o["timestamp"]),
                soc_start=o["soc_start"],
                soc_end=o["soc_end"],
                energy_kwh=o["energy_kwh"],
                direction=o["direction"],
            )
            for o in data.get("observations", [])
        ]
        return cls(nominal_capacity_kwh=data["nominal_capacity_kwh"], observations=observations)
