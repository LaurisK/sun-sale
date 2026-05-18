"""Forecast accuracy: per-slot delta between forecast and observed generation.

Aligns `GenerationSeries` (forecast) with `ObservedGenerationSeries` (measured
from the inverter today-total counter) on identical `(start, end)` slots and
emits a `ForecastErrorSeries`. Both inputs are already resampled onto the
PriceSeries grid, so the alignment is a direct slot-by-slot match.

Sign convention: `error_kwh = observed - forecast` — positive means the
forecast under-predicted, negative means it over-predicted.

Today this module is read-only — it surfaces an error signal for monitoring
(MAE, bias, MAPE). It is intentionally structured so a future calibration
stage can consume the same series to fit a per-hour-of-day correction or to
score forecast sources against each other.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..contract.models import (
    ForecastErrorSeries,
    ForecastErrorSlot,
    GenerationSeries,
    ObservedGenerationSeries,
)


def build_forecast_error_series(
    forecast: GenerationSeries,
    observed: ObservedGenerationSeries,
    now: datetime | None = None,
) -> ForecastErrorSeries:
    """Align forecast and observed slots by (start, end) and compute error statistics.

    Unmatched forecast slots (no corresponding observation) receive sentinel
    values of -1.0 so the chart can render "no data yet" for future slots.

    Args:
        forecast: GenerationSeries from the forecast pipeline stage.
        observed: ObservedGenerationSeries built from inverter today-total samples.
        now: Cycle timestamp; defaults to UTC now.

    Returns:
        ForecastErrorSeries with per-slot deltas and aggregate MAE/bias/MAPE.
        Returns an all-zero series when forecast is empty.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if not forecast.slots:
        return _empty(now)

    # Iterate over every forecast slot so the chart always has something to
    # paint for each one. Slots without a matching observation get a -1
    # sentinel on observed_kwh / error_kwh (relative_error stays None).
    observed_by_key = {(s.start, s.end): s.generated_kwh for s in observed.slots}

    slots: list[ForecastErrorSlot] = []
    total_forecast = 0.0
    total_observed = 0.0
    abs_error_sum = 0.0
    matched = 0
    for fc in forecast.slots:
        obs_kwh = observed_by_key.get((fc.start, fc.end))
        if obs_kwh is None:
            slots.append(ForecastErrorSlot(
                start=fc.start,
                end=fc.end,
                forecast_kwh=round(fc.expected_kwh, 6),
                observed_kwh=-1.0,
                error_kwh=-1.0,
                relative_error=None,
            ))
            continue
        err = obs_kwh - fc.expected_kwh
        rel = err / fc.expected_kwh if fc.expected_kwh != 0.0 else None
        slots.append(ForecastErrorSlot(
            start=fc.start,
            end=fc.end,
            forecast_kwh=round(fc.expected_kwh, 6),
            observed_kwh=round(obs_kwh, 6),
            error_kwh=round(err, 6),
            relative_error=round(rel, 6) if rel is not None else None,
        ))
        total_forecast += fc.expected_kwh
        total_observed += obs_kwh
        abs_error_sum += abs(err)
        matched += 1

    if matched == 0:
        # Forecast present, observation entirely missing — totals are sentinels.
        return ForecastErrorSeries(
            slots=tuple(slots),
            total_forecast_kwh=round(sum(s.expected_kwh for s in forecast.slots), 4),
            total_observed_kwh=-1.0,
            total_error_kwh=-1.0,
            mean_absolute_error_kwh=-1.0,
            bias_kwh=-1.0,
            mean_absolute_percentage_error=None,
            computed_at=now,
        )

    total_error = total_observed - total_forecast
    mae = abs_error_sum / matched
    bias = total_error / matched
    mape = abs_error_sum / total_forecast if total_forecast > 0 else None

    return ForecastErrorSeries(
        slots=tuple(slots),
        total_forecast_kwh=round(total_forecast, 4),
        total_observed_kwh=round(total_observed, 4),
        total_error_kwh=round(total_error, 4),
        mean_absolute_error_kwh=round(mae, 6),
        bias_kwh=round(bias, 6),
        mean_absolute_percentage_error=round(mape, 6) if mape is not None else None,
        computed_at=now,
    )


def _empty(now: datetime) -> ForecastErrorSeries:
    """Return an all-zero ForecastErrorSeries sentinel for when no forecast exists.

    Args:
        now: Cycle timestamp stamped into computed_at.

    Returns:
        ForecastErrorSeries with empty slot tuple and all numeric fields zero.
    """
    return ForecastErrorSeries(
        slots=(),
        total_forecast_kwh=0.0,
        total_observed_kwh=0.0,
        total_error_kwh=0.0,
        mean_absolute_error_kwh=0.0,
        bias_kwh=0.0,
        mean_absolute_percentage_error=None,
        computed_at=now,
    )


