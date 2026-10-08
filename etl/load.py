"""Orchestrator: clean all four sources and load them into the database.

Run:  python -m etl.load
Target DB is whatever config.database_url() resolves to (SQLite bootstrap now,
PostgreSQL once the password is set in .env and USE_SQLITE=0).
"""
from __future__ import annotations

import time

import pandas as pd
from sqlalchemy import create_engine, text

import config
from etl.clean import load_all_plc, load_hotmeal, load_clinker, validate_clinker


ROADMAP_SEED = [
    ("P0", "Data foundation", "Postgres schema + ingest 4 sources + QA report", "done", 1),
    ("P1", "Interactive App", "Roadmap, Data Explorer, Correlation Studio, Twin Simulator, Live Overview", "in_progress", 2),
    ("P2", "Digital Twin v1 (GBT)", "Temperature + combustion-safety models, SHAP, backtest", "planned", 3),
    ("P3", "Twin v2 + optimizer", "NN backup, Optuna optimizer, quality linkage, AFR-CV inference", "planned", 4),
    ("P4", "Live & shadow mode", "OPC live feed, twin shadows the CCR, operator insights", "planned", 5),
    ("P5", "Closed-loop advisory", "Setpoint guidance in CCR; variance/fuel/TSR gains", "planned", 6),
]


def refresh_clinker(engine) -> None:
    """Reload only lab_clinker from result.xlsx (Kiln-3 sheet), merged with the
    rows already in the DB so samples missing from the file are kept."""
    new = load_clinker()
    try:
        old = pd.read_sql("SELECT * FROM lab_clinker", engine)
        old["ts"] = pd.to_datetime(old["ts"], format="mixed")
    except Exception:
        old = pd.DataFrame(columns=new.columns)
    both = pd.concat([old.reindex(columns=new.columns), new], ignore_index=True)
    both = (validate_clinker(both).sort_values("ts")
            .drop_duplicates(subset=["ts"], keep="last").reset_index(drop=True))
    both.to_sql("lab_clinker", engine, if_exists="replace", index=False)
    with engine.begin() as cx:
        cx.execute(text("CREATE INDEX IF NOT EXISTS ix_clinker_ts ON lab_clinker (ts)"))
    print(f"lab_clinker: {len(old)} rows before, {len(new)} in file, {len(both)} after "
          f"({both['ts'].min()} -> {both['ts'].max()}, free_lime valid {both['free_lime'].notna().sum()})")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["clinker"],
                    help="reload just this source (others untouched)")
    args = ap.parse_args()
    url = config.database_url()
    print(f"Target DB: {url.split('@')[-1] if '@' in url else url}")
    engine = create_engine(url)
    if args.only == "clinker":
        refresh_clinker(engine)
        return

    t0 = time.time()
    print("\n[1/4] Cleaning + loading PLC process history (all months)...")
    process, dim, dq = load_all_plc()

    print("\n[2/4] Cleaning lab data...")
    hotmeal = load_hotmeal()
    clinker = load_clinker()

    print("\n[3/4] Writing tables to DB...")
    dim.to_sql("dim_tag", engine, if_exists="replace", index=False)
    dq.to_sql("data_quality", engine, if_exists="replace", index=False)
    # big table: chunked for driver friendliness
    process.to_sql("process_1min", engine, if_exists="replace", index=False, chunksize=10_000)
    hotmeal.to_sql("lab_hotmeal", engine, if_exists="replace", index=False)
    clinker.to_sql("lab_clinker", engine, if_exists="replace", index=False)
    pd.DataFrame(ROADMAP_SEED,
                 columns=["phase", "title", "detail", "status", "ordinal"]
                 ).to_sql("roadmap_milestone", engine, if_exists="replace", index=False)

    print("[4/4] Indexing...")
    with engine.begin() as cx:
        cx.execute(text("CREATE INDEX IF NOT EXISTS ix_process_ts ON process_1min (ts)"))
        cx.execute(text("CREATE INDEX IF NOT EXISTS ix_hotmeal_ts ON lab_hotmeal (ts)"))
        cx.execute(text("CREATE INDEX IF NOT EXISTS ix_clinker_ts ON lab_clinker (ts)"))

    dt = time.time() - t0
    print("\n================ INGESTION COMPLETE ================")
    print(f"process_1min : {len(process):>8} rows  {process['ts'].min()} -> {process['ts'].max()}")
    print(f"dim_tag      : {len(dim):>8} tags")
    print(f"lab_hotmeal  : {len(hotmeal):>8} rows  (DOC valid: {hotmeal['doc'].notna().sum()})")
    print(f"lab_clinker  : {len(clinker):>8} rows  (free_lime valid: {clinker['free_lime'].notna().sum()})")
    print(f"elapsed      : {dt:0.1f}s")
    print("\nData-quality (worst 8 by pct_valid):")
    print(dq.sort_values("pct_valid").head(8)[["tag", "name", "pct_valid", "n_out_of_range"]].to_string(index=False))


if __name__ == "__main__":
    main()
