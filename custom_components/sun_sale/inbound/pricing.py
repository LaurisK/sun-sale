"""Pricing stage: Nordpool HA-state reader + 72h PriceSeries assembly.

The `NordpoolTranslator` reads the Nordpool sensor and produces NordpoolData
(today + tomorrow, with tomorrow zero-filled until it's published). The
`build_price_series*` functions then apply tariff formulas and stitch in
persisted yesterday entries to produce the full 72h PriceSeries.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..pipeline import tariff as tariff_module
from ..contract.models import (
    NordpoolData,
    PriceEntry,
    PriceSeries,
    PriceSlot,
    SunSaleConfig,
    TariffConfig,
    YesterdayPrices,
)

_LOGGER = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Nordpool translator (HA-edge reader)
# ---------------------------------------------------------------------------

def _zero_fill_tomorrow(
    entries: list[PriceEntry], resolution: timedelta, now: datetime
) -> list[PriceEntry]:
    """Append zero-price entries for tomorrow if tomorrow has no entries yet."""
    tomorrow_date = (now + timedelta(days=1)).date()
    has_tomorrow = any(e.start.date() == tomorrow_date for e in entries)
    if has_tomorrow:
        return entries

    tomorrow_start = datetime(
        tomorrow_date.year, tomorrow_date.month, tomorrow_date.day,
        0, 0, 0, tzinfo=timezone.utc,
    )
    slots_per_day = 96 if resolution <= timedelta(minutes=15) else 24
    zero_entries = [
        PriceEntry(
            start=tomorrow_start + i * resolution,
            end=tomorrow_start + (i + 1) * resolution,
            price_eur_kwh=0.0,
        )
        for i in range(slots_per_day)
    ]
    return entries + zero_entries


class NordpoolTranslator:
    """Reads Nordpool sensor; produces NordpoolData for today + tomorrow.

    Resolution is auto-detected from the sensor data (15min or 1h).
    Tomorrow entries are zero-filled when not yet published.
    Coordinator prepends yesterday entries from persistent store.
    """

    output_type = NordpoolData

    def __init__(self, entity_id: str) -> None:
        self._entity_id = entity_id

    def parse(self, hass: Any, now: datetime | None = None) -> NordpoolData:
        """Parse Nordpool state into NordpoolData (today + tomorrow). Sync; callable from tests."""
        if now is None:
            now = datetime.now(timezone.utc)

        state = hass.states.get(self._entity_id)
        if state is None:
            _LOGGER.warning("Nordpool entity '%s' not found", self._entity_id)
            return NordpoolData(entries=[], resolution=timedelta(hours=1))

        raw_entries: list[dict] = []
        for attr_key in ("raw_today", "raw_tomorrow"):
            raw = state.attributes.get(attr_key)
            if isinstance(raw, list):
                raw_entries.extend(raw)

        if raw_entries:
            return self._parse_raw_entries(raw_entries, now)

        return self._parse_legacy(state, now)

    def _parse_raw_entries(self, raw_entries: list[dict], now: datetime) -> NordpoolData:
        parsed: list[tuple[datetime, float]] = []
        for entry in raw_entries:
            try:
                sv = entry["start"]
                dt = sv if isinstance(sv, datetime) else datetime.fromisoformat(str(sv))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                start_utc = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
                parsed.append((start_utc, float(entry["value"])))
            except (KeyError, ValueError, TypeError):
                continue

        seen: set[datetime] = set()
        unique: list[tuple[datetime, float]] = []
        for item in sorted(parsed):
            if item[0] not in seen:
                seen.add(item[0])
                unique.append(item)

        if not unique:
            return NordpoolData(entries=[], resolution=timedelta(hours=1))

        resolution = (unique[1][0] - unique[0][0]) if len(unique) >= 2 else timedelta(hours=1)
        entries = [PriceEntry(start=s, end=s + resolution, price_eur_kwh=p) for s, p in unique]
        entries = _zero_fill_tomorrow(entries, resolution, now)
        return NordpoolData(entries=entries, resolution=resolution)

    def _parse_legacy(self, state: Any, now: datetime) -> NordpoolData:
        """Legacy format: flat list of up to 24 hourly prices per day."""
        resolution = timedelta(hours=1)
        entries: list[PriceEntry] = []
        for offset, attr_key in enumerate(("today", "tomorrow")):
            raw = state.attributes.get(attr_key)
            if not isinstance(raw, list):
                continue
            base_date = (now + timedelta(days=offset)).date()
            for hour_idx, price in enumerate(raw):
                if price is None or hour_idx >= 24:
                    continue
                start = datetime(
                    base_date.year, base_date.month, base_date.day,
                    hour_idx, 0, 0, tzinfo=timezone.utc,
                )
                entries.append(PriceEntry(start=start, end=start + resolution, price_eur_kwh=float(price)))

        if not entries:
            return NordpoolData(entries=[], resolution=resolution)
        entries = _zero_fill_tomorrow(entries, resolution, now)
        return NordpoolData(entries=entries, resolution=resolution)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> NordpoolData:
        return self.parse(hass, now)
