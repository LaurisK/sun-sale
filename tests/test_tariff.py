"""Tests for tariff.py — pure Python, no HA required."""
from custom_components.sun_sale.tariff import buy_price, sell_price, compute_tariffs
from tests.conftest import make_price, default_tariff_config


def test_buy_price_basic():
    config = default_tariff_config()
    # (0.05 + 0.03 + 0.01) * 1.21 = 0.09 * 1.21 = 0.1089
    result = buy_price(0.05, config)
    assert abs(result - 0.1089) < 1e-9


def test_sell_price_basic():
    config = default_tariff_config()
    # (0.15 - 0.02 - 0.005) * (1 - 0.0) = 0.125
    result = sell_price(0.15, config)
    assert abs(result - 0.125) < 1e-9


def test_buy_price_zero_spot():
    config = default_tariff_config()
    # (0 + 0.03 + 0.01) * 1.21 = 0.04 * 1.21 = 0.0484
    result = buy_price(0.0, config)
    assert abs(result - 0.0484) < 1e-9


def test_buy_price_negative_spot():
    config = default_tariff_config()
    # Nordpool can go negative; formula should still work
    result = buy_price(-0.05, config)
    # (-0.05 + 0.03 + 0.01) * 1.21 = -0.01 * 1.21 = -0.0121
    assert abs(result - (-0.0121)) < 1e-9


def test_sell_price_negative_spot():
    config = default_tariff_config()
    result = sell_price(-0.10, config)
    # (-0.10 - 0.02 - 0.005) = -0.125
    assert result < 0


def test_buy_always_greater_than_sell_for_same_spot():
    config = default_tariff_config()
    for spot in [-0.05, 0.0, 0.05, 0.10, 0.20, 0.50]:
        assert buy_price(spot, config) > sell_price(spot, config), (
            f"buy should exceed sell at spot={spot}"
        )


def test_compute_tariffs_length():
    prices = [make_price(h, 0.10) for h in range(24)]
    results = compute_tariffs(prices, default_tariff_config())
    assert len(results) == 24


def test_compute_tariffs_preserves_spot():
    prices = [make_price(0, 0.15)]
    results = compute_tariffs(prices, default_tariff_config())
    assert results[0].spot_price == 0.15


def test_compute_tariffs_buy_sell_populated():
    prices = [make_price(0, 0.10)]
    config = default_tariff_config()
    results = compute_tariffs(prices, config)
    assert results[0].buy_price == buy_price(0.10, config)
    assert results[0].sell_price == sell_price(0.10, config)


def test_compute_tariffs_empty():
    results = compute_tariffs([], default_tariff_config())
    assert results == []
