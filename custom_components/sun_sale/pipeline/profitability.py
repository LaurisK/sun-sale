"""Profitability scoring: how attractive is today's peak vs recent history.

Pure Python — no Home Assistant imports, no third-party deps.

Algorithm (see docs in `memory/project_profitability_scoring.md`):

  1. Classify each historical day into WEEKDAY / HOLIDAY / WEEKEND.
  2. Compute per-class median peak across the full history window.
  3. Normalise every daily peak by its class median (adjusted = peak / median).
  4. Score today's adjusted peak as its percentile rank within the most
     recent N days of adjusted peaks.

The module accepts an `is_holiday(date) -> bool` predicate so callers can
plug in the `holidays` package (or any other source) without this module
depending on it.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from statistics import median
from typing import Callable, Iterable

from ..contract.models import (
    DailyPeak,
    DayClass,
    PriceHistory,
    PriceSeries,
    ProfitabilityScore,
)


# Minimum days required in the rolling window before a score is meaningful.
MIN_HISTORY_DAYS = 14

# Days used for the percentile rank (the rolling window).
DEFAULT_RANK_WINDOW_DAYS = 30


def classify_day(
    d: date,
    is_holiday: Callable[[date], bool] | None = None,
) -> DayClass:
    """Return the day's class.

    Holiday-on-weekend collapses to WEEKEND (already the cheaper bucket).
    """
    if d.weekday() >= 5:                       # 5 = Sat, 6 = Sun
        return DayClass.WEEKEND
    if is_holiday is not None and is_holiday(d):
        return DayClass.HOLIDAY
    return DayClass.WEEKDAY


def compute_class_medians(
    peaks: Iterable[DailyPeak],
) -> dict[DayClass, float]:
    """Median peak per day-class across the full input window.

    Missing classes fall back to the WEEKDAY median (preferred) or, failing
    that, the overall median. A returned class with value `0.0` would break
    division during normalisation; callers should treat the dict as opaque
    and use `_class_divisor` to read it.
    """
    buckets: dict[DayClass, list[float]] = {c: [] for c in DayClass}
    for p in peaks:
        buckets[p.day_class].append(p.peak_eur_kwh)

    result: dict[DayClass, float] = {}
    for c, values in buckets.items():
        if values:
            result[c] = median(values)
    return result


def _class_divisor(
    medians: dict[DayClass, float],
    cls: DayClass,
    fallback_pool: list[float],
) -> float:
    """Pick a non-zero divisor for the given class.

    Order: class-specific median → WEEKDAY median → overall median of pool.
    Returns 1.0 if nothing usable (treats the data as already normalised).
    """
    for candidate in (medians.get(cls), medians.get(DayClass.WEEKDAY)):
        if candidate and candidate > 0:
            return candidate
    if fallback_pool:
        overall = median(fallback_pool)
        if overall > 0:
            return overall
    return 1.0


def percentile_rank(value: float, distribution: list[float]) -> float:
    """Percentile rank of `value` within `distribution`, in [0.0, 1.0].

    Uses the "mean rank" convention: ties contribute 0.5 each, so a value
    equal to every entry returns 0.5 (not 0.0 and not 1.0). Empty input
    returns 0.5 — the neutral midpoint.
    """
    if not distribution:
        return 0.5
    below = sum(1 for x in distribution if x < value)
    equal = sum(1 for x in distribution if x == value)
    return (below + 0.5 * equal) / len(distribution)


def today_peak_from_price_series(
    price_series: PriceSeries,
    today: date,
) -> float | None:
    """Highest spot price among slots whose start falls on `today`.

    Uses spot, not buy/sell, because the *market* signal is what we score —
    user-specific tariff fees shouldn't influence the relative ranking.
    """
    today_slots = [s for s in price_series.slots if s.start.date() == today]
    if not today_slots:
        return None
    return max(s.spot_eur_kwh for s in today_slots)


def compute_profitability_score(
    price_series: PriceSeries,
    history: PriceHistory,
    now: datetime | None = None,
    is_holiday: Callable[[date], bool] | None = None,
    rank_window_days: int = DEFAULT_RANK_WINDOW_DAYS,
) -> ProfitabilityScore:
    """Score today's peak price against recent history."""
    if now is None:
        now = datetime.now(timezone.utc)

    today = now.date()
    today_class = classify_day(today, is_holiday)
    today_peak = today_peak_from_price_series(price_series, today) or 0.0

    medians = compute_class_medians(history.peaks)
    raw_values = [p.peak_eur_kwh for p in history.peaks]

    # Restrict to the rolling rank window. `history.peaks` is sorted ascending,
    # so we keep the tail. Compare against the most recent days excluding today
    # itself — today's peak is the value being scored.
    window_peaks = [p for p in history.peaks if p.day != today][-rank_window_days:]

    if len(window_peaks) < MIN_HISTORY_DAYS:
        return ProfitabilityScore(
            score=None,
            today_peak_eur_kwh=today_peak,
            today_class=today_class,
            class_medians=medians,
            window_days=len(window_peaks),
            computed_at=now,
        )

    adjusted_distribution = [
        p.peak_eur_kwh / _class_divisor(medians, p.day_class, raw_values)
        for p in window_peaks
    ]
    adjusted_today = today_peak / _class_divisor(medians, today_class, raw_values)
    score = percentile_rank(adjusted_today, adjusted_distribution)

    return ProfitabilityScore(
        score=score,
        today_peak_eur_kwh=today_peak,
        today_class=today_class,
        class_medians=medians,
        window_days=len(window_peaks),
        computed_at=now,
    )


def daily_peak_from_entries(
    day: date,
    entries: Iterable,
    is_holiday: Callable[[date], bool] | None = None,
) -> DailyPeak | None:
    """Build a `DailyPeak` for `day` from a flat list of `PriceEntry`-likes.

    Convenience for the coordinator when persisting today's history at end of
    day. Each entry needs `.start` (datetime) and `.price_eur_kwh` attributes.
    Returns None if no entries fall on `day`.
    """
    matching = [e.price_eur_kwh for e in entries if e.start.date() == day]
    if not matching:
        return None
    return DailyPeak(
        day=day,
        peak_eur_kwh=max(matching),
        day_class=classify_day(day, is_holiday),
    )
