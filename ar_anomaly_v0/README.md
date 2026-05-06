# AR Anomaly Engine — v0

Prophet-based AR anomaly detector. Companion to the existing `anomaly_agent.py`
in the repo root, which slices by **processing channel** — this v0 slices by
**issuing country** and runs at three time horizons (daily / weekly / monthly).

See [../V0_PROPOSAL.md](../V0_PROPOSAL.md) for the design doc.

## Setup

Dependencies are managed with [uv](https://docs.astral.sh/uv/). From the repo root:

```bash
uv sync                                # creates .venv and installs everything
gcloud auth application-default login  # one-time
```

Prophet pulls `cmdstanpy` and a Stan toolchain — first sync can take a
minute or two.

## Run

```bash
uv run python -m ar_anomaly_v0.main
```

This pulls the trailing 180 days from BigQuery, slices the data four ways
(merchant / merchant×CIT-MIT / merchant×country / merchant×country×CIT-MIT),
forecasts the expected AR for each slice, and writes flagged anomalies to
`AR_Anomalies_YYYY-MM-DD.csv` in this folder.

Optional flags:
- `--run-date YYYY-MM-DD` — backdate the run (target date is `run-date − 1`).
- `--output-dir PATH` — change where the CSV is written.

## Module map

| File | Responsibility |
|---|---|
| `config.py` | Project IDs, merchant list, volume thresholds, Prophet settings |
| `bq_io.py` | BigQuery pull + CSV write |
| `slices.py` | Build per-slice daily AR series; country volume-share helper |
| `baselines/prophet_model.py` | Prophet fit + forecast (≥90d history) |
| `baselines/flat_mean.py` | Trailing mean ± 1.28σ fallback (<90d history) |
| `detect.py` | Daily prediction-interval test + weekly/monthly z-test |
| `severity.py` | P1/P2 labels and lost-volume calc |
| `main.py` | Orchestration |

## Output schema

| Column | Notes |
|---|---|
| `run_date` | When the engine ran |
| `period_type` | `daily` \| `weekly` \| `monthly` |
| `period_end` | Last date of the observed window |
| `slice_level` | `merchant` \| `merchant_cit_mit` \| `merchant_country` \| `merchant_country_cit_mit` |
| `alias_name` | Salesforce alias |
| `issuing_country` | NULL for L1/L2 slices |
| `is_mit` | NULL for L1/L3 slices |
| `observed_ar` / `expected_ar` | |
| `ar_lower_bound` / `ar_upper_bound` | 80% prediction interval |
| `requested_volume` / `accepted_volume` | Over the window |
| `country_vol_share_pct` | Country's share of merchant volume (last 30d) |
| `direction` | `drop` \| `spike` |
| `method` | `prophet` \| `flat_trailing_mean` |
| `p_value` | NaN for daily (interval test); two-prop z-test for weekly/monthly |
| `severity` | See below |
| `total_volume_amount_lost` | `(expected_ar − observed_ar) × requested × atv` |
| `atv` | Avg transaction value over the window |
| `date_of_detection` | Same as `run_date` |

### Severity

| Status | When |
|---|---|
| `P1: Critical - Global Merchant Outage` | Drop at the merchant level |
| `P1: Critical - Country Outage` | Drop in a country handling >50% of merchant volume |
| `P2: Country-level drop` | Drop in a non-dominant country |
| `P2: Spike` | AR moved up significantly |

## Things to calibrate before turning on

1. **Volume thresholds** in `config.py` are placeholders. Plot AR
   coefficient-of-variation against requested volume per period for your
   merchants and pick the elbow.
2. **Merchant list** — the default is empty (= all merchants). For the v0
   shadow run, fill in the ~10 you want to monitor.
3. **`PROPHET_INTERVAL_WIDTH`** is 0.80; tighten if you see too much noise.
4. **`P_VALUE_THRESHOLD`** is 0.01 for the weekly/monthly z-test.

## Known limitations of v0

- No multiple-testing correction. At ~150 slices × 3 periods this is
  acceptable; revisit if false positives become the bottleneck.
- No per-merchant seasonality or holiday calendars (BFCM, regional).
  Prophet's auto-seasonality covers weekly cycles only.
- No alert deduplication — a slice that's anomalous Monday and Tuesday
  produces two rows.
- Output is CSV. BigQuery target table is a v1 deliverable.
- Weekly/monthly prediction-interval bounds in the CSV are reused from the
  daily forecast for display; the actual statistical test is the z-test.
