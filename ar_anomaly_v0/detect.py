"""Run anomaly tests over each slice for daily / weekly / monthly periods.

Daily test: actual AR vs Prophet (or flat-mean) prediction interval.
Weekly/monthly test: trailing-window AR vs baseline rate via two-proportion
z-test. Both legs are computed against the same baseline source so the
"expected" AR is consistent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
from scipy.stats import norm

from . import config
from .baselines import flat_mean, prophet_model
from .baselines.prophet_model import Forecast
from .slices import SliceKey, passes_volume_threshold


PERIODS = {
    "daily":   1,
    "weekly":  7,
    "monthly": 30,
}


@dataclass
class Detection:
    slice_key: SliceKey
    period_type: str
    period_end: date
    observed_ar: float
    expected_ar: float
    ar_lower_bound: float
    ar_upper_bound: float
    requested_volume: int
    accepted_volume: int
    direction: str
    method: str
    p_value: float
    atv: float
    is_anomaly: bool


def run(slice_series: dict[SliceKey, pd.DataFrame], run_date: date) -> list[Detection]:
    detections: list[Detection] = []
    target_date = run_date - timedelta(days=1)  # data lands for "yesterday"

    for key, series in slice_series.items():
        if series.empty:
            continue

        baseline = _baseline(series, target_date)
        if baseline is None:
            continue

        for period, days in PERIODS.items():
            det = _evaluate_period(key, series, baseline, period, days, target_date)
            if det is not None:
                detections.append(det)

    return detections


def _baseline(series: pd.DataFrame, target_date: date) -> Forecast | None:
    fcst = prophet_model.fit_and_forecast(series, target_date)
    if fcst is not None:
        return fcst
    return flat_mean.forecast(series, target_date)


def _evaluate_period(
    key: SliceKey,
    series: pd.DataFrame,
    baseline: Forecast,
    period: str,
    days: int,
    target_date: date,
) -> Detection | None:
    start = target_date - timedelta(days=days - 1)
    window = series[(series["requested_date"] >= start) & (series["requested_date"] <= target_date)]

    requested = int(window["requested_sum"].sum())
    accepted = int(window["accepted_sum"].sum())
    if not passes_volume_threshold(period, requested):
        return None
    if requested == 0:
        return None

    observed_ar = accepted / requested
    expected_ar = baseline.yhat
    atv = float(window["daily_atv"].mean()) if not window.empty else 0.0

    if period == "daily":
        is_anomaly = (observed_ar < baseline.yhat_lower) or (observed_ar > baseline.yhat_upper)
        p_value = float("nan")
        lower, upper = baseline.yhat_lower, baseline.yhat_upper
    else:
        # Two-proportion z-test: compare observed (accepted, requested) vs.
        # the same volume scaled by the baseline rate.
        p_value = _two_proportion_pvalue(accepted, requested, expected_ar)
        is_anomaly = p_value < config.P_VALUE_THRESHOLD
        # For weekly/monthly we don't have a window-specific Prophet interval,
        # so reuse the daily band as an approximate display.
        lower, upper = baseline.yhat_lower, baseline.yhat_upper

    if not is_anomaly:
        return None

    direction = "drop" if observed_ar < expected_ar else "spike"
    return Detection(
        slice_key=key,
        period_type=period,
        period_end=target_date,
        observed_ar=observed_ar,
        expected_ar=expected_ar,
        ar_lower_bound=lower,
        ar_upper_bound=upper,
        requested_volume=requested,
        accepted_volume=accepted,
        direction=direction,
        method=baseline.method,
        p_value=p_value,
        atv=atv,
        is_anomaly=True,
    )


def _two_proportion_pvalue(accepted: int, requested: int, expected_rate: float) -> float:
    """Two-sided z-test against an expected proportion.

    Treats the baseline rate as the null hypothesis and tests whether the
    observed rate could have come from it. Uses the normal approximation,
    which is fine for the volumes we threshold to.
    """
    if requested == 0 or expected_rate <= 0 or expected_rate >= 1:
        return float("nan")
    observed_rate = accepted / requested
    se = math.sqrt(expected_rate * (1 - expected_rate) / requested)
    if se == 0:
        return float("nan")
    z = (observed_rate - expected_rate) / se
    return float(2 * norm.sf(abs(z)))
