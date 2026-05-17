"""Tests for pricing.py — pure Python, no HA required."""
from datetime import datetime, timedelta, timezone

from custom_components.sun_sale.contract.models import (
    NordpoolData,
    TariffConfig,
    YesterdayPrices,
)
from custom_components.sun_sale.inbound.pricing import (
    build_price_series,
    build_price_series_72h,
)
from tests.conftest import BASE_DT, default_tariff_config, make_price

NOW = BASE_DT


# ---------------------------------------------------------------------------
# Basic round-trip
# ---------------------------------------------------------------------------

def test_empty_prices_returns_empty_series():
    ps = build_price_series([], default_tariff_config(), now=NOW)
    assert ps.slots == ()


def test_slot_count_matches_input():
    prices = [make_price(h, 0.10) for h in range(24)]
    ps = build_price_series(prices, default_tariff_config(), now=NOW)
    assert len(ps.slots) == 24


def test_computed_at_is_set():
    ps = build_price_series([make_price(0, 0.10)], default_tariff_config(), now=NOW)
    assert ps.computed_at == NOW


def test_sources_tuple():
    ps = build_price_series([make_price(0, 0.10)], default_tariff_config(), now=NOW)
    assert ps.slots[0].sources == ("nordpool", "tariff")


# ---------------------------------------------------------------------------
# Tariff math round-trips
# ---------------------------------------------------------------------------

def test_buy_price_formula():
    tc = TariffConfig(distribution_fee=0.03, tax_rate=0.21, markup=0.01,
                      sell_distribution_fee=0.0, sell_tax_rate=0.0, sell_markup=0.0)
    ps = build_price_series([make_price(0, 0.10)], tc, now=NOW)
    slot = ps.slots[0]
    expected_buy = (0.10 + 0.03 + 0.01) * (1.0 + 0.21)
    assert abs(slot.buy_eur_kwh - expected_buy) < 1e-9


def test_sell_price_formula():
    tc = TariffConfig(distribution_fee=0.0, tax_rate=0.0, markup=0.0,
                      sell_distribution_fee=0.02, sell_tax_rate=0.05, sell_markup=0.005)
    ps = build_price_series([make_price(0, 0.10)], tc, now=NOW)
    slot = ps.slots[0]
    expected_sell = (0.10 - 0.02 - 0.005) * (1.0 - 0.05)
    assert abs(slot.sell_eur_kwh - expected_sell) < 1e-9


def test_spot_price_preserved():
    ps = build_price_series([make_price(0, 0.12345)], default_tariff_config(), now=NOW)
    assert abs(ps.slots[0].spot_eur_kwh - 0.12345) < 1e-9


# ---------------------------------------------------------------------------
# Negative-spot → negative-sell
# ---------------------------------------------------------------------------

def test_negative_spot_produces_negative_sell():
    ps = build_price_series([make_price(0, -0.05)], default_tariff_config(), now=NOW)
    assert ps.slots[0].sell_eur_kwh < 0


def test_positive_spot_produces_positive_sell():
    ps = build_price_series([make_price(0, 0.10)], default_tariff_config(), now=NOW)
    assert ps.slots[0].sell_eur_kwh > 0


def test_sell_price_can_be_exactly_zero():
    tc = TariffConfig(distribution_fee=0.0, tax_rate=0.0, markup=0.0,
                      sell_distribution_fee=0.10, sell_tax_rate=0.0, sell_markup=0.0)
    ps = build_price_series([make_price(0, 0.10)], tc, now=NOW)
    # sell = (0.10 - 0.10) * 1 = 0.0 exactly
    assert ps.slots[0].sell_eur_kwh == 0.0


# ---------------------------------------------------------------------------
# Resolution detection
# ---------------------------------------------------------------------------

def test_hourly_resolution_detected():
    prices = [make_price(h, 0.10) for h in range(4)]
    ps = build_price_series(prices, default_tariff_config(), now=NOW)
    assert ps.resolution == timedelta(hours=1)


def test_single_slot_defaults_to_hourly_resolution():
    ps = build_price_series([make_price(0, 0.10)], default_tariff_config(), now=NOW)
    assert ps.resolution == timedelta(hours=1)


# ---------------------------------------------------------------------------
# slot_at / window helpers
# ---------------------------------------------------------------------------

def test_slot_at_returns_correct_slot():
    prices = [make_price(h, float(h) * 0.01) for h in range(24)]
    ps = build_price_series(prices, default_tariff_config(), now=NOW)
    t = NOW.replace(hour=5, minute=30)
    slot = ps.slot_at(t)
    assert slot is not None
    assert slot.start.hour == 5


def test_slot_at_returns_none_outside_range():
    ps = build_price_series([make_price(0, 0.10)], default_tariff_config(), now=NOW)
    t = NOW.replace(hour=5)
    assert ps.slot_at(t) is None


def test_window_returns_overlapping_slots():
    prices = [make_price(h, 0.10) for h in range(24)]
    ps = build_price_series(prices, default_tariff_config(), now=NOW)
    t1 = NOW.replace(hour=2)
    t2 = NOW.replace(hour=5)
    window = ps.window(t1, t2)
    assert len(window) == 3
    assert window[0].start.hour == 2
    assert window[-1].start.hour == 4


# ---------------------------------------------------------------------------
# 72h yesterday→today→tomorrow assembly
# ---------------------------------------------------------------------------

def _entry(day_offset: int, hour: int, price: float):
    start = (BASE_DT + timedelta(days=day_offset)).replace(hour=hour)
    from custom_components.sun_sale.contract.models import PriceEntry
    return PriceEntry(start=start, end=start + timedelta(hours=1), price_eur_kwh=price)


def test_72h_combines_yesterday_today_tomorrow():
    yesterday = tuple(_entry(-1, h, 0.05) for h in range(24))
    today_tomorrow = [_entry(0, h, 0.10) for h in range(24)] + [_entry(1, h, 0.15) for h in range(24)]
    nordpool = NordpoolData(entries=today_tomorrow, resolution=timedelta(hours=1))
    ps = build_price_series_72h(
        nordpool, YesterdayPrices(entries=yesterday), default_tariff_config(), now=NOW
    )
    assert len(ps.slots) == 72
    assert ps.slots[0].start == BASE_DT - timedelta(days=1)
    assert ps.slots[23].start.hour == 23
    assert ps.slots[24].start == BASE_DT
    assert ps.slots[-1].start == BASE_DT + timedelta(days=1, hours=23)


def test_72h_uses_nordpool_resolution_not_derived():
    # Sparse entries (a single today slot) — derivation would default to 1h.
    # Explicit 15-min resolution from NordpoolData must be preserved.
    today = [_entry(0, 0, 0.10)]
    nordpool = NordpoolData(entries=today, resolution=timedelta(minutes=15))
    ps = build_price_series_72h(
        nordpool, YesterdayPrices(entries=()), default_tariff_config(), now=NOW
    )
    assert ps.resolution == timedelta(minutes=15)


def test_72h_empty_yesterday_returns_only_today_tomorrow():
    today_tomorrow = [_entry(0, h, 0.10) for h in range(24)] + [_entry(1, h, 0.15) for h in range(24)]
    nordpool = NordpoolData(entries=today_tomorrow, resolution=timedelta(hours=1))
    ps = build_price_series_72h(
        nordpool, YesterdayPrices(entries=()), default_tariff_config(), now=NOW
    )
    assert len(ps.slots) == 48
    assert ps.slots[0].start == BASE_DT


def test_72h_applies_tariff_to_all_segments():
    tc = TariffConfig(distribution_fee=0.03, tax_rate=0.21, markup=0.01,
                      sell_distribution_fee=0.0, sell_tax_rate=0.0, sell_markup=0.0)
    yesterday = (_entry(-1, 0, 0.10),)
    today_tomorrow = [_entry(0, 0, 0.10)]
    nordpool = NordpoolData(entries=today_tomorrow, resolution=timedelta(hours=1))
    ps = build_price_series_72h(nordpool, YesterdayPrices(entries=yesterday), tc, now=NOW)
    expected_buy = (0.10 + 0.03 + 0.01) * 1.21
    assert abs(ps.slots[0].buy_eur_kwh - expected_buy) < 1e-9
    assert abs(ps.slots[1].buy_eur_kwh - expected_buy) < 1e-9
