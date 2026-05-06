"""
Anomaly Detection Agent
Folder: ~/Desktop/AnomalyDetection/
-----------------------------------------
Detects statistically significant anomalies across three payment metrics:
  - Acceptance Rate (AR)  — flags significant DROPS    (t-stat < -2.576)
  - Fraud Rate            — flags significant INCREASES (t-stat > +2.576)
  - Dispute Rate          — flags significant INCREASES (t-stat > +2.576)

Results are written to a dated CSV file in the same folder:
    AnomalyFile_YYYY-MM-DD.csv

A slack_summary.json is also written; Claude reads it and posts the top-5
summary to #anomaly-detection via its Slack MCP integration.

Usage:
    cd ~/Desktop/AnomalyDetection
    python3 anomaly_agent.py

Requirements:
    pip install -r requirements.txt
    gcloud auth application-default login  (if not already authenticated)
"""

import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date
from urllib.parse import quote

from google.cloud import bigquery

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BILLING_PROJECT    = "aicoe-452013"
OUTPUT_DIR         = os.path.dirname(os.path.abspath(__file__))

# Slack channel ID for #anomaly-detection.
# Messages are posted via Claude's Slack MCP integration — no webhook URL needed.
SLACK_CHANNEL_ID = "C0AK1TJ5P16"

COLUMN_ORDER = [
    "Anomaly_Type",              # A
    "overall_status",            # B
    "alias_name",                # C
    "channel_id",                # D
    "is_mit",                    # E
    "t_stat_alias_global",       # F
    "t_stat_channel_specific",   # G
    "channel_vol_share_pct",     # H
    "Total_Volume_Amount_Lost",  # I
    "Lost_Volume",               # J
    "ATV",                       # K
    "Date_of_Detection",         # L — today's date; used in the Looker URL date range
    "Link_to_Look",              # M — clickable HYPERLINK formula to the Looker dashboard
    "Account_Manager_Name",      # N — account manager from Salesforce
    "Account_Manager_Email",     # O — account manager email from Salesforce
]

# ---------------------------------------------------------------------------
# SQL Query
# ---------------------------------------------------------------------------

ANOMALY_SQL = """
-- ============================================================
-- ANOMALY DETECTION QUERY
-- ============================================================
-- Detects statistically significant anomalies in three metrics:
--   1. Acceptance Rate (AR)  — flags significant DROPS
--   2. Fraud Rate            — flags significant SPIKES
--   3. Dispute Rate          — flags significant SPIKES
--
-- Method: one-sample t-test comparing yesterday's value against
-- a 15-day rolling baseline (mean + standard deviation).
-- A result is flagged when the t-statistic breaches ±2.576,
-- which corresponds to 95% statistical confidence.
--
-- Both the merchant-level AND channel-level t-stats must breach
-- the threshold before a row is included — this dual check
-- reduces false positives from noisy low-volume channels.
-- ============================================================

WITH raw_performance AS (
  -- ============================================================
  -- STEP 1: BASE DATA COLLECTION
  -- ============================================================
  -- Pull the last 30 days of daily payment activity, grouped by:
  --   merchant alias (alias_name), processing channel, and MIT flag.
  --
  -- MIT (Merchant-Initiated Transaction) means the merchant triggered
  -- the payment without the customer being present (e.g. a subscription
  -- renewal). Customer-initiated means the customer was present at
  -- checkout. These behave differently so we track them separately.
  --
  -- We join to Salesforce tables purely to resolve human-readable
  -- merchant names. The payment data itself uses internal IDs.
  --
  -- COALESCE(..., 0) on fraud and dispute counts ensures that days
  -- with zero incidents produce a rate of 0 rather than NULL, keeping
  -- them in the baseline calculation.
  SELECT
    DATE(fct_payin.requested_at)                                                    AS requested_date,
    dim_salesforce_alias.salesforce_alias                                           AS alias_name,
    fct_payin.processing_channel_id                                                 AS channel_id,
    fct_payin.is_merchant_initiated                                                 AS is_mit,
    SUM(fct_payin.accepted_payments)                                                AS accepted_sum,      -- payments that were accepted
    SUM(fct_payin.requested_payments)                                               AS requested_sum,     -- all payment attempts (accepted + declined)
    COALESCE(SUM(fct_payin.captured_amount_usd), 0)
      / NULLIF(SUM(fct_payin.captured_payments), 0)                                AS daily_atv,         -- average transaction value in USD (NULLIF prevents divide-by-zero)
    COALESCE(SUM(fct_payin.fraud_reported_payments), 0)                             AS fraud_reported_sum, -- payments reported as fraudulent
    COALESCE(SUM(fct_payin.disputed_payments), 0)                                   AS disputed_sum       -- payments that were disputed by the cardholder
  FROM `cko-data-plc-prod-1775.payment.fct_payin_daily` AS fct_payin
  -- Join on entity_id first, then merchant_account_id as a fallback,
  -- to resolve the internal payment record to a Salesforce account
  LEFT JOIN `cko-data-ba-prod-1324.mapping.map_entity_merchant_to_salesforce_account`
    AS map_account_entity
    ON fct_payin.entity_id = map_account_entity.entity_id
  LEFT JOIN `cko-data-ba-prod-1324.mapping.map_entity_merchant_to_salesforce_account`
    AS map_account_merchant
    ON fct_payin.merchant_account_id = CAST(map_account_merchant.merchant_account_id AS STRING)
  -- Resolve Salesforce account ID to account details
  LEFT JOIN `cko-data-ba-prod-1324.salesforce.dim_salesforce_account` AS dim_salesforce_account
    ON COALESCE(map_account_entity.account_id, map_account_merchant.account_id)
       = dim_salesforce_account.account_id
  -- Resolve to the final human-readable merchant alias name
  LEFT JOIN `cko-data-ba-prod-1324.salesforce.dim_salesforce_alias` AS dim_salesforce_alias
    ON dim_salesforce_account.salesforce_alias = dim_salesforce_alias.salesforce_alias
  -- 30-day window gives enough data for a stable 15-day baseline,
  -- with buffer in case yesterday's data arrives slightly late
  WHERE fct_payin.requested_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  GROUP BY 1, 2, 3, 4
),

-- ====================================================================
-- ACCEPTANCE RATE (AR) ANOMALY DETECTION
-- Acceptance Rate = accepted payments / requested payments
-- A DROP in AR means more payments are being declined than usual.
-- We flag this when the drop is statistically significant at 95%
-- confidence (t-stat < -2.576) at BOTH the merchant and channel level.
-- ====================================================================

alias_daily_rollup AS (
  -- Roll up from channel level to merchant level for each day.
  -- This gives us the overall merchant acceptance rate per day,
  -- used to detect merchant-wide degradations vs. channel-specific ones.
  SELECT
    requested_date, alias_name, is_mit,
    SUM(accepted_sum) / NULLIF(SUM(requested_sum), 0) AS alias_ar_per_day  -- merchant-level daily AR
  FROM raw_performance
  GROUP BY 1, 2, 3
),

alias_level_stats AS (
  -- For each merchant, compute the 15-day statistical baseline for AR.
  -- alias_ar_t1   = yesterday's merchant AR (the value we're testing)
  -- alias_ar_mean = 15-day average AR (the expected normal value)
  -- alias_ar_std  = 15-day standard deviation (how much AR normally varies)
  -- alias_n       = number of days in the sample (used in the t-test denominator)
  SELECT
    alias_name, is_mit,
    MAX(CASE WHEN requested_date = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
             THEN alias_ar_per_day END)   AS alias_ar_t1,    -- yesterday's value
    AVG(alias_ar_per_day)                AS alias_ar_mean,   -- 15-day average
    STDDEV(alias_ar_per_day)             AS alias_ar_std,    -- 15-day standard deviation
    COUNT(DISTINCT requested_date)       AS alias_n          -- number of data points
  FROM alias_daily_rollup
  GROUP BY 1, 2
),

channel_level_stats AS (
  -- Same baseline statistics as alias_level_stats but at channel granularity.
  -- We need both levels because an issue might only appear in one channel
  -- while the merchant's overall AR looks fine (or vice versa).
  -- Also captures yesterday's volume and ATV for impact calculations.
  SELECT
    alias_name, channel_id, is_mit,
    MAX(CASE WHEN requested_date = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
             THEN accepted_sum / NULLIF(requested_sum, 0) END)  AS chan_ar_t1,    -- yesterday's channel AR
    AVG(accepted_sum / NULLIF(requested_sum, 0))                AS chan_ar_mean,  -- 15-day average channel AR
    STDDEV(accepted_sum / NULLIF(requested_sum, 0))             AS chan_ar_std,   -- 15-day standard deviation
    COUNT(DISTINCT requested_date)                              AS chan_n,         -- number of data points
    MAX(CASE WHEN requested_date = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
             THEN requested_sum END)                            AS chan_vol_t1,   -- yesterday's total payment attempts
    MAX(CASE WHEN requested_date = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
             THEN daily_atv END)                                AS chan_atv_t1    -- yesterday's average transaction value
  FROM raw_performance
  GROUP BY 1, 2, 3
),

ar_computed AS (
  -- Join merchant-level and channel-level stats to compute:
  --   1. Two t-statistics (how many standard errors away from normal)
  --   2. This channel's share of the merchant's total volume (to identify primary channels)
  --   3. Business impact: how many payments were lost and estimated revenue lost
  --
  -- T-stat formula: (observed - mean) / (std / sqrt(n))
  -- A value below -2.576 means the drop is statistically significant at 99% confidence.
  -- SAFE_DIVIDE is used throughout to handle channels with zero or NULL standard deviation.
  SELECT
    c.alias_name, c.channel_id, c.is_mit,
    -- Merchant-level t-stat: is the whole merchant's AR significantly below its baseline?
    SAFE_DIVIDE(
      (a.alias_ar_t1 - a.alias_ar_mean),
      SAFE_DIVIDE(a.alias_ar_std, SQRT(a.alias_n))
    )                                                                               AS t_stat_alias_global,
    -- Channel-level t-stat: is this specific channel's AR significantly below its baseline?
    SAFE_DIVIDE(
      (c.chan_ar_t1 - c.chan_ar_mean),
      SAFE_DIVIDE(c.chan_ar_std, SQRT(c.chan_n))
    )                                                                               AS t_stat_channel_specific,
    -- What % of the merchant's total volume goes through this channel?
    -- >50% means this is the merchant's primary channel.
    ROUND(SAFE_DIVIDE(c.chan_vol_t1,
      SUM(c.chan_vol_t1) OVER (PARTITION BY c.alias_name, c.is_mit)) * 100, 2)    AS channel_vol_share_pct,
    -- Estimated lost payments: how many more payments would have been accepted
    -- if AR had stayed at its 15-day average? GREATEST(0,...) prevents negatives.
    ROUND(GREATEST(0, (c.chan_ar_mean - c.chan_ar_t1)) * c.chan_vol_t1)            AS Lost_Volume,
    c.chan_atv_t1                                                                   AS ATV,
    -- Estimated revenue lost: lost payments × average transaction value
    ROUND(GREATEST(0, (c.chan_ar_mean - c.chan_ar_t1))
      * c.chan_vol_t1 * c.chan_atv_t1, 2)                                          AS Total_Volume_Amount_Lost
  FROM channel_level_stats c
  JOIN alias_level_stats a ON c.alias_name = a.alias_name AND c.is_mit = a.is_mit
  -- Only include channels that had activity yesterday (excludes channels with no data)
  WHERE c.chan_ar_t1 IS NOT NULL
),

-- ====================================================================
-- FRAUD RATE ANOMALY DETECTION
-- Fraud Rate = fraud-reported payments / accepted payments
-- A SPIKE in fraud rate means a higher-than-normal proportion of
-- accepted payments are being reported as fraudulent.
-- We flag this when the spike is statistically significant at 95%
-- confidence (t-stat > +2.576) at BOTH the merchant and channel level.
-- Lost_Volume here = raw count of fraud-reported payments yesterday.
-- ====================================================================

fraud_alias_daily AS (
  -- Daily merchant-level fraud rate: fraud reports as a proportion of accepted payments
  SELECT
    requested_date, alias_name, is_mit,
    SUM(fraud_reported_sum) / NULLIF(SUM(accepted_sum), 0) AS alias_fr_per_day
  FROM raw_performance
  GROUP BY 1, 2, 3
),

fraud_alias_stats AS (
  -- 15-day baseline for fraud rate at the merchant level
  SELECT
    alias_name, is_mit,
    MAX(CASE WHEN requested_date = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
             THEN alias_fr_per_day END)   AS alias_fr_t1,    -- yesterday's merchant fraud rate
    AVG(alias_fr_per_day)                AS alias_fr_mean,   -- 15-day average fraud rate
    STDDEV(alias_fr_per_day)             AS alias_fr_std,    -- 15-day standard deviation
    COUNT(DISTINCT requested_date)       AS alias_n
  FROM fraud_alias_daily
  GROUP BY 1, 2
),

fraud_channel_stats AS (
  -- 15-day baseline for fraud rate at the channel level.
  -- Also captures yesterday's accepted volume and fraud count for context.
  SELECT
    alias_name, channel_id, is_mit,
    MAX(CASE WHEN requested_date = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
             THEN fraud_reported_sum / NULLIF(accepted_sum, 0) END)  AS chan_fr_t1,    -- yesterday's channel fraud rate
    AVG(fraud_reported_sum / NULLIF(accepted_sum, 0))                AS chan_fr_mean,  -- 15-day average
    STDDEV(fraud_reported_sum / NULLIF(accepted_sum, 0))             AS chan_fr_std,   -- 15-day std deviation
    COUNT(DISTINCT requested_date)                                   AS chan_n,
    MAX(CASE WHEN requested_date = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
             THEN accepted_sum END)                                  AS chan_vol_t1,   -- accepted payment volume yesterday
    MAX(CASE WHEN requested_date = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
             THEN fraud_reported_sum END)                            AS chan_fraud_t1  -- number of fraud reports yesterday
  FROM raw_performance
  GROUP BY 1, 2, 3
),

fraud_computed AS (
  -- Same t-stat calculation as AR but in the positive direction.
  -- A t-stat > +2.576 means the fraud rate is significantly HIGHER than normal.
  -- Total_Volume_Amount_Lost and ATV are not applicable for fraud — left as NULL
  -- in the final UNION so the column types stay consistent.
  SELECT
    c.alias_name, c.channel_id, c.is_mit,
    -- Merchant-level t-stat: is the whole merchant's fraud rate significantly above baseline?
    SAFE_DIVIDE(
      (a.alias_fr_t1 - a.alias_fr_mean),
      SAFE_DIVIDE(a.alias_fr_std, SQRT(a.alias_n))
    )                                                                               AS t_stat_alias_global,
    -- Channel-level t-stat: is this specific channel's fraud rate significantly above baseline?
    SAFE_DIVIDE(
      (c.chan_fr_t1 - c.chan_fr_mean),
      SAFE_DIVIDE(c.chan_fr_std, SQRT(c.chan_n))
    )                                                                               AS t_stat_channel_specific,
    -- Volume share: is this the merchant's primary channel?
    ROUND(SAFE_DIVIDE(c.chan_vol_t1,
      SUM(c.chan_vol_t1) OVER (PARTITION BY c.alias_name, c.is_mit)) * 100, 2)    AS channel_vol_share_pct,
    -- Raw fraud report count yesterday (used as the volume threshold filter below)
    CAST(c.chan_fraud_t1 AS FLOAT64)                                                AS Lost_Volume
  FROM fraud_channel_stats c
  JOIN fraud_alias_stats a ON c.alias_name = a.alias_name AND c.is_mit = a.is_mit
  WHERE c.chan_fr_t1 IS NOT NULL  -- only channels active yesterday
),

-- ====================================================================
-- DISPUTE RATE ANOMALY DETECTION
-- Dispute Rate = disputed payments / accepted payments
-- A SPIKE in dispute rate means a higher-than-normal proportion of
-- accepted payments are being disputed by cardholders.
-- Uses identical logic to fraud detection above.
-- Lost_Volume here = raw count of disputed payments yesterday.
-- ====================================================================

dispute_alias_daily AS (
  -- Daily merchant-level dispute rate: disputes as a proportion of accepted payments
  SELECT
    requested_date, alias_name, is_mit,
    SUM(disputed_sum) / NULLIF(SUM(accepted_sum), 0) AS alias_dr_per_day
  FROM raw_performance
  GROUP BY 1, 2, 3
),

dispute_alias_stats AS (
  -- 15-day baseline for dispute rate at the merchant level
  SELECT
    alias_name, is_mit,
    MAX(CASE WHEN requested_date = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
             THEN alias_dr_per_day END)   AS alias_dr_t1,    -- yesterday's merchant dispute rate
    AVG(alias_dr_per_day)                AS alias_dr_mean,   -- 15-day average dispute rate
    STDDEV(alias_dr_per_day)             AS alias_dr_std,    -- 15-day standard deviation
    COUNT(DISTINCT requested_date)       AS alias_n
  FROM dispute_alias_daily
  GROUP BY 1, 2
),

dispute_channel_stats AS (
  -- 15-day baseline for dispute rate at the channel level
  SELECT
    alias_name, channel_id, is_mit,
    MAX(CASE WHEN requested_date = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
             THEN disputed_sum / NULLIF(accepted_sum, 0) END)  AS chan_dr_t1,      -- yesterday's channel dispute rate
    AVG(disputed_sum / NULLIF(accepted_sum, 0))                AS chan_dr_mean,    -- 15-day average
    STDDEV(disputed_sum / NULLIF(accepted_sum, 0))             AS chan_dr_std,     -- 15-day std deviation
    COUNT(DISTINCT requested_date)                             AS chan_n,
    MAX(CASE WHEN requested_date = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
             THEN accepted_sum END)                            AS chan_vol_t1,     -- accepted payment volume yesterday
    MAX(CASE WHEN requested_date = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
             THEN disputed_sum END)                            AS chan_dispute_t1  -- number of disputes yesterday
  FROM raw_performance
  GROUP BY 1, 2, 3
),

dispute_computed AS (
  -- Same structure as fraud_computed. t-stat > +2.576 = significant dispute rate increase.
  SELECT
    c.alias_name, c.channel_id, c.is_mit,
    -- Merchant-level t-stat: is the whole merchant's dispute rate significantly above baseline?
    SAFE_DIVIDE(
      (a.alias_dr_t1 - a.alias_dr_mean),
      SAFE_DIVIDE(a.alias_dr_std, SQRT(a.alias_n))
    )                                                                               AS t_stat_alias_global,
    -- Channel-level t-stat: is this channel's dispute rate significantly above baseline?
    SAFE_DIVIDE(
      (c.chan_dr_t1 - c.chan_dr_mean),
      SAFE_DIVIDE(c.chan_dr_std, SQRT(c.chan_n))
    )                                                                               AS t_stat_channel_specific,
    -- Volume share: is this the merchant's primary channel?
    ROUND(SAFE_DIVIDE(c.chan_vol_t1,
      SUM(c.chan_vol_t1) OVER (PARTITION BY c.alias_name, c.is_mit)) * 100, 2)    AS channel_vol_share_pct,
    -- Raw dispute count yesterday (used as the volume threshold filter below)
    CAST(c.chan_dispute_t1 AS FLOAT64)                                              AS Lost_Volume
  FROM dispute_channel_stats c
  JOIN dispute_alias_stats a ON c.alias_name = a.alias_name AND c.is_mit = a.is_mit
  WHERE c.chan_dr_t1 IS NOT NULL  -- only channels active yesterday
),

-- ====================================================================
-- STEP A: ACCOUNT MANAGER LOOKUP
-- ====================================================================
-- Pulls the account manager name and email for each merchant alias
-- from the Salesforce alias dimension table.
-- Joined at the end so it applies to all three anomaly types at once.
-- ====================================================================

account_manager_lookup AS (
  SELECT
    a.salesforce_alias,
    a.account_manager_name AS Account_Manager_Name,
    e.email                AS Account_Manager_Email
  FROM `cko-data-ba-prod-1324.salesforce.dim_salesforce_alias` a
  LEFT JOIN `cko-data-ba-prod-1324.salesforce.dim_employee` e
    ON a.account_manager_id = e.user_id
),

-- ====================================================================
-- STEP B: COMBINE ALL THREE ANOMALY TYPES
-- ====================================================================
-- Each SELECT block filters one metric type to only rows that breach
-- the anomaly threshold, labels the severity, and selects a consistent
-- set of columns. The three blocks are combined with UNION ALL.
--
-- Severity labels:
--   "Primary Channel" = this channel carries >50% of the merchant's volume
--   "Global Merchant" = anomaly is spread across the merchant (no single dominant channel)
--
-- Thresholds:
--   AR:      both t-stats < -2.576  AND revenue loss > $1,000
--   Fraud:   both t-stats > +2.576  AND >= 5 fraud reports yesterday
--   Dispute: both t-stats > +2.576  AND >= 3 disputes yesterday
--
-- The minimum count filters (5 fraud, 3 disputes) prevent low-volume
-- channels from generating noise — e.g. a channel going from 1 to 2
-- fraud reports would produce a huge t-stat but isn't actionable.
--
-- CAST(NULL AS FLOAT64) is used for columns that don't apply to a
-- given metric type, ensuring UNION ALL column types are compatible.
-- ====================================================================

all_anomalies AS (
  SELECT
    'AR Anomaly'                                                                    AS Anomaly_Type,
    CASE
      -- Channel handles majority of the merchant's volume: likely their primary route is failing
      WHEN t_stat_alias_global   < -2.576
       AND t_stat_channel_specific < -2.576
       AND channel_vol_share_pct  > 50    THEN 'P1: Critical - Primary Channel Outage'
      -- Anomaly present but not concentrated in one dominant channel
      WHEN t_stat_alias_global   < -2.576
       AND t_stat_channel_specific < -2.576 THEN 'P1: Critical - Global Merchant Outage'
      ELSE 'Alert: Significant Degradation'
    END                                                                             AS overall_status,
    alias_name, channel_id, is_mit,
    t_stat_alias_global, t_stat_channel_specific, channel_vol_share_pct,
    Total_Volume_Amount_Lost, Lost_Volume, ATV
  FROM ar_computed
  WHERE t_stat_alias_global   < -2.576
    AND t_stat_channel_specific < -2.576
    AND Total_Volume_Amount_Lost > 1000   -- filter out low-impact noise (< $1,000 revenue effect)

  UNION ALL

  SELECT
    'Fraud Anomaly'                                                                 AS Anomaly_Type,
    CASE
      -- Channel handles majority of volume and is showing a fraud spike
      WHEN t_stat_alias_global   > 2.576
       AND t_stat_channel_specific > 2.576
       AND channel_vol_share_pct  > 50   THEN 'P1: Critical - Primary Channel Spike'
      -- Fraud spike present across the merchant but not in one dominant channel
      WHEN t_stat_alias_global   > 2.576
       AND t_stat_channel_specific > 2.576 THEN 'P1: Critical - Global Merchant Spike'
      ELSE 'Alert: Significant Increase'
    END                                                                             AS overall_status,
    alias_name, channel_id, is_mit,
    t_stat_alias_global, t_stat_channel_specific, channel_vol_share_pct,
    CAST(NULL AS FLOAT64)                                                           AS Total_Volume_Amount_Lost,  -- not applicable for fraud
    Lost_Volume,                                                                    -- = number of fraud reports yesterday
    CAST(NULL AS FLOAT64)                                                           AS ATV                        -- not applicable for fraud
  FROM fraud_computed
  WHERE t_stat_alias_global   > 2.576
    AND t_stat_channel_specific > 2.576
    AND Lost_Volume >= 5          -- require at least 5 fraud reports to avoid single-incident noise

  UNION ALL

  SELECT
    'Dispute Anomaly'                                                               AS Anomaly_Type,
    CASE
      -- Channel handles majority of volume and is showing a dispute spike
      WHEN t_stat_alias_global   > 2.576
       AND t_stat_channel_specific > 2.576
       AND channel_vol_share_pct  > 50   THEN 'P1: Critical - Primary Channel Spike'
      -- Dispute spike present across the merchant but not in one dominant channel
      WHEN t_stat_alias_global   > 2.576
       AND t_stat_channel_specific > 2.576 THEN 'P1: Critical - Global Merchant Spike'
      ELSE 'Alert: Significant Increase'
    END                                                                             AS overall_status,
    alias_name, channel_id, is_mit,
    t_stat_alias_global, t_stat_channel_specific, channel_vol_share_pct,
    CAST(NULL AS FLOAT64)                                                           AS Total_Volume_Amount_Lost,  -- not applicable for disputes
    Lost_Volume,                                                                    -- = number of disputed payments yesterday
    CAST(NULL AS FLOAT64)                                                           AS ATV                        -- not applicable for disputes
  FROM dispute_computed
  WHERE t_stat_alias_global   > 2.576
    AND t_stat_channel_specific > 2.576
    AND Lost_Volume >= 3          -- require at least 3 disputes to avoid single-incident noise
)

-- ====================================================================
-- FINAL OUTPUT: join anomalies with account manager details
-- ====================================================================
-- LEFT JOIN so rows are kept even if no account manager is found in
-- Salesforce (e.g. unmapped merchants). Account manager fields will
-- be NULL for those rows rather than dropping the anomaly entirely.
-- ====================================================================

SELECT
  a.*,
  m.Account_Manager_Name,
  m.Account_Manager_Email
FROM all_anomalies a
LEFT JOIN account_manager_lookup m ON a.alias_name = m.salesforce_alias

-- Group by anomaly type, then within each type show highest-impact rows first.
-- COALESCE handles the NULL Total_Volume_Amount_Lost in fraud/dispute rows.
ORDER BY a.Anomaly_Type, a.is_mit, COALESCE(a.Total_Volume_Amount_Lost, 0) DESC
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_looker_link(alias: str, is_mit: bool) -> str:
    """
    Builds a =HYPERLINK() formula pointing to the Looker dashboard for this merchant.

    The URL filters the dashboard by:
      - MIT flag (Yes/No)
      - Date range: last 15 days (fixed Looker filter)
      - Merchant alias name (double-quoted so commas in names are not split)
      - The single processing channel ID for that row (filled in by run() via {CHANNELS})

    The formula is written as =HYPERLINK("url", "label") so Excel and Google Sheets
    render it as a clickable link when the CSV is opened.
    """
    is_mit_str = "Yes" if is_mit else "No"

    url = (
        "https://checkoutinternal.eu.looker.com/dashboards/16805"
        f"?Is%20Merchant%20Initiated%20(Yes%20%2F%20No)={is_mit_str}"
        f"&Requested%20Date=15%20days"
        f"&MCC%20Used%20Payment%20Level=&Account%20Manager%20Name%20(Salesforce)="
        f"&Alias={quote('\"' + alias + '\"', safe='')}"
        f"&Processing%20Channel%20ID={{CHANNELS}}"  # placeholder filled in run()
    )
    return url


def build_slack_messages(rows: list[dict], today: str) -> list[str]:
    """
    Builds the Slack messages for the top 5 merchants by Lost Volume Amount.

    Returns a list of strings — one message per merchant plus a header message —
    so each post stays well under Slack's 5000-character limit regardless of how
    many channels a merchant has.

    Only AR Anomaly rows are included. Channel breakdown is capped at 5 channels
    per merchant (sorted by lost amount descending); any overflow is noted.
    """
    alias_agg = defaultdict(lambda: {
        "total_lost_amt": 0.0,
        "total_lost_vol": 0.0,
        "anomaly_type": "AR Anomaly",
        "channels": [],
    })

    for row in rows:
        if row.get("Anomaly_Type") != "AR Anomaly":
            continue
        alias = row.get("alias_name") or ""
        amt   = float(row.get("Total_Volume_Amount_Lost") or 0)
        vol   = float(row.get("Lost_Volume") or 0)
        alias_agg[alias]["total_lost_amt"] += amt
        alias_agg[alias]["total_lost_vol"] += vol
        alias_agg[alias]["channels"].append({
            "channel_id":     row.get("channel_id", ""),
            "overall_status": row.get("overall_status", ""),
            "is_mit":         row.get("is_mit", ""),
            "lost_vol":       vol,
            "lost_amt":       amt,
            "link_to_look":   row.get("Link_to_Look", ""),
        })

    top5 = sorted(alias_agg.items(), key=lambda x: x[1]["total_lost_amt"], reverse=True)[:5]
    today_fmt = date.fromisoformat(today).strftime("%-d %b %Y")
    rank_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]

    # Message 1: header only
    header = "\n".join([
        "🤖 **Anomaly Detection Bot** — Acceptance Rate Anomaly Report",
        f"📅 **{today_fmt}**  |  Confidence Threshold: 99%  |  Baseline: 15-day rolling average",
        "",
        "Flagged **5 merchants** with the highest estimated processing volume impact due to "
        "acceptance rate drops vs. their historical baseline. "
        "Figures represent estimated lost payments and processing volume.",
    ])
    messages = [header]

    for i, (alias, data) in enumerate(top5):
        lines = [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            f"**{rank_emojis[i]}  {alias}**",
            f"> 📅 Date of Detection: **{today_fmt}**",
            f"> 🔍 Anomaly Type: **{data['anomaly_type']}**",
            f"> 💳 Lost Payments: **{int(data['total_lost_vol']):,}**",
            f"> 💰 Lost Volume Amount: **${data['total_lost_amt']:,.0f}**",
            "",
            "**Channel Breakdown:**",
        ]
        channels_sorted = sorted(data["channels"], key=lambda c: c["lost_amt"], reverse=True)
        shown    = channels_sorted[:5]
        overflow = len(channels_sorted) - len(shown)
        for ch in shown:
            mit_label = "Yes" if str(ch["is_mit"]).lower() in ("true", "yes", "1") else "No"
            m = re.search(r'HYPERLINK\("([^"]+)"', ch["link_to_look"])
            looker_part = f" | <{m.group(1)}|🔍 Looker>" if m else ""
            lines.append(
                f"• `{ch['channel_id']}` | {ch['overall_status']} | MIT: {mit_label} | "
                f"{int(ch['lost_vol']):,} payments | **${ch['lost_amt']:,.0f}**{looker_part}"
            )
        if overflow:
            lines.append(f"_...{overflow} more channel(s) — see CSV for full breakdown_")

        # Append footer divider to the last merchant block
        if i == len(top5) - 1:
            lines.append("")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("_Powered by Anomaly Detection Agent · Project: aicoe-452013_")

        messages.append("\n".join(lines))

    return messages


def write_slack_summary(rows: list[dict], today: str) -> None:
    """
    Writes the top-5 anomaly summary to slack_summary.json in the output directory.

    Stores a list of messages (one per merchant plus a header) so each post stays
    within Slack's 5000-character limit. Claude reads this file and posts each
    message in sequence to #anomaly-detection (C0AK1TJ5P16) via its Slack MCP
    integration. No webhook URL or manual setup is required.
    """
    ar_rows = [r for r in rows if r.get("Anomaly_Type") == "AR Anomaly"]
    if not ar_rows:
        print("No AR anomalies to report — slack_summary.json not written.")
        return

    messages = build_slack_messages(rows, today)
    summary_path = os.path.join(OUTPUT_DIR, "slack_summary.json")

    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump({"channel_id": SLACK_CHANNEL_ID, "messages": messages, "today": today}, f, indent=2)

    print(f"Slack summary written to slack_summary.json — Claude will post {len(messages)} message(s) to #anomaly-detection.")


def run():
    today = date.today().isoformat()
    output_filename = f"AnomalyFile_{today}.csv"
    output_path = os.path.join(OUTPUT_DIR, output_filename)

    print(f"Connecting to BigQuery (project: {BILLING_PROJECT})...")
    client = bigquery.Client(project=BILLING_PROJECT)

    print("Running anomaly detection query (AR + Fraud + Dispute)...")
    query_job = client.query(ANOMALY_SQL)
    results = list(query_job.result())

    rows = [dict(row) for row in results]

    # ------------------------------------------------------------------
    # Add Date_of_Detection and Link_to_Look to every row.
    # Each link filters on the single channel ID in that row so each
    # channel can be viewed independently in Looker.
    # ------------------------------------------------------------------
    for row in rows:
        alias   = row.get("alias_name") or ""
        is_mit  = bool(row.get("is_mit"))
        channel = row.get("channel_id") or ""

        url = build_looker_link(alias, is_mit).replace(
            "{CHANNELS}", quote(channel, safe="")
        )

        row["Date_of_Detection"] = today
        row["Link_to_Look"] = f'=HYPERLINK("{url}","🔍 View Channel Data {alias}")'

    if os.path.exists(output_path):
        os.remove(output_path)
        print(f"Removed existing {output_filename}")

    print(f"Writing results to {output_filename}...")
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMN_ORDER, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    if rows:
        ar_count      = sum(1 for r in rows if r["Anomaly_Type"] == "AR Anomaly")
        fraud_count   = sum(1 for r in rows if r["Anomaly_Type"] == "Fraud Anomaly")
        dispute_count = sum(1 for r in rows if r["Anomaly_Type"] == "Dispute Anomaly")
        print(f"Done — {len(rows)} total anomalies written to {output_filename}")
        print(f"  AR Anomalies:      {ar_count}")
        print(f"  Fraud Anomalies:   {fraud_count}")
        print(f"  Dispute Anomalies: {dispute_count}")
    else:
        print(f"Done — No anomalies detected. Empty file written to {output_filename}")

    # Write Slack summary JSON — Claude reads this and posts to #anomaly-detection via MCP.
    write_slack_summary(rows, today)

    return output_path


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
