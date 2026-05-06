"""Generate slice combinations and apply per-period volume thresholds.

A slice is a (alias, issuing_country, is_mit) tuple where issuing_country
and is_mit may be None to denote "all". Each slice produces its own AR
time series.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from . import config


SLICE_LEVELS = [
    "merchant",
    "merchant_cit_mit",
    "merchant_country",
    "merchant_country_cit_mit",
]


@dataclass(frozen=True)
class SliceKey:
    alias_name: str
    issuing_country: str | None
    is_mit: bool | None

    @property
    def level(self) -> str:
        has_country = self.issuing_country is not None
        has_mit = self.is_mit is not None
        if has_country and has_mit:
            return "merchant_country_cit_mit"
        if has_country:
            return "merchant_country"
        if has_mit:
            return "merchant_cit_mit"
        return "merchant"


def build_slice_series(history: pd.DataFrame) -> dict[SliceKey, pd.DataFrame]:
    """Return one daily AR time series per slice.

    Output frames have columns: requested_date, accepted_sum, requested_sum,
    daily_atv, ar.
    """
    series: dict[SliceKey, pd.DataFrame] = {}

    for alias in history["alias_name"].unique():
        merchant = history[history["alias_name"] == alias]
        if merchant.empty:
            continue

        # L1: merchant only
        series[SliceKey(alias, None, None)] = _aggregate(merchant, ["requested_date"])

        # L2: merchant × CIT/MIT
        for is_mit, sub in merchant.groupby("is_mit"):
            series[SliceKey(alias, None, bool(is_mit))] = _aggregate(sub, ["requested_date"])

        # L3: merchant × country
        for country, sub in merchant.groupby("issuing_country"):
            series[SliceKey(alias, str(country), None)] = _aggregate(sub, ["requested_date"])

        # L4: merchant × country × CIT/MIT
        for (country, is_mit), sub in merchant.groupby(["issuing_country", "is_mit"]):
            series[SliceKey(alias, str(country), bool(is_mit))] = _aggregate(sub, ["requested_date"])

    return series


def _aggregate(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    out = (
        df.groupby(group_cols)
        .agg(
            accepted_sum=("accepted_sum", "sum"),
            requested_sum=("requested_sum", "sum"),
            daily_atv=("daily_atv", "mean"),
        )
        .reset_index()
        .sort_values(group_cols)
    )
    out["ar"] = out["accepted_sum"] / out["requested_sum"].where(out["requested_sum"] > 0)
    return out


def passes_volume_threshold(period: str, requested_volume: float) -> bool:
    return requested_volume >= config.VOLUME_THRESHOLDS[period]


def country_volume_share(history: pd.DataFrame, alias: str, country: str | None) -> float:
    """% of an alias's recent requested volume going through a country.

    Uses the trailing 30 days. Returns 100 when country is None (the slice
    is the whole merchant).
    """
    if country is None:
        return 100.0
    cutoff = history["requested_date"].max()
    if cutoff is None:
        return 0.0
    start = cutoff - pd.Timedelta(days=30)
    recent = history[(history["alias_name"] == alias) & (history["requested_date"] > start.date())]
    total = recent["requested_sum"].sum()
    if total == 0:
        return 0.0
    in_country = recent[recent["issuing_country"] == country]["requested_sum"].sum()
    return float(round(100 * in_country / total, 2))
