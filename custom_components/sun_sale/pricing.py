"""Pricing stage: normalise Nordpool spot prices into PriceSeries.

Pure Python — no Home Assistant imports.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import tariff as tariff_module
from .models import HourlyPrice, PriceSlot, PriceSeries, TariffConfig


def build_price_series(
    prices: list[HourlyPrice],
    config: TariffConfig,
    now: datetime | None = None,
) -> PriceSeries:
    """Convert raw Nordpool hourly prices + tariff config into a PriceSeries."""
    if now is None:
        now = datetime.now(timezone.utc)

    slots: list[PriceSlot] = []
    for p in prices:
        buy = tariff_module.buy_price(p.price_eur_kwh, config)
        sell = tariff_module.sell_price(p.price_eur_kwh, config)
        slots.append(PriceSlot(
            start=p.start,
            end=p.end,
            buy_eur_kwh=buy,
            sell_eur_kwh=sell,
            spot_eur_kwh=p.price_eur_kwh,
            sell_allowed=sell > 0.0,
            sources=("nordpool", "tariff"),
        ))

    resolution = (slots[1].start - slots[0].start) if len(slots) >= 2 else timedelta(hours=1)

    return PriceSeries(
        slots=tuple(slots),
        resolution=resolution,
        computed_at=now,
    )
