"""Prophet forecast for daily AR.

Fits one model per slice on the trailing AR series, returns the forecast
for the requested target date with a prediction interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd
from prophet import Prophet

from .. import config


@dataclass
class Forecast:
    yhat: float
    yhat_lower: float
    yhat_upper: float
    method: str = "prophet"


def fit_and_forecast(series: pd.DataFrame, target_date: date) -> Forecast | None:
    """Fit Prophet on `series` and return the forecast for `target_date`.

    `series` must have columns `requested_date` and `ar`. Returns None if
    there's not enough history for Prophet — caller should fall back.
    """
    history = series.dropna(subset=["ar"])
    history = history[history["requested_date"] < target_date]
    if len(history) < config.PROPHET_MIN_HISTORY_DAYS:
        return None

    df = history[["requested_date", "ar"]].rename(columns={"requested_date": "ds", "ar": "y"})
    df["ds"] = pd.to_datetime(df["ds"])

    model = Prophet(
        weekly_seasonality=True,
        yearly_seasonality="auto",
        daily_seasonality=False,
        interval_width=config.PROPHET_INTERVAL_WIDTH,
    )
    model.fit(df)

    future = pd.DataFrame({"ds": [pd.Timestamp(target_date)]})
    forecast = model.predict(future).iloc[0]
    return Forecast(
        yhat=float(forecast["yhat"]),
        yhat_lower=float(forecast["yhat_lower"]),
        yhat_upper=float(forecast["yhat_upper"]),
    )
