# AR Anomaly Detection — v0 Proposal

**Author:** Hugo Ducruc
**Date:** 2026-05-06
**Status:** Draft for team review

---

## 1. Goal

Build a statistically rigorous engine that flags meaningful drops (and spikes) in **Acceptance Rate** — `accepted payments / requested payments` — across multiple aggregation levels and multiple time horizons. The engine should produce an explainable, low-noise alert list an analyst can validate.

This complements (and partially overlaps with) the existing `anomaly_agent.py` in this repo, which uses a 15-day t-test sliced by **processing channel**. v0 focuses on the gap that tool does not cover: **issuing-country slicing, multi-period detection, and seasonality-aware baselines**.

---

## 2. Design principles

- Start simple. Anything we add must be explainable in one paragraph.
- Use well-known libraries (`prophet`, `scipy`, `pandas`, `google-cloud-bigquery`).
- Prefer false positives over false negatives — missing a real AR drop is the more painful mistake.
- One run per day. All windows are trailing.

---

## 3. Scope

**In scope (v0):**
- ~10 merchants, ~5 issuing countries each, both CIT and MIT
- Three time horizons: daily, weekly (trailing 7d), monthly (trailing 30d)
- Two-sided detection (drops are P1, spikes are P2)
- Prophet baseline with a flat-trailing-mean fallback for new merchants (<90 days history)
- Output written to a dated CSV; BigQuery table to follow once the schema is finalised

**Out of scope (deferred to v1+):**
- Multiple-testing correction (e.g. FDR / Bonferroni) — at ~150 slices the risk is manageable; revisit if alert noise grows
- Per-merchant seasonality config and explicit holiday calendars (BFCM, regional holidays)
- Slack / Looker integration — analyst reads the CSV directly
- Fraud and dispute metrics — the existing tool covers those
- Cross-day alert deduplication

---

## 4. Slicing strategy

For each `(merchant, run_date)`, generate every slice in the table below, then drop any slice whose trailing volume is below the per-period threshold (see §6). Pruning low-volume slices is the simplest way to keep false-positive count low and to avoid AR ratios that swing wildly on a handful of transactions.

| Level | Slice keys |
|---|---|
| L1 | merchant |
| L2 | merchant × CIT/MIT |
| L3 | merchant × issuing country |
| L4 | merchant × issuing country × CIT/MIT |

**Why all four levels:** an anomaly can hide in a country slice while the merchant total looks flat (caveat raised in the brief). Conversely, a small steady drop everywhere only shows up at the merchant level. We need both.

---

## 5. Time periods

All windows are trailing and the engine runs once daily after data lands.

| Period | Observed value | Forecast target |
|---|---|---|
| Daily | yesterday's AR | Prophet `yhat` for yesterday |
| Weekly | trailing 7-day AR ending yesterday | trailing 7-day AR vs. expected |
| Monthly | trailing 30-day AR ending yesterday | trailing 30-day AR vs. expected |

**Important detail for weekly/monthly:** AR is computed as `sum(accepted) / sum(requested)` over the window — never as the average of daily ARs. Otherwise high-volume days are under-weighted and the metric drifts.

---

## 6. The expected value — Prophet, with a fallback

For each surviving slice, fit a forecast on the trailing AR series.

### Path A — Prophet (≥90 days of history)

- Daily AR series, with `weekly_seasonality=True`, `yearly_seasonality='auto'`, `daily_seasonality=False`.
- Volume included as an additional regressor (or, equivalently, we fit on accepted/requested counts and reconstruct AR), so the model can learn AR-vs-volume effects like end-of-month billing spikes.
- **80% prediction interval** for the forecast. Two-sided: anomaly when the actual is outside `[yhat_lower, yhat_upper]`.

Why Prophet: handles weekly seasonality and growth trends out of the box, well-documented, easy to explain to non-statisticians.

### Path B — Flat trailing mean (<90 days of history)

- Mean ± 1.28 · stddev over the trailing 30 days (the 1.28 multiplier mimics an 80% normal interval).
- Same downstream comparison logic.

Why a fallback: Prophet cannot reliably learn a weekly cycle from <12 weeks of data. New merchants would otherwise produce garbage forecasts.

### Statistical comparison for weekly / monthly

In addition to the Prophet interval check, we run a **two-proportion z-test** between the observed `(accepted, requested)` and the expected `(accepted, requested)` derived from the baseline rate × observed requested volume. This produces a clean, comparable p-value across slices.

---

## 7. Volume threshold (derived from variance)

AR is a ratio, so low-volume slices look dramatic for no reason. Rather than picking a fixed threshold, we calibrate one offline:

1. Compute the **coefficient of variation** of AR over the trailing 60 days for every slice.
2. Plot CV against daily volume.
3. Pick the volume threshold at the elbow where CV stabilises.

This is done **once offline** per period (daily / weekly / monthly), the resulting thresholds are saved in config, and we revisit periodically. No recomputation per run.

---

## 8. Severity and impact

Mirroring the existing tool's vocabulary so analysts aren't confused.

| Status | Trigger |
|---|---|
| `P1: Critical - Country Outage` | AR drop in a country slice that handles >50% of the merchant's volume |
| `P1: Critical - Global Merchant Outage` | AR drop at the merchant level, not concentrated in any one country |
| `P2: Country-level drop` | AR drop in a non-dominant country slice |
| `P2: Spike` | AR moved up significantly (worth flagging to the risk team — could indicate a fraud filter going down) |

**Lost volume** is computed as in the existing tool:
`(expected_ar − observed_ar) × requested_volume × atv`
Negative values for spikes.

---

## 9. Output

### v0 — CSV

Dated CSV at the repo root, modeled on the existing `AnomalyFile_YYYY-MM-DD.csv` so analysts already know how to read it. Proposed columns:

| Column | Notes |
|---|---|
| `run_date` | Date the engine ran |
| `period_type` | `daily` \| `weekly` \| `monthly` |
| `period_end` | Last date of the observed window |
| `slice_level` | `merchant` \| `merchant_cit_mit` \| `merchant_country` \| `merchant_country_cit_mit` |
| `alias_name` | Merchant alias |
| `issuing_country` | NULL for L1/L2 |
| `is_mit` | NULL for L1/L3 |
| `observed_ar` | |
| `expected_ar` | Prophet `yhat` or trailing mean |
| `ar_lower_bound` / `ar_upper_bound` | 80% interval |
| `requested_volume` / `accepted_volume` | Over the period |
| `country_vol_share_pct` | What % of merchant volume this country represents |
| `direction` | `drop` \| `spike` |
| `method` | `prophet` \| `flat_trailing_mean` |
| `p_value` | From the two-proportion z-test |
| `severity` | See §8 |
| `total_volume_amount_lost` | Same formula as existing tool |
| `atv` | Average transaction value |

### v1 — BigQuery

Same schema, append-only table, partitioned by `run_date`, clustered on `(alias_name, period_type)`. Schema to be finalised by Hugo.

---

## 10. Validation plan before turning on

1. **Backtest** on 6 months of history for 2–3 merchants with known historical incidents. Check that the engine flags them and that the alert volume is manageable.
2. **Sanity panel**: a notebook plotting observed AR, expected AR, and the prediction interval per slice. Visual check that the bounds look reasonable.
3. **Shadow run** for 2 weeks alongside the existing tool. Compare the two outputs daily. Disagreements get triaged manually before we trust the new engine.

---

## 11. Proposed module layout

```
ar_anomaly/
  config.py            # merchant list, volume thresholds, project IDs
  bq_io.py             # read fct_payin_daily; write CSV (later: BQ)
  slices.py            # generate slice combinations, apply volume filter
  baselines/
    prophet_model.py   # fit + forecast
    flat_mean.py       # fallback for new merchants
  detect.py            # daily / weekly / monthly comparison logic
  severity.py          # status labels + lost-volume calc
  main.py              # orchestration
```

---

## 12. Open items for the team

- Confirm `fct_payin_daily` (or the source we will use) exposes a clean `issuing_country` column.
- Decide on the eventual BigQuery destination dataset.
- Sign off on the four slice levels in §4 — should we add anything else (card scheme, BIN, payment method)?
- Identify 2–3 historical AR incidents we can use for the backtest in §10.
