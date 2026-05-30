"""Tests for pipeline/monthly_bill.py — pure Python."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:    # pragma: no cover
    from backports.zoneinfo import ZoneInfo    # type: ignore[no-redef]

from custom_components.sun_sale.contract.models import (
    GridExportTodayHistory,
    GridImportTodayHistory,
    GridPowerHistory,
    GridPowerReading,
    MonthlyBillState,
    ObservedGridSeries,
    ObservedGridSlot,
    PriceSeries,
    PriceSlot,
)
from custom_components.sun_sale.inbound.grid import build_observed_grid_series
from custom_components.sun_sale.pipeline.monthly_bill import (
    build_monthly_bill_result,
    compute_bill_slots,
)


LOCAL_TZ = ZoneInfo("Europe/Vilnius")


def _slot(start_utc: datetime, end_utc: datetime, buy: float = 0.20, sell: float = 0.15) -> PriceSlot:
    return PriceSlot(
        start=start_utc,
        end=end_utc,
        buy_eur_kwh=buy,
        sell_eur_kwh=sell,
        spot_eur_kwh=buy,
        sources=("nordpool",),
    )


def _price_series(slots: list[PriceSlot]) -> PriceSeries:
    return PriceSeries(
        slots=tuple(slots),
        resolution=timedelta(minutes=15),
        computed_at=slots[0].start if slots else datetime(2026, 5, 30, tzinfo=timezone.utc),
    )


def _samples_every_minute(start: datetime, count: int, power_kw: float) -> list[GridPowerReading]:
    return [
        GridPowerReading(power_kw=power_kw, timestamp=start + timedelta(minutes=i))
        for i in range(count)
    ]


_NO_IMPORT = GridImportTodayHistory(samples=())
_NO_EXPORT = GridExportTodayHistory(samples=())


def _grid_series_from_samples(
    samples: list[GridPowerReading], price_series: PriceSeries, now: datetime,
) -> ObservedGridSeries:
    """Wrap raw grid-power samples into an ObservedGridSeries via the real builder.

    Tests use this so they exercise the same path the DAG uses in production.
    `now` must be on or after the last sample.
    """
    return build_observed_grid_series(
        GridPowerHistory(samples=tuple(samples)),
        _NO_IMPORT, _NO_EXPORT,
        price_series.slots,
        now=now,
        local_tz=LOCAL_TZ,
    )


def _empty_grid_series(now: datetime) -> ObservedGridSeries:
    """Return an empty ObservedGridSeries usable for no-samples tests."""
    return ObservedGridSeries(slots=(), computed_at=now)


# ---------------------------------------------------------------------------
# compute_bill_slots
# ---------------------------------------------------------------------------


def test_compute_bill_slots_emits_dense_zero_slots_when_no_samples():
    """Every overlapping price slot must appear, even with no samples."""
    t0 = datetime(2026, 5, 29, 0, 0, tzinfo=timezone.utc)
    slots = [_slot(t0 + timedelta(minutes=15 * i), t0 + timedelta(minutes=15 * (i + 1))) for i in range(4)]
    ps = _price_series(slots)
    grid = _empty_grid_series(t0)

    out = compute_bill_slots(grid, ps, t0, t0 + timedelta(hours=1))

    assert len(out) == 4
    assert all(s.imported_kwh == 0.0 and s.exported_kwh == 0.0 for s in out)
    assert all(s.net_cost_eur == 0.0 for s in out)


def test_compute_bill_slots_imports_priced_with_buy():
    t0 = datetime(2026, 5, 29, 0, 0, tzinfo=timezone.utc)
    slot = _slot(t0, t0 + timedelta(minutes=15), buy=0.25, sell=0.10)
    ps = _price_series([slot])
    grid = ObservedGridSeries(
        slots=(ObservedGridSlot(
            start=t0, end=t0 + timedelta(minutes=15),
            imported_kwh=1.0, exported_kwh=0.0, source="inverter",
        ),),
        computed_at=t0 + timedelta(minutes=15),
    )

    out = compute_bill_slots(grid, ps, t0, t0 + timedelta(minutes=15))

    assert len(out) == 1
    assert out[0].imported_kwh == 1.0    # 4 kW * 0.25h
    assert out[0].exported_kwh == 0.0
    assert out[0].net_cost_eur == 0.25


def test_compute_bill_slots_exports_priced_with_sell_even_when_negative():
    t0 = datetime(2026, 5, 29, 0, 0, tzinfo=timezone.utc)
    slot = _slot(t0, t0 + timedelta(minutes=15), buy=0.25, sell=-0.05)
    ps = _price_series([slot])
    grid = ObservedGridSeries(
        slots=(ObservedGridSlot(
            start=t0, end=t0 + timedelta(minutes=15),
            imported_kwh=0.0, exported_kwh=1.0, source="inverter",
        ),),
        computed_at=t0 + timedelta(minutes=15),
    )

    out = compute_bill_slots(grid, ps, t0, t0 + timedelta(minutes=15))

    assert out[0].imported_kwh == 0.0
    assert out[0].exported_kwh == 1.0
    assert out[0].net_cost_eur == 0.05    # exporting on a negative sell costs us


# ---------------------------------------------------------------------------
# build_monthly_bill_result
# ---------------------------------------------------------------------------


def _build_pricing_for_window(start_utc: datetime, end_utc: datetime, buy: float = 0.20, sell: float = 0.15) -> PriceSeries:
    slots = []
    cursor = start_utc
    while cursor < end_utc:
        slots.append(_slot(cursor, cursor + timedelta(minutes=15), buy=buy, sell=sell))
        cursor += timedelta(minutes=15)
    return _price_series(slots)


def test_first_run_with_empty_state_zeroes_carry_and_previous_month():
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    ps = _build_pricing_for_window(
        datetime(2026, 5, 28, 0, 0, tzinfo=timezone.utc), now,
    )

    result = build_monthly_bill_result(
        grid_series=_empty_grid_series(now),
        price_series=ps,
        stored_state=None,
        local_tz=LOCAL_TZ,
        now=now,
    )

    assert result.carry_eur == 0.0
    assert result.previous_month_eur == 0.0
    assert result.previous_month_str == ""
    assert result.month_str == "2026-05"
    assert result.total_month_eur == 0.0
    # Slots cover the live window from yday-start LOCAL to now.
    assert len(result.slots) > 0


def test_day_rollover_folds_yday_into_carry():
    """When stored.yday_str != current yday, the just-finished day is baked into carry."""
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    # Yesterday (LOCAL) is 2026-05-29; the previous stored yday is 2026-05-28.
    stored = MonthlyBillState(
        month_str="2026-05",
        carry_eur=3.0,
        yday_str="2026-05-28",
    )
    # Bake-in window in UTC: 2026-05-28 00:00 LOCAL → 2026-05-29 00:00 LOCAL
    # = 2026-05-27 21:00 UTC → 2026-05-28 21:00 UTC
    bake_start = datetime(2026, 5, 27, 21, 0, tzinfo=timezone.utc)
    bake_end = datetime(2026, 5, 28, 21, 0, tzinfo=timezone.utc)
    ps = _build_pricing_for_window(bake_start, now, buy=0.10, sell=0.05)
    # 24h * 1 kW import = 24 kWh @ 0.10 = 2.40 EUR for the bake window.
    samples = _samples_every_minute(bake_start, count=24 * 60, power_kw=1.0)
    grid = _grid_series_from_samples(samples, ps, now)

    result = build_monthly_bill_result(
        grid_series=grid,
        price_series=ps,
        stored_state=stored,
        local_tz=LOCAL_TZ,
        now=now,
    )

    assert result.carry_eur == 3.0 + 2.4
    assert result.updated_state.yday_str == "2026-05-29"
    assert result.updated_state.carry_eur == result.carry_eur


def test_month_rollover_finalises_previous_month():
    """Crossing into a new month: previous month total = carry + bridge from old_yday to month_start."""
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    # Stored: last cycle ran on 2026-05-31 (month_str=2026-05, yday=2026-05-30)
    stored = MonthlyBillState(
        month_str="2026-05",
        carry_eur=10.0,
        yday_str="2026-05-30",
    )
    # Bridge: 2026-05-30 00:00 LOCAL → 2026-06-01 00:00 LOCAL (new month start)
    bridge_start = datetime(2026, 5, 29, 21, 0, tzinfo=timezone.utc)
    bridge_end = datetime(2026, 5, 31, 21, 0, tzinfo=timezone.utc)
    ps = _build_pricing_for_window(bridge_start, now, buy=0.20, sell=0.10)
    samples = _samples_every_minute(bridge_start, count=48 * 60, power_kw=0.5)
    grid = _grid_series_from_samples(samples, ps, now)

    result = build_monthly_bill_result(
        grid_series=grid,
        price_series=ps,
        stored_state=stored,
        local_tz=LOCAL_TZ,
        now=now,
    )

    # 48h * 0.5 kW = 24 kWh @ 0.20 = 4.80 EUR; carry was 10.0
    assert result.previous_month_str == "2026-05"
    assert result.previous_month_eur == 10.0 + 4.8
    assert result.carry_eur == 0.0
    assert result.month_str == "2026-06"


def test_same_day_leaves_carry_unchanged():
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    stored = MonthlyBillState(
        month_str="2026-05",
        carry_eur=7.5,
        yday_str="2026-05-29",
        previous_month_str="2026-04",
        previous_month_eur=42.0,
    )
    ps = _build_pricing_for_window(
        datetime(2026, 5, 28, 21, 0, tzinfo=timezone.utc), now,
    )

    result = build_monthly_bill_result(
        grid_series=_empty_grid_series(now),
        price_series=ps,
        stored_state=stored,
        local_tz=LOCAL_TZ,
        now=now,
    )

    assert result.carry_eur == 7.5
    assert result.previous_month_eur == 42.0
    assert result.previous_month_str == "2026-04"


def test_live_slots_never_cross_into_previous_month():
    """On day 1 of a new month, live slots cover only the new month."""
    now = datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc)
    ps = _build_pricing_for_window(
        datetime(2026, 5, 29, 21, 0, tzinfo=timezone.utc), now,
    )
    stored = MonthlyBillState(
        month_str="2026-06",
        carry_eur=0.0,
        yday_str="2026-05-31",
    )

    result = build_monthly_bill_result(
        grid_series=_empty_grid_series(now),
        price_series=ps,
        stored_state=stored,
        local_tz=LOCAL_TZ,
        now=now,
    )

    month_start_utc = datetime(2026, 5, 31, 21, 0, tzinfo=timezone.utc)
    assert all(s.start >= month_start_utc for s in result.slots)
