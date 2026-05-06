"""Configuration for the AR anomaly v0 engine.

Edit MERCHANTS to change scope. All other defaults are sensible starting
points; tighten/loosen after the first shadow run.
"""

from dataclasses import dataclass, field
from datetime import date


BILLING_PROJECT = "cko-oca-qa-6931"

# Merchants to monitor. Empty list = all merchants meeting the volume threshold.
MERCHANTS: list[str] = [
    'Netflix'
    # "Klarna Bank AB",
    # ...
]

# How much history to pull. Prophet wants enough days to learn weekly
# seasonality; 180 days is a comfortable default.
HISTORY_DAYS = 180

# A merchant with fewer than this many days of history falls back to the
# flat trailing-mean baseline instead of Prophet.
PROPHET_MIN_HISTORY_DAYS = 90

# Trailing window used by the flat-mean fallback.
FLAT_MEAN_WINDOW_DAYS = 30

# Prediction interval width for Prophet. 0.80 = 80% interval.
PROPHET_INTERVAL_WIDTH = 0.80

# Significance threshold for the two-proportion z-test on weekly/monthly.
P_VALUE_THRESHOLD = 0.01

# Minimum requested volume for a slice to be tested.
# Calibrated by plotting AR coefficient-of-variation against volume and
# picking the elbow. These are placeholders — replace after calibration.
VOLUME_THRESHOLDS = {
    "daily":   500,
    "weekly":  3500,
    "monthly": 15000,
}

# Severity threshold: a country slice with this much of merchant volume
# is treated as the merchant's primary route.
PRIMARY_SHARE_PCT = 50.0


@dataclass
class RunContext:
    run_date: date
    output_dir: str
    history_days: int = HISTORY_DAYS
    merchants: list[str] = field(default_factory=lambda: list(MERCHANTS))
