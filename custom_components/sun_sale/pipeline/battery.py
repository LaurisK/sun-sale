"""Battery degradation model and capacity estimator.

Pure Python — no Home Assistant imports.
"""
from __future__ import annotations

from datetime import datetime

from ..contract.const import CAPACITY_OBS_MIN_SOC_DELTA
from ..contract.models import BatteryConfig, BatteryState, CapacityObservation

# Implied capacity more than this multiple of nominal is physically impossible
# given the inverter's charge/discharge limits and the 5-minute update cycle.
# Acts as a guard against sensor-unit bugs (e.g. W read as kW) corrupting the
# weighted-average estimator.
_CAPACITY_OBS_MAX_MULTIPLIER = 2.0


def degradation_cost_per_kwh(config: BatteryConfig, state: BatteryState) -> float:
    """Compute the wear cost per kWh cycled through the battery.

    Formula: purchase_price / (rated_cycle_life * estimated_capacity_kwh * 2).
    The *2 accounts for one full cycle = one charge + one discharge.

    Args:
        config: Battery configuration including purchase price and cycle life.
        state: Current battery state containing the learned capacity estimate.

    Returns:
        Degradation cost in EUR/kWh.
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
    """Compute net profit per kWh charged: sell_revenue - buy_cost - degradation.

    Degradation is counted twice (once charging, once discharging).
    Efficiency reduces the kWh available to sell.

    Args:
        buy_tariff: Effective buy price in EUR/kWh.
        sell_tariff: Effective sell price in EUR/kWh.
        deg_cost: Degradation cost per kWh (from degradation_cost_per_kwh).
        efficiency: Round-trip efficiency (0.0–1.0).

    Returns:
        Net profit in EUR per kWh charged; negative means unprofitable.
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
        """Initialise estimator with the nameplate capacity and optional history.

        Args:
            nominal_capacity_kwh: Nameplate battery capacity; used as fallback
                when no observations are available.
            observations: Previously recorded charge/discharge observations to
                seed the estimator on startup.
        """
        self._nominal = nominal_capacity_kwh
        self._observations: list[CapacityObservation] = list(observations or [])

    def add_observation(self, obs: CapacityObservation) -> None:
        """Record a charge/discharge observation; silently discards bad samples.

        Rejected when ``|soc_delta| < CAPACITY_OBS_MIN_SOC_DELTA`` (too small
        to yield a reliable implied capacity) or when the implied capacity
        exceeds ``nominal × _CAPACITY_OBS_MAX_MULTIPLIER`` (sensor-unit bug
        or transient reading artifact that would corrupt the weighted average).

        Args:
            obs: Observation to append.
        """
        soc_delta = abs(obs.soc_end - obs.soc_start)
        if soc_delta < CAPACITY_OBS_MIN_SOC_DELTA:
            return
        implied = obs.energy_kwh / soc_delta
        if implied > self._nominal * _CAPACITY_OBS_MAX_MULTIPLIER:
            return
        self._observations.append(obs)

    @property
    def estimated_capacity_kwh(self) -> float:
        """Current best estimate of usable capacity in kWh."""
        max_plausible = self._nominal * _CAPACITY_OBS_MAX_MULTIPLIER
        implied = [
            obs.energy_kwh / abs(obs.soc_end - obs.soc_start)
            for obs in self._observations
            if abs(obs.soc_end - obs.soc_start) >= CAPACITY_OBS_MIN_SOC_DELTA
            and obs.energy_kwh / abs(obs.soc_end - obs.soc_start) <= max_plausible
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

    def debug_observations(self) -> dict:
        """Return per-observation diagnostics for auditing the capacity estimate.

        Exposes every stored observation with its implied capacity
        (``energy_kwh / |soc_delta|``), whether it currently passes the
        plausibility filters, and the exponential weight it carries in the
        weighted average. Lets the debug API show *why* the estimate sits where
        it does — e.g. a run of low-implied discharge samples dragging it below
        nominal. Diagnostic only; nothing in the pipeline consumes it.

        Returns:
            Dict with the live ``estimated_capacity_kwh``, ``nominal_capacity_kwh``,
            total/accepted observation counts, and an ``observations`` list of
            per-sample rows in storage order (newest last).
        """
        max_plausible = self._nominal * _CAPACITY_OBS_MAX_MULTIPLIER

        def _implied(obs: CapacityObservation) -> float | None:
            """Return implied capacity for one observation, or None when Δsoc is 0."""
            d = abs(obs.soc_end - obs.soc_start)
            return obs.energy_kwh / d if d > 0 else None

        accepted = [
            o for o in self._observations
            if abs(o.soc_end - o.soc_start) >= CAPACITY_OBS_MIN_SOC_DELTA
            and (_implied(o) or float("inf")) <= max_plausible
        ]
        n = len(accepted)

        rows: list[dict] = []
        acc_i = 0
        for obs in self._observations:
            implied = _implied(obs)
            is_acc = (
                abs(obs.soc_end - obs.soc_start) >= CAPACITY_OBS_MIN_SOC_DELTA
                and implied is not None
                and implied <= max_plausible
            )
            weight = self.DECAY ** (n - 1 - acc_i) if is_acc else 0.0
            if is_acc:
                acc_i += 1
            rows.append({
                "timestamp": obs.timestamp.isoformat(),
                "direction": obs.direction,
                "soc_start": round(obs.soc_start, 4),
                "soc_end": round(obs.soc_end, 4),
                "soc_delta": round(obs.soc_end - obs.soc_start, 4),
                "energy_kwh": round(obs.energy_kwh, 4),
                "implied_capacity_kwh": (
                    round(implied, 3) if implied is not None else None
                ),
                "accepted": is_acc,
                "weight": round(weight, 4),
            })

        return {
            "estimated_capacity_kwh": round(self.estimated_capacity_kwh, 4),
            "nominal_capacity_kwh": self._nominal,
            "count": len(self._observations),
            "accepted_count": n,
            "observations": rows,
        }

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
        """Deserialise from the HA persistent-storage dict format.

        Args:
            data: Dict previously produced by to_dict().

        Returns:
            Restored CapacityEstimator with all historical observations.
        """
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
