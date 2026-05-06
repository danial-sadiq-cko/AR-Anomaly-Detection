"""Flat trailing-mean fallback for slices with insufficient history.

Mean ± k·stddev over the trailing window. k=1.28 mimics an 80% normal
interval, matching Prophet's default for v0.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from .. import config
from .prophet_model import Forecast


K_STD = 1.28  # ~80% interval under a normal


def forecast(series: pd.DataFrame, target_date: date) -> Forecast | None:
    start = target_date - timedelta(days=config.FLAT_MEAN_WINDOW_DAYS)
    window = series[
        (series["requested_date"] >= start) & (series["requested_date"] < target_date)
    ].dropna(subset=["ar"])
    if len(window) < 7:
        return None

    mean = float(window["ar"].mean())
    std = float(window["ar"].std(ddof=1)) if len(window) > 1 else 0.0
    return Forecast(
        yhat=mean,
        yhat_lower=mean - K_STD * std,
        yhat_upper=mean + K_STD * std,
        method="flat_trailing_mean",
    )
