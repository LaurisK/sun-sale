"""Tests for pipeline/charging_profile.py — pure Python, no HA required."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.sun_sale.contract.models import (
    BatteryStatus,
    ChargeMode,
    GenerationSeries,
    GenerationSlot,
    PriceSeries,
    PriceSlot,
)
from custom_components.sun_sale.pipeline.charging_profile import (
    build_charging_profile,
)
from tests.conftest import default_battery_config


NOW = datetime(2024, 1, 15, 6, 0, 0, tzinfo=timezone.utc)


def _gen_slot(hour: int, kwh: float) -> GenerationSlot:
    start = NOW.replace(hour=hour, minute=0)
    return GenerationSlot(
        start=start,
        end=start + timedelta(hours=1),
        expected_kwh=kwh,
        source="open_meteo",
        confidence=None,
    )


def _price_slot(hour: int, sell: float, buy: float = 0.20) -> PriceSlot:
    start = NOW.replace(hour=hour, minute=0)
    return PriceSlot(
        start=start,
        end=start + timedelta(hours=1),
        buy_eur_kwh=buy,
        sell_eur_kwh=sell,
        spot_eur_kwh=sell,
        sell_allowed=sell > 0.0,
        sources=("nordpool", "tariff"),
    )


def _gen_series(slots: list[GenerationSlot]) -> GenerationSeries:
    return GenerationSeries(
        slots=tuple(slots),
        primary="open_meteo",
        overlays=(),
        computed_at=NOW,
    )


def _price_series(slots: list[PriceSlot]) -> PriceSeries:
    return PriceSeries(
        slots=tuple(slots),
        resolution=timedelta(hours=1),
        computed_at=NOW,
    )


def _status(soc: float, total: float = 10.0) -> BatteryStatus:
    return BatteryStatus(
        total_capacity_kwh=total,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        soc=soc,
        remaining_capacity_kwh=soc * total,
    )


# ---------------------------------------------------------------------------
# Case 1: generation fits in battery → all generating slots SOLAR_CHARGE
# ---------------------------------------------------------------------------

def test_case1_all_solar_charge_when_generation_fits():
    # max_soc=0.95, soc=0.5, total=10 → free = 4.5 kWh
    # gen total = 3.0 kWh < 4.5 → case 1
    gens = [_gen_slot(8, 1.0), _gen_slot(12, 2.0), _gen_slot(16, 0.0)]
    prices = [_price_slot(8, 0.10), _price_slot(12, 0.15), _price_slot(16, 0.08)]
    profile = build_charging_profile(
        _status(soc=0.5), _gen_series(gens), _price_series(prices),
        default_battery_config(), NOW,
    )
    assert profile.solar_exceeds_capacity is False
    modes = [s.mode for s in profile.slots]
    assert modes == [ChargeMode.SOLAR_CHARGE, ChargeMode.SOLAR_CHARGE, ChargeMode.IDLE]
    assert profile.allocated_solar_kwh == 3.0
    assert profile.total_no_export_kwh == 0.0


def test_case1_free_capacity_uses_max_soc_not_one():
    # max_soc=0.95: even at soc=0 free is 9.5, not 10.0
    gens = [_gen_slot(12, 9.4)]
    prices = [_price_slot(12, 0.10)]
    profile = build_charging_profile(
        _status(soc=0.0), _gen_series(gens), _price_series(prices),
        default_battery_config(), NOW,
    )
    assert profile.free_capacity_kwh == 9.5
    assert profile.solar_exceeds_capacity is False
    assert profile.slots[0].mode == ChargeMode.SOLAR_CHARGE


# ---------------------------------------------------------------------------
# Case 2: generation overflows → pick lowest sell-price slots for battery
# ---------------------------------------------------------------------------

def test_case2_lowest_sell_price_slots_fill_battery():
    # free = (0.95 - 0.85) * 10 = 1.0 kWh
    # gen total = 5.0 → exceeds capacity
    # slots ranked by sell price asc: hour 12 (sell=0.05), then 8 (0.10), then 16 (0.20)
    # cumulative: 12 → 2.0 >= 1.0, stop. Only hour 12 is SOLAR_CHARGE.
    gens = [_gen_slot(8, 1.0), _gen_slot(12, 2.0), _gen_slot(16, 2.0)]
    prices = [_price_slot(8, 0.10), _price_slot(12, 0.05), _price_slot(16, 0.20)]
    profile = build_charging_profile(
        _status(soc=0.85), _gen_series(gens), _price_series(prices),
        default_battery_config(), NOW,
    )
    assert profile.solar_exceeds_capacity is True
    modes_by_hour = {s.start.hour: s.mode for s in profile.slots}
    assert modes_by_hour[12] == ChargeMode.SOLAR_CHARGE
    assert modes_by_hour[8] == ChargeMode.SELL
    assert modes_by_hour[16] == ChargeMode.SELL
    assert profile.allocated_solar_kwh == 2.0  # marginal slot kept whole


def test_case2_marginal_slot_kept_whole_overfills():
    # free = 1.0; cheapest slot has 2.0 kWh → allocated_solar = 2.0 (overshoots, OK)
    gens = [_gen_slot(12, 2.0), _gen_slot(13, 2.0)]
    prices = [_price_slot(12, 0.05), _price_slot(13, 0.10)]
    profile = build_charging_profile(
        _status(soc=0.85), _gen_series(gens), _price_series(prices),
        default_battery_config(), NOW,
    )
    assert profile.allocated_solar_kwh == 2.0
    assert profile.free_capacity_kwh == pytest.approx(1.0)
    modes_by_hour = {s.start.hour: s.mode for s in profile.slots}
    assert modes_by_hour[12] == ChargeMode.SOLAR_CHARGE
    assert modes_by_hour[13] == ChargeMode.SELL


def test_case2_multiple_slots_summed_to_reach_free_capacity():
    # free = (0.95 - 0.5) * 10 = 4.5 kWh
    # gen total = 6.0 → exceeds
    # cheapest order: 8 (sell=0.01), 12 (0.05), 16 (0.20)
    # cumulative: 8 → 2.0 (< 4.5), 12 → 4.0 (< 4.5), 16 → 6.0 (>= 4.5, stop after)
    gens = [_gen_slot(8, 2.0), _gen_slot(12, 2.0), _gen_slot(16, 2.0)]
    prices = [_price_slot(8, 0.01), _price_slot(12, 0.05), _price_slot(16, 0.20)]
    profile = build_charging_profile(
        _status(soc=0.5), _gen_series(gens), _price_series(prices),
        default_battery_config(), NOW,
    )
    modes_by_hour = {s.start.hour: s.mode for s in profile.slots}
    assert modes_by_hour[8] == ChargeMode.SOLAR_CHARGE
    assert modes_by_hour[12] == ChargeMode.SOLAR_CHARGE
    assert modes_by_hour[16] == ChargeMode.SOLAR_CHARGE  # marginal, kept whole
    assert profile.allocated_solar_kwh == 6.0


# ---------------------------------------------------------------------------
# NO_EXPORT: excess solar where sell price <= 0
# ---------------------------------------------------------------------------

def test_no_export_when_sell_price_negative():
    # free = 1.0; cheapest is the negative-price slot but we WANT to charge there
    # because storing avoids paying to export. Allocate negative-price slot first.
    # After it's allocated, remaining positive-price slot → SELL.
    # Add a second negative slot that doesn't fit → NO_EXPORT.
    gens = [_gen_slot(8, 1.0), _gen_slot(12, 2.0), _gen_slot(16, 2.0)]
    prices = [
        _price_slot(8, 0.10),    # positive
        _price_slot(12, -0.02),  # negative → preferred for battery
        _price_slot(16, -0.05),  # most negative → preferred, but capacity full
    ]
    profile = build_charging_profile(
        _status(soc=0.85), _gen_series(gens), _price_series(prices),
        default_battery_config(), NOW,
    )
    modes_by_hour = {s.start.hour: s.mode for s in profile.slots}
    # hour 16 (most negative) gets battery; capacity then exceeded
    assert modes_by_hour[16] == ChargeMode.SOLAR_CHARGE
    # hour 12 next-cheapest, but free_capacity already met → NO_EXPORT (still negative)
    assert modes_by_hour[12] == ChargeMode.NO_EXPORT
    # hour 8 positive → SELL
    assert modes_by_hour[8] == ChargeMode.SELL
    assert profile.total_no_export_kwh == 2.0


def test_no_export_when_battery_full_and_sell_negative():
    # free = 0 (soc == max_soc=0.95): nothing allocated; negative slot → NO_EXPORT
    gens = [_gen_slot(12, 1.0), _gen_slot(13, 1.0)]
    prices = [_price_slot(12, -0.01), _price_slot(13, 0.10)]
    profile = build_charging_profile(
        _status(soc=0.95), _gen_series(gens), _price_series(prices),
        default_battery_config(), NOW,
    )
    assert profile.free_capacity_kwh == 0.0
    assert profile.solar_exceeds_capacity is True
    modes_by_hour = {s.start.hour: s.mode for s in profile.slots}
    assert modes_by_hour[12] == ChargeMode.NO_EXPORT
    assert modes_by_hour[13] == ChargeMode.SELL
    assert profile.total_no_export_kwh == 1.0
    assert profile.allocated_solar_kwh == 0.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_zero_generation_all_idle():
    gens = [_gen_slot(8, 0.0), _gen_slot(12, 0.0)]
    prices = [_price_slot(8, 0.10), _price_slot(12, 0.10)]
    profile = build_charging_profile(
        _status(soc=0.5), _gen_series(gens), _price_series(prices),
        default_battery_config(), NOW,
    )
    assert all(s.mode == ChargeMode.IDLE for s in profile.slots)
    assert profile.allocated_solar_kwh == 0.0
    assert profile.solar_exceeds_capacity is False


def test_past_slots_excluded():
    # NOW = 06:00. Slot at 05:00 must be excluded; slot at 12:00 included.
    past = _gen_slot(5, 1.0)
    future = _gen_slot(12, 1.0)
    prices = [_price_slot(5, 0.10), _price_slot(12, 0.10)]
    profile = build_charging_profile(
        _status(soc=0.5), _gen_series([past, future]), _price_series(prices),
        default_battery_config(), NOW,
    )
    assert len(profile.slots) == 1
    assert profile.slots[0].start.hour == 12


def test_tomorrow_slots_excluded():
    tomorrow_start = NOW.replace(hour=12) + timedelta(days=1)
    tomorrow_slot = GenerationSlot(
        start=tomorrow_start,
        end=tomorrow_start + timedelta(hours=1),
        expected_kwh=5.0,
        source="open_meteo",
        confidence=None,
    )
    today_slot = _gen_slot(12, 1.0)
    tomorrow_price = PriceSlot(
        start=tomorrow_start,
        end=tomorrow_start + timedelta(hours=1),
        buy_eur_kwh=0.2,
        sell_eur_kwh=0.1,
        spot_eur_kwh=0.1,
        sell_allowed=True,
        sources=("nordpool", "tariff"),
    )
    profile = build_charging_profile(
        _status(soc=0.5),
        _gen_series([today_slot, tomorrow_slot]),
        _price_series([_price_slot(12, 0.10), tomorrow_price]),
        default_battery_config(),
        NOW,
    )
    assert len(profile.slots) == 1
    assert profile.slots[0].start == today_slot.start


def test_today_remaining_generation_kwh_matches_sum():
    gens = [_gen_slot(8, 1.5), _gen_slot(12, 2.0), _gen_slot(16, 0.5)]
    prices = [_price_slot(8, 0.10), _price_slot(12, 0.10), _price_slot(16, 0.10)]
    profile = build_charging_profile(
        _status(soc=0.5), _gen_series(gens), _price_series(prices),
        default_battery_config(), NOW,
    )
    assert profile.today_remaining_generation_kwh == 4.0


def test_profile_is_immutable():
    import dataclasses
    gens = [_gen_slot(12, 1.0)]
    prices = [_price_slot(12, 0.10)]
    profile = build_charging_profile(
        _status(soc=0.5), _gen_series(gens), _price_series(prices),
        default_battery_config(), NOW,
    )
    try:
        profile.slots[0].expected_kwh = 99.0  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("ChargingProfileSlot should be frozen")


# ---------------------------------------------------------------------------
# Node wiring: ChargingProfileNode within the DAG engine
# ---------------------------------------------------------------------------

def test_charging_profile_node_produces_profile_from_inputs():
    import asyncio
    from custom_components.sun_sale.contract.models import (
        ChargingProfile,
        SunSaleConfig,
    )
    from custom_components.sun_sale.pipeline.dag_engine import NodeContext
    from custom_components.sun_sale.pipeline.nodes import ChargingProfileNode

    status = _status(soc=0.5)
    generation = _gen_series([_gen_slot(12, 2.0)])
    prices = _price_series([_price_slot(12, 0.10)])
    config = SunSaleConfig(tariff=None, battery=default_battery_config())
    ctx = NodeContext(
        primary={},
        secondary={
            BatteryStatus: status,
            GenerationSeries: generation,
            PriceSeries: prices,
        },
        config=config,
        now=NOW,
    )
    node = ChargingProfileNode()
    asyncio.run(node.run(ctx))
    profile = ctx.secondary[ChargingProfile]
    assert isinstance(profile, ChargingProfile)
    assert profile.solar_exceeds_capacity is False
    assert profile.slots[0].mode == ChargeMode.SOLAR_CHARGE
