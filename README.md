# Anomaly Detection Agent

## What This Does

This tool automatically detects when a merchant's payment performance has moved significantly outside its normal range. Every time it runs it queries the last 30 days of payment data from BigQuery, applies statistical anomaly detection across three metrics, and writes all flagged merchants to a dated CSV file. It also posts a summary of the top 5 merchants to the `#anomaly-detection` Slack channel automatically.

**Three metrics are monitored:**

| Metric | What it measures | Flagged when... |
|---|---|---|
| **Acceptance Rate (AR)** | % of payment requests that are accepted | Rate drops significantly below normal |
| **Fraud Rate** | % of accepted payments reported as fraud | Rate spikes significantly above normal |
| **Dispute Rate** | % of accepted payments that are disputed | Rate spikes significantly above normal |

---

## Folder Structure

```
AnomalyDetection/
├── anomaly_agent.py       # Main script — run this to generate the CSV
├── requirements.txt       # Python dependencies
├── README.md              # This file
├── AnomalyFile_YYYY-MM-DD.csv   # Output file (created on each run)
└── slack_summary.json     # Intermediate file used to post to Slack (auto-generated)
```

---

## How to Run

### First-time setup

```bash
# 1. Navigate to the folder
cd ~/Desktop/AnomalyDetection

# 2. Install dependencies (one-time only)
pip install -r requirements.txt

# 3. Authenticate with Google Cloud (one-time only, or if your credentials expire)
gcloud auth application-default login
```

### Running the agent

```bash
cd ~/Desktop/AnomalyDetection
python3 anomaly_agent.py
```

That's it. The agent will:
1. Connect to BigQuery and run the anomaly detection query
2. Write results to `AnomalyFile_YYYY-MM-DD.csv` in the same folder
3. Automatically post a Slack summary to `#anomaly-detection`

Running it again on the same day deletes the old file and writes a fresh one.

**Prerequisites:**
- Python 3.8 or higher
- Access to the `cko-ca-prod-6784` GCP project
- Google Cloud SDK authenticated (`gcloud auth application-default login`)

---

## What You Get

### Output CSV

A file named `AnomalyFile_YYYY-MM-DD.csv` is created in the `AnomalyDetection` folder each time the agent runs. Each row represents one merchant channel where an anomaly was detected.

**To share results:** just send the CSV — no credentials or software needed to open it.

### Slack Summary

After writing the CSV, the agent automatically posts a summary to `#anomaly-detection` showing the **top 5 merchants by Lost Volume Amount**. The message breaks down:
- Merchant name, date, anomaly type, total lost payments and volume
- Per-channel breakdown with severity status and a direct Looker link for each channel

Slack posting is handled by Claude's built-in Slack MCP integration — no webhook URL or manual setup is required.

---

## Understanding the Output File

| Column | What it means |
|---|---|
| `Anomaly_Type` | Which metric triggered the flag: `AR Anomaly`, `Fraud Anomaly`, or `Dispute Anomaly` |
| `overall_status` | Severity classification (see Severity Levels below) |
| `alias_name` | The merchant's name (Salesforce alias) |
| `channel_id` | The specific processing channel ID where the anomaly was detected |
| `is_mit` | Whether transactions are Merchant-Initiated (`True`) or Customer-Initiated (`False`) |
| `t_stat_alias_global` | How far the merchant's overall metric has moved from its 15-day average, in standard deviations. Values beyond ±2.576 are statistically significant at 99% confidence |
| `t_stat_channel_specific` | Same as above but measured at the individual channel level rather than across the whole merchant |
| `channel_vol_share_pct` | What % of this merchant's total payment volume flows through this channel. High % = this is the merchant's primary channel |
| `Total_Volume_Amount_Lost` | **AR only.** Estimated USD revenue lost due to the AR drop: `(normal AR − actual AR) × volume × avg transaction value` |
| `Lost_Volume` | **AR:** estimated payments that should have been accepted but weren't. **Fraud/Dispute:** raw count of fraud reports or disputes yesterday |
| `ATV` | **AR only.** Average Transaction Value (USD) for this channel yesterday |
| `Date_of_Detection` | The date the agent was run |
| `Link_to_Look` | Clickable link to the Looker dashboard pre-filtered to this merchant, MIT flag, and the **single channel ID in that row** — each row links to its own channel independently |
| `Account_Manager_Name` | Account manager from Salesforce |
| `Account_Manager_Email` | Account manager email from Salesforce |

### Severity Levels

| Status | What it means |
|---|---|
| `P1: Critical - Primary Channel Outage` | AR dropped sharply **and** this channel handles >50% of the merchant's volume — likely their main route is failing |
| `P1: Critical - Global Merchant Outage` | AR dropped sharply across the merchant but not concentrated in one dominant channel |
| `P1: Critical - Primary Channel Spike` | Fraud or dispute rate spiked sharply **and** this is the merchant's primary channel |
| `P1: Critical - Global Merchant Spike` | Fraud or dispute rate spiked across the merchant but not in one dominant channel |

---

## How the Detection Works

### The Core Idea

For each merchant and channel, the agent calculates a **15-day statistical baseline** (mean and standard deviation) for the metric. It then compares yesterday's value against that baseline using a **t-test** — a standard statistical technique that measures how unusual a data point is relative to its history.

```
t = (yesterday's value − 15-day average) / (standard deviation ÷ √number_of_days)
```

- A t-stat of **−2.576 or lower** means the value has dropped to a level that would only happen by chance 1% of the time — statistically significant at 99% confidence.
- A t-stat of **+2.576 or higher** means the value has spiked to the same significance level.

### Why Two T-Stats?

An anomaly is only flagged when **both** conditions are true:
1. The **merchant's overall metric** (across all channels) is significantly off
2. **This specific channel's metric** is also significantly off

This dual check reduces false positives. A single noisy channel won't trigger an alert unless the whole merchant is also showing a problem — and vice versa.

### Anomaly Thresholds

| Metric | Flag direction | Statistical threshold | Volume threshold |
|---|---|---|---|
| Acceptance Rate | DROP | Both t-stats < −2.576 | Revenue loss > $1,000 |
| Fraud Rate | INCREASE | Both t-stats > +2.576 | ≥ 5 fraud reports yesterday |
| Dispute Rate | INCREASE | Both t-stats > +2.576 | ≥ 3 disputes yesterday |

The volume thresholds prevent low-volume channels from generating noise — a channel with 2 fraud reports going from 1 to 2 would produce a huge t-stat but isn't actionable.

---

## Data Sources

| Source | Purpose |
|---|---|
| `cko-data-plc-prod-1775.payment.fct_payin_daily` | Daily payment metrics per merchant/channel (accepted, requested, fraud, dispute counts) |
| `cko-data-plc-prod-1775.salesforce.map_entity_merchant_to_salesforce_account` | Maps internal entity/merchant IDs to Salesforce account IDs |
| `cko-data-plc-prod-1775.salesforce.dim_salesforce_account` | Maps Salesforce account IDs to merchant alias names |
| `cko-data-plc-prod-1775.salesforce.dim_salesforce_alias` | Resolves the final display name (alias) for each merchant |
| `cko-data-ba-prod-1324.int_salesforce.int_salesforce_alias` | Maps alias to account manager name and email |

The query looks back **30 days** to compute the 15-day baseline with enough buffer for data availability.

---

## SQL Structure — CTE Walkthrough

The SQL query is built as a chain of CTEs (Common Table Expressions). Each CTE is one step in the pipeline:

```
raw_performance
      │
      ├── alias_daily_rollup  ──► alias_level_stats  ──┐
      │                                                 ├──► ar_computed  ──┐
      ├── channel_level_stats ────────────────────────►─┘                   │
      │                                                                      │
      ├── fraud_alias_daily  ──► fraud_alias_stats  ──┐                     │
      │                                               ├──► fraud_computed   ├──► UNION ALL ──► Final CSV
      ├── fraud_channel_stats ──────────────────────►─┘                     │
      │                                                                      │
      ├── dispute_alias_daily ──► dispute_alias_stats ──┐                   │
      │                                                  ├──► dispute_computed ─┘
      └── dispute_channel_stats ──────────────────────►─┘
```

**Step 1 — `raw_performance`**: Pulls 30 days of raw daily totals per merchant, channel, and MIT flag. Adds up accepted payments, requested payments, fraud reports, disputes, and computes average transaction value.

**Step 2 — Alias daily rollups** (`alias_daily_rollup`, `fraud_alias_daily`, `dispute_alias_daily`): Collapses channel-level data up to the merchant level and computes a daily rate for each metric.

**Step 3 — Alias-level stats** (`alias_level_stats`, `fraud_alias_stats`, `dispute_alias_stats`): For each merchant, calculates the 15-day mean, standard deviation, and yesterday's value for the metric.

**Step 4 — Channel-level stats** (`channel_level_stats`, `fraud_channel_stats`, `dispute_channel_stats`): Same as Step 3 but kept at the individual channel granularity, plus yesterday's volume and ATV.

**Step 5 — Computed anomaly tables** (`ar_computed`, `fraud_computed`, `dispute_computed`): Joins merchant stats and channel stats together to calculate the two t-statistics and business impact metrics for every channel.

**Step 6 — Final UNION ALL**: Filters each computed table to only rows that breach the anomaly thresholds, applies severity labels, and combines all three metric types into one result set.

---

## Key Technical Decisions

- **`SAFE_DIVIDE` and `NULLIF`**: Prevent division-by-zero errors on channels with no payment volume on a given day. Without this, the query would fail on inactive channels.
- **`COALESCE(..., 0)` for fraud and dispute**: Days with zero fraud or disputes would otherwise produce `NULL` rates, which would be excluded from the mean/stddev calculation and skew the baseline. Defaulting to 0 keeps them in the baseline.
- **`CAST(NULL AS FLOAT64)`**: Fraud and dispute rows don't have a revenue loss figure or ATV. These columns are left empty (`NULL`) with an explicit type so BigQuery's `UNION ALL` doesn't have a type mismatch error.
- **30-day lookback, 15-day effective baseline**: The `WHERE` clause fetches 30 days so there's always enough data even if yesterday's data arrives late. The `STDDEV`/`AVG` then naturally use however many days are present.
- **Double-quoted alias in Looker URL**: The `Alias=` filter parameter wraps the merchant name in double quotes (e.g. `Alias=%22Zilch%20USA%2C%20Inc.%22`). Without this, Looker interprets the comma in names like "Zilch USA, Inc." as a delimiter between two separate filter values. The surrounding `%22` characters (URL-encoded double quotes) tell Looker to treat everything inside as a single string.
- **Per-channel Looker links**: Each row's `Link_to_Look` URL filters on only that row's channel ID, so clicking the link opens Looker scoped to that single channel. This replaced an earlier approach that included all of a merchant's channels in every link.
- **Slack message splitting**: The Slack summary is split into one message per merchant (plus a header) so each post stays within Slack's 5000-character limit. Channel breakdown is capped at 5 channels per merchant; any overflow is noted with a reference to the CSV.
