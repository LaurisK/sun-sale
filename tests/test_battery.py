"""Tests for battery.py — pure Python, no HA required."""
from datetime import datetime, timezone
import pytest
from custom_components.sun_sale.battery import (
    CapacityEstimator,
    degradation_cost_per_kwh,
    trade_profit_per_kwh,
)
from custom_components.sun_sale.models import BatteryConfig, BatteryState, CapacityObservation
from tests.conftest import default_battery_config, default_battery_state

NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)


def obs(soc_start, soc_end, energy_kwh, direction="charge"):
    return CapacityObservation(
        timestamp=NOW, soc_start=soc_start, soc_end=soc_end,
        energy_kwh=energy_kwh, direction=direction,
    )


# ---------------------------------------------------------------------------
# degradation_cost_per_kwh
# ---------------------------------------------------------------------------

def test_degradation_cost_formula():
    config = default_battery_config()
    state = default_battery_state()
    # 5000 / (6000 * 10.0 * 2) = 5000 / 120000 = 0.041667
    expected = 5000.0 / (6000 * 10.0 * 2)
    assert abs(degradation_cost_per_kwh(config, state) - expected) < 1e-10


def test_degradation_scales_with_capacity():
    config = default_battery_config()
    state_small = BatteryState(soc=0.5, estimated_capacity_kwh=5.0)
    state_large = BatteryState(soc=0.5, estimated_capacity_kwh=20.0)
    # Smaller capacity → higher cost per kWh (fewer kWh per cycle)
    assert degradation_cost_per_kwh(config, state_small) > degradation_cost_per_kwh(config, state_large)


# ---------------------------------------------------------------------------
# trade_profit_per_kwh
# ---------------------------------------------------------------------------

def test_trade_profitable_positive_spread():
    # sell=0.20 * 0.9 - buy=0.05 - deg=0.04 * 2 = 0.18 - 0.05 - 0.08 = 0.05
    assert trade_profit_per_kwh(0.05, 0.20, 0.04, 0.9) > 0


def test_trade_not_profitable_small_spread():
    # sell=0.12 * 0.9 - buy=0.10 - deg=0.04 * 2 = 0.108 - 0.10 - 0.08 = -0.072
    assert trade_profit_per_kwh(0.10, 0.12, 0.04, 0.9) <= 0


def test_trade_zero_profit_boundary():
    # solve: sell * eff - buy - deg * 2 = 0
    # sell = (buy + deg * 2) / eff = (0.05 + 0.08) / 0.9 = 0.14444...
    buy = 0.05
    deg = 0.04
    eff = 0.9
    sell = (buy + deg * 2) / eff
    profit = trade_profit_per_kwh(buy, sell, deg, eff)
    assert abs(profit) < 1e-10


def test_trade_profit_formula():
    profit = trade_profit_per_kwh(0.05, 0.20, 0.04, 0.90)
    expected = 0.20 * 0.90 - 0.05 - 0.04 * 2
    assert abs(profit - expected) < 1e-12


def test_trade_efficiency_reduces_profit():
    p_high = trade_profit_per_kwh(0.05, 0.20, 0.04, 0.95)
    p_low = trade_profit_per_kwh(0.05, 0.20, 0.04, 0.80)
    assert p_high > p_low


# ---------------------------------------------------------------------------
# CapacityEstimator
# ---------------------------------------------------------------------------

def test_estimator_returns_nominal_when_no_observations():
    est = CapacityEstimator(nominal_capacity_kwh=10.0)
    assert est.estimated_capacity_kwh == 10.0


def test_estimator_single_observation():
    est = CapacityEstimator(nominal_capacity_kwh=10.0)
    # charge 20%→80%: delta=0.6, energy=5.5kWh → implied=5.5/0.6=9.167
    est.add_observation(obs(0.20, 0.80, 5.5))
    assert abs(est.estimated_capacity_kwh - 5.5 / 0.6) < 0.001


def test_estimator_discards_small_delta():
    est = CapacityEstimator(nominal_capacity_kwh=10.0)
    est.add_observation(obs(0.50, 0.53, 0.3))  # delta=0.03 < 0.05 threshold
    # Nominal should be unchanged since observation was discarded
    assert est.estimated_capacity_kwh == 10.0


def test_estimator_discards_at_exact_threshold():
    est = CapacityEstimator(nominal_capacity_kwh=10.0)
    est.add_observation(obs(0.50, 0.549, 0.49))  # delta=0.049 < 0.05
    assert est.estimated_capacity_kwh == 10.0


def test_estimator_recent_bias():
    est = CapacityEstimator(nominal_capacity_kwh=10.0)
    # Old: implies 8.0 kWh (6.4 / 0.8)
    est.add_observation(obs(0.10, 0.90, 6.4))
    # Recent: implies 9.5 kWh (7.6 / 0.8)
    est.add_observation(obs(0.10, 0.90, 7.6))
    result = est.estimated_capacity_kwh
    # Should be closer to 9.5 than 8.0 (weighted toward recent)
    assert result > 8.75, f"Expected >8.75, got {result}"


def test_estimator_convergence_multiple_identical():
    est = CapacityEstimator(nominal_capacity_kwh=10.0)
    for _ in range(5):
        est.add_observation(obs(0.10, 0.90, 7.2))  # implies 9.0 kWh
    assert abs(est.estimated_capacity_kwh - 9.0) < 0.01


def test_estimator_serialization_roundtrip():
    est = CapacityEstimator(nominal_capacity_kwh=10.0)
    est.add_observation(obs(0.20, 0.80, 5.5))
    est.add_observation(obs(0.10, 0.90, 8.0))
    original = est.estimated_capacity_kwh

    d = est.to_dict()
    restored = CapacityEstimator.from_dict(d)
    assert abs(restored.estimated_capacity_kwh - original) < 1e-10


def test_estimator_from_dict_no_observations():
    d = {"nominal_capacity_kwh": 12.5, "observations": []}
    est = CapacityEstimator.from_dict(d)
    assert est.estimated_capacity_kwh == 12.5
