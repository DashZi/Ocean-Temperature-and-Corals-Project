from __future__ import annotations

from pathlib import Path
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
# не могу разобраться, какой из интерпретеров использовать, чтобы там были установлены все нужные библиотеки.
# а вообще, мне нужно оставить только 1 питон и убрать 2 остальных


# ---------- Config ----------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLEAN_PATH = PROJECT_ROOT / "data" / "cleaned" / "ocean_clean.csv"
REPORT_PATH = PROJECT_ROOT / "outputs" / "tables" / "data_quality_report.csv"

SQL_SERVER = r"localhost\SQLEXPRESS"
SQL_DATABASE = "OceanDB"
SQL_SCHEMA = "dbo"
SQL_TABLE = "realistic_ocean_climate_dataset"

# If you have ODBC Driver 18 installed, you can switch to:
# DRIVER = "ODBC Driver 18 for SQL Server"
DRIVER = "ODBC Driver 17 for SQL Server"

# Domain rules (tune if you want)
RANGES = {
    "latitude": (-90, 90),
    "longitude": (-180, 180),
    "sst_c": (-2, 40),
    "ph_level": (6.5, 9.0),
    "species_observed": (0, 1_000_000),
}

SEVERITY_MAP = {"low": 1, "medium": 2, "high": 3}

def make_engine():
    # Windows Integrated Auth (Trusted Connection)
    # Note: TrustServerCertificate avoids SSL issues on local SQL Server setups.
    conn_str = (
        f"mssql+pyodbc://@{SQL_SERVER}/{SQL_DATABASE}"
        f"?driver={DRIVER.replace(' ', '+')}"
        f"&trusted_connection=yes"
        f"&TrustServerCertificate=yes"
    )
    return create_engine(conn_str, fast_executemany=True)


def extract_from_sql(engine) -> pd.DataFrame:
    # Quote column names defensively (Date is a reserved-ish word sometimes)
    sql = text(f"""
        SELECT
            [Date],
            [Location],
            [Latitude],
            [Longitude],
            [SST_C],
            [pH_Level],
            [Bleaching_Severity],
            [Species_Observed],
            [Marine_Heatwave]
        FROM [{SQL_SCHEMA}].[{SQL_TABLE}];
    """)
    return pd.read_sql(sql, engine)


def standardize_and_rename(df: pd.DataFrame) -> pd.DataFrame:
    # Match your SQL table columns exactly -> canonical names
    rename = {
        "Date": "date",
        "Location": "location",
        "Latitude": "latitude",
        "Longitude": "longitude",
        "SST_C": "sst_c",
        "pH_Level": "ph_level",
        "Bleaching_Severity": "bleaching_severity",
        "Species_Observed": "species_observed",
        "Marine_Heatwave": "marine_heatwave",
    }
    return df.rename(columns=rename).copy()


def coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # date already comes as date from SQL, but coercing is safe
    df["date"] = pd.to_datetime(df["date"], errors="coerce")

    # numerics
    for col in ["latitude", "longitude", "sst_c", "ph_level", "species_observed"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # bleaching severity -> numeric index
    df["bleaching_severity"] = (
        df["bleaching_severity"]
        .astype("string")
        .str.strip()
        .str.lower()
        .replace({"none": pd.NA, "nan": pd.NA, "": pd.NA})
    )
    df["severity_index"] = df["bleaching_severity"].map(SEVERITY_MAP)

    # Marine_Heatwave is bit in SQL -> should come as 0/1 or bool, normalize to bool
    df["marine_heatwave"] = df["marine_heatwave"].fillna(0)
    df["marine_heatwave"] = df["marine_heatwave"].astype(int).astype(bool)

    return df


def apply_range_checks(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.copy()
    flags = []

    for col, (lo, hi) in RANGES.items():
        if col not in df.columns:
            continue
        bad = df[col].notna() & ((df[col] < lo) | (df[col] > hi))
        n_bad = int(bad.sum())
        if n_bad:
            flags.append({
                "column": col,
                "bad_count": n_bad,
                "example_bad_values": df.loc[bad, col].head(5).tolist(),
            })
            # soft delete
            df.loc[bad, col] = np.nan

    return df, pd.DataFrame(flags)


def compute_sst_anomaly(df: pd.DataFrame) -> pd.DataFrame:
    """
    Dataset-derived anomaly:
      sst_anomaly = sst_c - mean(sst_c) for the same (location, month)
    """
    df = df.copy()
    df["month"] = df["date"].dt.month

    baseline = (
        df.groupby(["location", "month"], dropna=False)["sst_c"]
        .mean()
        .rename("sst_baseline_location_month")
        .reset_index()
    )
    df = df.merge(baseline, on=["location", "month"], how="left")
    df["sst_anomaly"] = df["sst_c"] - df["sst_baseline_location_month"]
    return df


def deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    before = len(df)
    df = df.drop_duplicates(subset=["location", "date"], keep="first")
    print(f"Deduped rows: {before - len(df)}")
    return df


def make_quality_report(df_raw: pd.DataFrame, df_clean: pd.DataFrame, range_flags: pd.DataFrame) -> pd.DataFrame:
    report = []
    report.append({"metric": "rows_raw", "value": len(df_raw)})
    report.append({"metric": "rows_clean", "value": len(df_clean)})
    report.append({"metric": "rows_dropped", "value": len(df_raw) - len(df_clean)})

    for col in df_clean.columns:
        report.append({"metric": f"missing_{col}", "value": int(df_clean[col].isna().sum())})

    if range_flags is not None and not range_flags.empty:
        for _, r in range_flags.iterrows():
            report.append({"metric": f"out_of_range_{r['column']}", "value": int(r["bad_count"])})
    else:
        report.append({"metric": "out_of_range_total", "value": 0})

    return pd.DataFrame(report)


def main(write_back_to_sql: bool = False) -> None:
    CLEAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    engine = make_engine()

    # 1) Extract
    df_raw = extract_from_sql(engine)
    print("Pulled rows from SQL:", len(df_raw))

    # 2) Standardize / coerce / validate
    df = standardize_and_rename(df_raw)
    df = coerce_types(df)
    df, range_flags = apply_range_checks(df)

    # 3) Drop rows missing critical fields for your analysis
    # target = severity_index, predictor = SST anomaly, needs date+location+sst
    before = len(df)
    df = df.dropna(subset=["date", "location", "sst_c", "severity_index"])
    print(f"Dropped rows missing required fields: {before - len(df)}")

    # 4) Compute anomaly
    df = compute_sst_anomaly(df)

    # 5) Deduplicate
    df = deduplicate(df)

    # 6) Save to CSV + report
    df.to_csv(CLEAN_PATH, index=False)
    print(f"Saved cleaned CSV -> {CLEAN_PATH}")

    report = make_quality_report(df_raw, df, range_flags)
    report.to_csv(REPORT_PATH, index=False)
    print(f"Saved quality report -> {REPORT_PATH}")

    # 7) Quick hypothesis sanity checks
    corr = df["sst_anomaly"].corr(df["severity_index"])
    print(f"Correlation: sst_anomaly vs severity_index = {corr:.3f}")

    hw_means = df.groupby("marine_heatwave")["severity_index"].mean()
    print("Mean severity_index by marine_heatwave:")
    print(hw_means)

    # Optional: write cleaned dataset back into SQL as a new table
    if write_back_to_sql:
        out_table = "realistic_ocean_climate_clean"
        df.to_sql(out_table, engine, schema=SQL_SCHEMA, if_exists="replace", index=False)
        print(f"Wrote cleaned table -> {SQL_SCHEMA}.{out_table}")


if __name__ == "__main__":
    # set True if you want the cleaned table written back to SQL Server
    main(write_back_to_sql=False)