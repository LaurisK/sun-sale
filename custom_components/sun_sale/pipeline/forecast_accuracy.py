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
    """Pair forecast and observed slots by `(start, end)` and emit error stats."""
    if now is None:
        now = datetime.now(timezone.utc)

    if not forecast.slots:
        return _empty(now)

    # No observed history yet (inverter sensor just configured, or no samples
    # collected): emit one -1-filled slot per forecast slot so consumers can
    # distinguish "accuracy pending" from "no forecast at all".
    if not observed.slots:
        return _pending(forecast, now)

    forecast_by_key = {(s.start, s.end): s.expected_kwh for s in forecast.slots}

    slots: list[ForecastErrorSlot] = []
    total_forecast = 0.0
    total_observed = 0.0
    abs_error_sum = 0.0
    for obs in observed.slots:
        fc_kwh = forecast_by_key.get((obs.start, obs.end))
        if fc_kwh is None:
            continue
        err = obs.generated_kwh - fc_kwh
        rel = err / fc_kwh if fc_kwh != 0.0 else None
        slots.append(ForecastErrorSlot(
            start=obs.start,
            end=obs.end,
            forecast_kwh=round(fc_kwh, 6),
            observed_kwh=round(obs.generated_kwh, 6),
            error_kwh=round(err, 6),
            relative_error=round(rel, 6) if rel is not None else None,
        ))
        total_forecast += fc_kwh
        total_observed += obs.generated_kwh
        abs_error_sum += abs(err)

    if not slots:
        return _empty(now)

    total_error = total_observed - total_forecast
    n = len(slots)
    mae = abs_error_sum / n
    bias = total_error / n
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


def _pending(forecast: GenerationSeries, now: datetime) -> ForecastErrorSeries:
    """Sentinel result for the "forecast known, observation not yet recovered"
    case. Each per-slot field that depends on observation is -1; consumers
    treat the -1 mark as invalid/no-data."""
    slots = tuple(
        ForecastErrorSlot(
            start=s.start,
            end=s.end,
            forecast_kwh=round(s.expected_kwh, 6),
            observed_kwh=-1.0,
            error_kwh=-1.0,
            relative_error=None,
        )
        for s in forecast.slots
    )
    return ForecastErrorSeries(
        slots=slots,
        total_forecast_kwh=round(sum(s.expected_kwh for s in forecast.slots), 4),
        total_observed_kwh=-1.0,
        total_error_kwh=-1.0,
        mean_absolute_error_kwh=-1.0,
        bias_kwh=-1.0,
        mean_absolute_percentage_error=None,
        computed_at=now,
    )
