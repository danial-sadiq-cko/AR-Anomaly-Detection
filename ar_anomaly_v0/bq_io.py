"""Read AR data from BigQuery and write the anomalies CSV."""

from __future__ import annotations

import csv
import os
from datetime import date

import pandas as pd
from google.cloud import bigquery

from . import config


# One row per (date, alias, issuing_country, is_mit). The engine slices and
# rolls up downstream — fewer round-trips, simpler SQL.
PULL_SQL = """
WITH base AS (
  SELECT
    DATE(fct_payin.requested_at)  AS requested_date,
    dim_salesforce_alias.salesforce_alias AS alias_name,
    fct_payin.issuing_country AS issuing_country,
    fct_payin.is_merchant_initiated AS is_mit,
    SUM(fct_payin.accepted_payments) AS accepted_sum,      -- payments that were accepted
    SUM(fct_payin.requested_payments) AS requested_sum     -- all payment attempts (accepted + declined)

  FROM `cko-data-plc-prod-1775.payment.fct_payin_daily` AS fct_payin
  LEFT JOIN `cko-data-ba-prod-1324.mapping.map_entity_merchant_to_salesforce_account`
    AS map_account_entity
    ON fct_payin.entity_id = map_account_entity.entity_id
  LEFT JOIN `cko-data-ba-prod-1324.mapping.map_entity_merchant_to_salesforce_account`
    AS map_account_merchant
    ON fct_payin.merchant_account_id = CAST(map_account_merchant.merchant_account_id AS STRING)
  LEFT JOIN `cko-data-ba-prod-1324.salesforce.dim_salesforce_account` AS dim_salesforce_account
    ON COALESCE(map_account_entity.account_id, map_account_merchant.account_id)
       = dim_salesforce_account.account_id
  LEFT JOIN `cko-data-ba-prod-1324.salesforce.dim_salesforce_alias` AS dim_salesforce_alias
    ON dim_salesforce_account.salesforce_alias = dim_salesforce_alias.salesforce_alias
  WHERE fct_payin.requested_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @history_days DAY)
    AND fct_payin.issuing_country IS NOT NULL
    AND fct_payin.issuing_country = 'United Kingdom'
    {merchant_filter}
  GROUP BY 1, 2, 3, 4
)
SELECT * FROM base
WHERE alias_name IS NOT NULL
"""


def pull_history(history_days: int, merchants: list[str]) -> pd.DataFrame:
    client = bigquery.Client(project=config.BILLING_PROJECT)

    if merchants:
        merchant_filter = "AND dim_salesforce_alias.salesforce_alias IN UNNEST(@merchants)"
        params = [
            bigquery.ScalarQueryParameter("history_days", "INT64", history_days),
            bigquery.ArrayQueryParameter("merchants", "STRING", merchants),
        ]
    else:
        merchant_filter = ""
        params = [bigquery.ScalarQueryParameter("history_days", "INT64", history_days)]

    sql = PULL_SQL.format(merchant_filter=merchant_filter)
    job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    df = job.result().to_dataframe(create_bqstorage_client=False)

    df["requested_date"] = pd.to_datetime(df["requested_date"]).dt.date
    df["accepted_sum"] = df["accepted_sum"].fillna(0).astype(int)
    df["requested_sum"] = df["requested_sum"].fillna(0).astype(int)
    df["daily_atv"] = df["daily_atv"].fillna(0.0).astype(float)
    return df


CSV_COLUMNS = [
    "run_date",
    "period_type",
    "period_end",
    "slice_level",
    "alias_name",
    "issuing_country",
    "is_mit",
    "observed_ar",
    "expected_ar",
    "ar_lower_bound",
    "ar_upper_bound",
    "requested_volume",
    "accepted_volume",
    "country_vol_share_pct",
    "direction",
    "method",
    "p_value",
    "severity",
    "total_volume_amount_lost",
    "atv",
    "date_of_detection",
]


def write_csv(rows: list[dict], run_date: date, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"AR_Anomalies_{run_date.isoformat()}.csv")
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col) for col in CSV_COLUMNS})
    return path
