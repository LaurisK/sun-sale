"""Tests for inbound/grid.py — ObservedGridSeries assembly."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.sun_sale.contract.models import (
    GridExportTodayHistory,
    GridExportTodayReading,
    GridImportTodayHistory,
    GridImportTodayReading,
    GridPowerHistory,
    GridPowerReading,
    PriceEntry,
    PriceSeries,
)
from custom_components.sun_sale.inbound.grid import build_observed_grid_series
from custom_components.sun_sale.inbound.pricing import build_price_series
from tests.conftest import BASE_DT, default_tariff_config

NOW = BASE_DT
TODAY = NOW.date()
YESTERDAY = TODAY - timedelta(days=1)

_NO_IMPORT = GridImportTodayHistory(samples=())
_NO_EXPORT = GridExportTodayHistory(samples=())


def _hourly_72h_price_series() -> PriceSeries:
    """72h hourly price grid covering yesterday 00:00 → tomorrow 23:59 (UTC)."""
    base = NOW - timedelta(days=1)
    entries = [
        PriceEntry(
            start=base + timedelta(hours=h),
            end=base + timedelta(hours=h + 1),
            price_eur_kwh=0.10,
        )
        for h in range(72)
    ]
    return build_price_series(entries, default_tariff_config(), now=NOW)


def _power(t: datetime, kw: float) -> GridPowerReading:
    """Helper to build a grid power sample."""
    return GridPowerReading(power_kw=kw, timestamp=t)


def _imp(t: datetime, kwh: float) -> GridImportTodayReading:
    return GridImportTodayReading(today_total_kwh=kwh, timestamp=t)


def _exp(t: datetime, kwh: float) -> GridExportTodayReading:
    return GridExportTodayReading(today_total_kwh=kwh, timestamp=t)


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------

def test_empty_history_yields_empty_series():
    series = build_observed_grid_series(
        GridPowerHistory(samples=()),
        _NO_IMPORT, _NO_EXPORT,
        _hourly_72h_price_series().slots,
        now=NOW,
    )
    assert series.slots == ()


def test_empty_price_grid_yields_empty_series():
    history = GridPowerHistory(samples=(_power(NOW, 1.0),))
    series = build_observed_grid_series(
        history, _NO_IMPORT, _NO_EXPORT, (), now=NOW,
    )
    assert series.slots == ()


# ---------------------------------------------------------------------------
# Per-slot averaging — import side
# ---------------------------------------------------------------------------

def test_pure_import_slot_averages_to_imported_kwh():
    """A slot with steady 2 kW import for one hour → 2 kWh imported."""
    now = NOW.replace(hour=12)
    samples = tuple(
        _power(NOW.replace(hour=10, minute=m), 2.0) for m in range(60)
    )
    series = build_observed_grid_series(
        GridPowerHistory(samples=samples), _NO_IMPORT, _NO_EXPORT,
        _hourly_72h_price_series().slots, now=now,
    )
    by_hour = {(s.start.date(), s.start.hour): s for s in series.slots}
    slot = by_hour[(TODAY, 10)]
    assert abs(slot.imported_kwh - 2.0) < 1e-6
    assert slot.exported_kwh == 0.0


def test_pure_export_slot_averages_to_exported_kwh():
    """A slot with steady 3 kW export for one hour → 3 kWh exported, 0 imported."""
    now = NOW.replace(hour=12)
    samples = tuple(
        _power(NOW.replace(hour=10, minute=m), -3.0) for m in range(60)
    )
    series = build_observed_grid_series(
        GridPowerHistory(samples=samples), _NO_IMPORT, _NO_EXPORT,
        _hourly_72h_price_series().slots, now=now,
    )
    by_hour = {(s.start.date(), s.start.hour): s for s in series.slots}
    slot = by_hour[(TODAY, 10)]
    assert slot.imported_kwh == 0.0
    assert abs(slot.exported_kwh - 3.0) < 1e-6


# ---------------------------------------------------------------------------
# Mixed-sign slot — the bug fixed by the new module
# ---------------------------------------------------------------------------

def test_mixed_sign_slot_preserves_gross_flows():
    """30 min import @ 2 kW + 30 min export @ 2 kW → non-zero on both sides.

    The previous implementation (signed average then sign split) would collapse
    both to zero. The new per-sample split must report ~1 kWh each side.
    """
    now = NOW.replace(hour=12)
    base = NOW.replace(hour=10)
    samples = tuple(
        _power(base + timedelta(minutes=m), 2.0 if m < 30 else -2.0)
        for m in range(60)
    )
    series = build_observed_grid_series(
        GridPowerHistory(samples=samples), _NO_IMPORT, _NO_EXPORT,
        _hourly_72h_price_series().slots, now=now,
    )
    by_hour = {(s.start.date(), s.start.hour): s for s in series.slots}
    slot = by_hour[(TODAY, 10)]
    # mean(positive) over all 60 samples = (30*2 + 30*0)/60 = 1.0 kW * 1h
    assert abs(slot.imported_kwh - 1.0) < 1e-6
    assert abs(slot.exported_kwh - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# Partial slot at "now"
# ---------------------------------------------------------------------------

def test_partial_slot_is_clamped_at_now():
    """A slot only half-elapsed by `now` should report half of the kWh."""
    now = NOW.replace(hour=10, minute=30)
    samples = tuple(
        _power(NOW.replace(hour=10, minute=m), 4.0) for m in range(30)
    )
    series = build_observed_grid_series(
        GridPowerHistory(samples=samples), _NO_IMPORT, _NO_EXPORT,
        _hourly_72h_price_series().slots, now=now,
    )
    by_hour = {(s.start.date(), s.start.hour): s for s in series.slots}
    slot = by_hour[(TODAY, 10)]
    # 4 kW * 0.5h = 2 kWh
    assert abs(slot.imported_kwh - 2.0) < 1e-6


# ---------------------------------------------------------------------------
# End-of-day correction
# ---------------------------------------------------------------------------

def test_end_of_day_correction_scales_today_import_to_counter():
    """Today's imported sum should rescale to match the import counter total."""
    now = NOW.replace(hour=12)
    samples = tuple(
        _power(NOW.replace(hour=10, minute=m), 2.0) for m in range(60)
    )    # power-averaged: today_imp ≈ 2.0 kWh
    import_total = GridImportTodayHistory(
        samples=(_imp(NOW.replace(hour=11, minute=55), 3.0),)
    )
    series = build_observed_grid_series(
        GridPowerHistory(samples=samples), import_total, _NO_EXPORT,
        _hourly_72h_price_series().slots, now=now,
    )
    by_hour = {(s.start.date(), s.start.hour): s for s in series.slots}
    # factor = 3.0 / 2.0 = 1.5, applied to today only
    assert abs(by_hour[(TODAY, 10)].imported_kwh - 3.0) < 1e-6


def test_end_of_day_correction_independent_per_side():
    """Import scaling does not affect export and vice versa."""
    now = NOW.replace(hour=12)
    base = NOW.replace(hour=10)
    samples = tuple(
        _power(base + timedelta(minutes=m), 2.0 if m < 30 else -2.0)
        for m in range(60)
    )    # imp=1.0, exp=1.0 before correction
    import_total = GridImportTodayHistory(samples=(_imp(NOW.replace(hour=11), 2.0),))
    export_total = GridExportTodayHistory(samples=(_exp(NOW.replace(hour=11), 1.5),))
    series = build_observed_grid_series(
        GridPowerHistory(samples=samples), import_total, export_total,
        _hourly_72h_price_series().slots, now=now,
    )
    by_hour = {(s.start.date(), s.start.hour): s for s in series.slots}
    assert abs(by_hour[(TODAY, 10)].imported_kwh - 2.0) < 1e-6
    assert abs(by_hour[(TODAY, 10)].exported_kwh - 1.5) < 1e-6


def test_correction_skipped_when_factor_out_of_bounds():
    """A factor outside [0.5, 2.0] (counter wildly off) must skip correction."""
    now = NOW.replace(hour=12)
    samples = tuple(
        _power(NOW.replace(hour=10, minute=m), 2.0) for m in range(60)
    )    # power-averaged today_imp ≈ 2.0
    import_total = GridImportTodayHistory(
        samples=(_imp(NOW.replace(hour=11), 100.0),)
    )    # 100/2 = 50 → way out of bounds
    series = build_observed_grid_series(
        GridPowerHistory(samples=samples), import_total, _NO_EXPORT,
        _hourly_72h_price_series().slots, now=now,
    )
    by_hour = {(s.start.date(), s.start.hour): s for s in series.slots}
    assert abs(by_hour[(TODAY, 10)].imported_kwh - 2.0) < 1e-6


def test_correction_skipped_for_yesterday_slots():
    """Yesterday's slots are never scaled by today's counters."""
    now = NOW.replace(hour=2)    # early on TODAY; yesterday has full data
    yest_h10 = (NOW - timedelta(days=1)).replace(hour=10)
    samples = tuple(
        _power(yest_h10 + timedelta(minutes=m), 1.0) for m in range(60)
    )    # yesterday hour-10: imported_kwh = 1.0
    # Today's counter would scale, but no today samples here:
    import_total = GridImportTodayHistory(
        samples=(_imp(NOW.replace(hour=1), 5.0),)
    )
    series = build_observed_grid_series(
        GridPowerHistory(samples=samples), import_total, _NO_EXPORT,
        _hourly_72h_price_series().slots, now=now,
    )
    by_hour = {(s.start.date(), s.start.hour): s for s in series.slots}
    assert abs(by_hour[(YESTERDAY, 10)].imported_kwh - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# Series-level totals
# ---------------------------------------------------------------------------

def test_series_totals_match_per_slot_sums():
    """Series totals should equal the sum of per-slot values for each side."""
    now = NOW.replace(hour=12)
    yest_h10 = (NOW - timedelta(days=1)).replace(hour=10)
    samples = (
        # yesterday: 1 h pure import @ 2 kW = 2 kWh imp
        *tuple(_power(yest_h10 + timedelta(minutes=m), 2.0) for m in range(60)),
        # today: 1 h pure export @ 1.5 kW = 1.5 kWh exp
        *tuple(_power(NOW.replace(hour=10, minute=m), -1.5) for m in range(60)),
    )
    series = build_observed_grid_series(
        GridPowerHistory(samples=samples), _NO_IMPORT, _NO_EXPORT,
        _hourly_72h_price_series().slots, now=now,
    )
    assert abs(series.total_yesterday_imported_kwh - 2.0) < 1e-4
    assert series.total_yesterday_exported_kwh == 0.0
    assert series.total_today_imported_kwh == 0.0
    assert abs(series.total_today_exported_kwh - 1.5) < 1e-4
