"""Entry point. Pulls data, runs detection, writes the dated CSV.

Usage:
    python -m ar_anomaly_v0.main
"""

from __future__ import annotations

import argparse
import os
from datetime import date

from . import bq_io, config, detect, severity
from .slices import build_slice_series, country_volume_share


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--output-dir", default=os.path.dirname(os.path.abspath(__file__)))
    args = parser.parse_args()

    print(f"[v0] pulling {config.HISTORY_DAYS} days of history")
    history = bq_io.pull_history(config.HISTORY_DAYS, config.MERCHANTS)
    print(f"[v0] pulled {len(history):,} rows across {history['alias_name'].nunique()} merchants")

    slice_series = build_slice_series(history)
    print(f"[v0] built {len(slice_series):,} slice series")

    detections = detect.run(slice_series, args.run_date)
    print(f"[v0] flagged {len(detections):,} anomalies")

    rows = [_to_csv_row(d, history, args.run_date) for d in detections]
    path = bq_io.write_csv(rows, args.run_date, args.output_dir)
    print(f"[v0] wrote {path}")


def _to_csv_row(d: "detect.Detection", history, run_date: date) -> dict:
    share = country_volume_share(history, d.slice_key.alias_name, d.slice_key.issuing_country)
    return {
        "run_date": run_date.isoformat(),
        "period_type": d.period_type,
        "period_end": d.period_end.isoformat(),
        "slice_level": d.slice_key.level,
        "alias_name": d.slice_key.alias_name,
        "issuing_country": d.slice_key.issuing_country,
        "is_mit": d.slice_key.is_mit,
        "observed_ar": round(d.observed_ar, 6),
        "expected_ar": round(d.expected_ar, 6),
        "ar_lower_bound": round(d.ar_lower_bound, 6),
        "ar_upper_bound": round(d.ar_upper_bound, 6),
        "requested_volume": d.requested_volume,
        "accepted_volume": d.accepted_volume,
        "country_vol_share_pct": share,
        "direction": d.direction,
        "method": d.method,
        "p_value": None if d.p_value != d.p_value else round(d.p_value, 6),  # NaN check
        "severity": severity.classify(d, share),
        "total_volume_amount_lost": severity.lost_volume_amount(d),
        "atv": round(d.atv, 4),
        "date_of_detection": run_date.isoformat(),
    }


if __name__ == "__main__":
    main()
