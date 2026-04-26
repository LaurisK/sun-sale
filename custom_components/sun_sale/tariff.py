"""Tariff formula: convert Nordpool spot prices to effective buy/sell prices.

Pure Python — no Home Assistant imports.
"""
from __future__ import annotations

from .models import HourlyPrice, TariffConfig, TariffResult


def buy_price(spot: float, config: TariffConfig) -> float:
    """Total cost to buy 1 kWh from the grid.

    Formula: (spot + distribution_fee + markup) * (1 + tax_rate)
    """
    return (spot + config.distribution_fee + config.markup) * (1.0 + config.tax_rate)


def sell_price(spot: float, config: TariffConfig) -> float:
    """Total revenue from selling 1 kWh to the grid.

    Formula: (spot - sell_distribution_fee - sell_markup) * (1 - sell_tax_rate)
    """
    return (spot - config.sell_distribution_fee - config.sell_markup) * (1.0 - config.sell_tax_rate)


def compute_tariffs(prices: list[HourlyPrice], config: TariffConfig) -> list[TariffResult]:
    """Convert raw spot prices to effective buy/sell prices for each hour."""
    return [
        TariffResult(
            hour=p.start,
            spot_price=p.price_eur_kwh,
            buy_price=buy_price(p.price_eur_kwh, config),
            sell_price=sell_price(p.price_eur_kwh, config),
        )
        for p in prices
    ]
