"""Data-driven validation of the Combustion Control logic doc's State 1/2/3
target bands, using the documented (UNCHANGED) 7-parameter classification
logic against real plant history at native 1-second resolution.

Reuses backend.analysis.STATE_RULES / _classify_state directly (never
redefines them), so this can never silently drift from the logic actually
served by the app. Classifies every in-load minute (feed_gate via
config.KILN_FEED_MIN_TPH) into State 1/2/3 by the documented parameter
thresholds, joining in the eLIMS lab quality data (nearest-time, matching
twin/features.py's tolerance convention: DOC 4h, clinker QC 2h), then reports
the calciner outlet temperature (config.CALCINER_TEMP_TAG) actually observed
during each state -- cross-checked against native 1-second CSV data
(config.CSV_1S_PATH), which reproduces the same bands within rounding.

Key finding (2026-09): three of the seven voting parameters (kiln torque,
secondary air temp, hot-meal DOC) are now running ABOVE the doc's entire
documented range in current plant operation, so almost every minute's vote
saturates at State 3. State 1 (920-940 degC) triggers in only ~0.3% of
in-load history -- too sparse to treat as a validated band. States 2 and 3
are each backed by 190k+ minutes and are reliable, though they overlap far
more than the doc's tight 20 degC windows suggest. Full narrative in
docs/STATE_BAND_ANALYSIS.md.

Run:  python -m etl.analyze_state_bands [--source 1s|1min]
Out:  models/state_band_analysis.json
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

import config
from backend import db
from backend.analysis import STATE_RULES, _classify_state, CV as CALCINER_TEMP

OUT = Path(__file__).resolve().parent.parent / "models" / "state_band_analysis.json"


def _load_1min() -> pd.DataFrame:
    df = db.q(f'SELECT ts, "{CALCINER_TEMP}", "{config.KILN_FEED_TAG}", '
             '"433MD140_TQ", "423AM822_NO", "443KH050_TT1" FROM process_1min ORDER BY ts')
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def _load_1s() -> pd.DataFrame:
    import polars as pl
    lf = pl.scan_csv(config.CSV_1S_PATH,
                     schema_overrides={CALCINER_TEMP: pl.Float64,
                                       config.KILN_FEED_TAG: pl.Float64,
                                       "433MD140_TQ": pl.Float64,
                                       "423AM822_NO": pl.Float64,
                                       "443KH050_TT1": pl.Float64},
                     null_values=["", "NA", "N/A"])
    df = (lf.select(["Time", CALCINER_TEMP, config.KILN_FEED_TAG,
                     "433MD140_TQ", "423AM822_NO", "443KH050_TT1"])
          .collect(engine="streaming").to_pandas())
    df["ts"] = pd.to_datetime(df["Time"], format="%Y-%m-%d %H:%M:%S", errors="coerce")
    return df.drop(columns="Time").dropna(subset=["ts"])


def _join_lab(df: pd.DataFrame) -> pd.DataFrame:
    """eLIMS quality data, nearest-time join (matches twin/features.py)."""
    hm = db.q("SELECT ts, doc FROM lab_hotmeal WHERE doc IS NOT NULL")
    hm["ts"] = pd.to_datetime(hm["ts"])
    hm = hm.groupby("ts", as_index=False)["doc"].mean().sort_values("ts")   # avg sides A/B

    cl = db.q("SELECT ts, free_lime, liter_weight, liq FROM lab_clinker")
    cl["ts"] = pd.to_datetime(cl["ts"]).sort_values()
    cl = cl.sort_values("ts").rename(columns={"liq": "liquid"})

    df = df.sort_values("ts")
    df = pd.merge_asof(df, hm, on="ts", direction="nearest", tolerance=pd.Timedelta("4h"))
    df = pd.merge_asof(df, cl, on="ts", direction="nearest", tolerance=pd.Timedelta("2h"))
    return df


def classify_state(df: pd.DataFrame) -> pd.Series:
    """Per-row State 1/2/3 vote, using backend.analysis's OWN classifier --
    never redefined here -- averaged across whichever of the 7 parameters
    are available for that row, then rounded (matches simulate()'s own
    votes -> state_num logic)."""
    votes = pd.DataFrame(index=df.index)
    for key in STATE_RULES:
        votes[key] = df[key].apply(lambda v: _classify_state(key, None if pd.isna(v) else float(v)))
    return votes.mean(axis=1).round(), votes.notna().sum(axis=1)


def band_stats(cv: pd.Series) -> dict:
    return {"n": int(len(cv)), "mean": round(float(cv.mean()), 1), "std": round(float(cv.std()), 1),
            "p5": round(float(cv.quantile(.05)), 1), "p25": round(float(cv.quantile(.25)), 1),
            "median": round(float(cv.median()), 1),
            "p75": round(float(cv.quantile(.75)), 1), "p95": round(float(cv.quantile(.95)), 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["1s", "1min"], default="1min",
                    help="1min (fast, from process_1min) or 1s (slow, ~30s, "
                         "scans config.CSV_1S_PATH directly -- both give the "
                         "same bands within rounding, see module docstring)")
    args = ap.parse_args()
    t0 = time.time()

    df = _load_1s() if args.source == "1s" else _load_1min()
    print(f"[bands] loaded {len(df):,} {args.source} rows  ({time.time()-t0:.1f}s)")

    df = _join_lab(df)
    feed_ok = df[config.KILN_FEED_TAG] > config.KILN_FEED_MIN_TPH
    print(f"[bands] feed_gate (> {config.KILN_FEED_MIN_TPH} TPH): "
          f"{feed_ok.sum():,}/{len(df):,} rows pass")

    state_num, n_votes = classify_state(df)
    mask = feed_ok & state_num.notna() & df[CALCINER_TEMP].notna()
    print(f"[bands] avg votes used per row: {n_votes[mask].mean():.2f} of {len(STATE_RULES)}")
    print(f"[bands] usable rows: {mask.sum():,}")

    out = {"generated_at": pd.Timestamp.now().isoformat()[:19], "source": args.source,
          "feed_gate_tph": config.KILN_FEED_MIN_TPH, "logic_unchanged": True,
          "doc_target_bands": {"State 1": "920-940", "State 2": "900-920", "State 3": "880-900"},
          "states": {}}
    print(f"\n================ BAND PER STATE ({args.source}, logic unchanged) ================")
    for s in [1, 2, 3]:
        sel = mask & (state_num == s)
        cv = df.loc[sel, CALCINER_TEMP]
        if len(cv) == 0:
            continue
        stats = band_stats(cv)
        stats["pct_of_data"] = round(100 * len(cv) / mask.sum(), 1)
        out["states"][f"State {s}"] = stats
        print(f"  State {s}: n={stats['n']:>10,}  ({stats['pct_of_data']}%)  "
              f"mean={stats['mean']:.1f}+/-{stats['std']:.1f}  "
              f"band(p5-p95)=[{stats['p5']:.1f}, {stats['p95']:.1f}]")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2))
    print(f"\nsaved -> {OUT}")
    print(f"elapsed: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
