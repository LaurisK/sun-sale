"""Pricing stage: normalise Nordpool spot prices into PriceSeries.

Pure Python — no Home Assistant imports.

Owns the 72h yesterday→today→tomorrow assembly. Downstream consumers of
PriceSeries can rely on it covering the full window when yesterday data is
available from the persistent store.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..pipeline import tariff as tariff_module
from ..contract.models import (
    NordpoolData,
    PriceEntry,
    PriceSeries,
    PriceSlot,
    TariffConfig,
    YesterdayPrices,
)


def build_price_series(
    prices: list[PriceEntry],
    config: TariffConfig,
    now: datetime | None = None,
    resolution: timedelta | None = None,
) -> PriceSeries:
    """Apply tariff formulas to a list of Nordpool entries → PriceSeries.

    If `resolution` is provided it is recorded verbatim; otherwise it is
    derived from the first two slots (or defaults to 1h for single-slot input).
    """
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

    if resolution is None:
        resolution = (slots[1].start - slots[0].start) if len(slots) >= 2 else timedelta(hours=1)

    return PriceSeries(
        slots=tuple(slots),
        resolution=resolution,
        computed_at=now,
    )


def build_price_series_72h(
    nordpool: NordpoolData,
    yesterday: YesterdayPrices,
    config: TariffConfig,
    now: datetime | None = None,
) -> PriceSeries:
    """Assemble the full yesterday→today→tomorrow PriceSeries.

    Combines persisted yesterday entries with today+tomorrow from the Nordpool
    translator, then applies the tariff. Resolution is taken from
    `nordpool.resolution` so the inbound translator stays the single source of
    truth for slot granularity.
    """
    combined = list(yesterday.entries) + list(nordpool.entries)
    return build_price_series(combined, config, now=now, resolution=nordpool.resolution)
