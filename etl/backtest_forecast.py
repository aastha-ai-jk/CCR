"""Independently validate backend/forecast.py against REAL historical 1-second
data -- proves the deployed model + feature engineering (not just the sandbox
that trained it) actually reproduces the reported MAE, using this repo's own
code path (backend.forecast.forecast_from_recent), not a re-import of the
training script's evaluation.

Reads a bounded window from config.CSV_1S_PATH (not process_1min -- that's
1-minute-aggregated and can't supply the model's per-second lag/rolling
features), steps a query timestamp across it, and for each query point:
  1. slices the preceding buffer (>= backend.forecast.MIN_BUFFER_S seconds)
  2. calls backend.forecast.forecast_from_recent() on it (the exact function
     the live API endpoint uses)
  3. compares the prediction to the REAL actual value 60s later in the CSV

Run:  python -m etl.backtest_forecast [--hours 3] [--step-min 5] [--end "2026-08-10 12:00:00"]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config                          # noqa: E402
from backend import forecast           # noqa: E402

BUFFER_MARGIN_S = 120                  # extra lead-in beyond MIN_BUFFER_S


def load_window(end: str, hours: float) -> pd.DataFrame:
    import polars as pl
    start = (pd.Timestamp(end) - pd.Timedelta(hours=hours)
             - pd.Timedelta(seconds=forecast.MIN_BUFFER_S + BUFFER_MARGIN_S))
    header = pl.scan_csv(config.CSV_1S_PATH, infer_schema_length=0).collect_schema().names()
    overrides = {c: pl.Float64 for c in header if c != "Time"}
    print(f"[backtest] scanning {config.CSV_1S_PATH} for {start} -> {end} "
          "(full-file scan; CSV has no random seek by date, ~2-3 min)")
    t0 = time.time()
    lf = (pl.scan_csv(config.CSV_1S_PATH, schema_overrides=overrides,
                      null_values=["", "NA", "N/A"])
            .filter((pl.col("Time") >= str(start)) & (pl.col("Time") <= end)))
    df = lf.collect(streaming=True).to_pandas()
    df["ts"] = pd.to_datetime(df["Time"])
    df = df.drop(columns="Time").sort_values("ts").reset_index(drop=True)
    print(f"[backtest] loaded {len(df):,} rows in {time.time()-t0:.1f}s")
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=3.0, help="query window length")
    ap.add_argument("--step-min", type=float, default=5.0, help="minutes between queries")
    ap.add_argument("--end", type=str, default="2026-08-10 12:00:00",
                    help="end of the query window (within the held-out test period)")
    ap.add_argument("--bias-correct", type=float, default=None,
                    help="additive degC correction to apply to predictions before scoring")
    args = ap.parse_args()

    df = load_window(args.end, args.hours)
    q_start = pd.Timestamp(args.end) - pd.Timedelta(hours=args.hours)
    step = pd.Timedelta(minutes=args.step_min)

    # The model was trained against a Savitzky-Golay DENOISED target (training's
    # preprocess() denoises the whole frame, including the target column,
    # before extracting y = target.shift(-60s)) -- so the fair "actual" to
    # score against is the denoised value, not the raw noisy CSV reading.
    # Denoise the target column once, over the whole loaded window, using the
    # SAME causal (trailing-only) rolling mean training now uses -- unlike the
    # old centered Savitzky-Golay filter, this never looks at samples after
    # the point being denoised, so it's a fair, leakage-free ground truth.
    full_idx = pd.date_range(df["ts"].min(), df["ts"].max(), freq="1s")
    cv_grid = (df.set_index("ts")[forecast.CV].reindex(full_idx)
              .ffill().interpolate(limit_direction="both"))
    cv_denoised = cv_grid.rolling(forecast.SMOOTH_WINDOW, min_periods=1).mean()

    preds, actuals, ts_used = [], [], []
    t = q_start
    while t <= pd.Timestamp(args.end):
        buf_start = t - pd.Timedelta(seconds=forecast.MIN_BUFFER_S + BUFFER_MARGIN_S)
        buf = df[(df["ts"] > buf_start) & (df["ts"] <= t)]
        out = forecast.forecast_from_recent(buf)
        target_ts = t + pd.Timedelta(seconds=60)
        if out.get("available") and target_ts in cv_denoised.index:
            actual = cv_denoised.loc[target_ts]
            if pd.notna(actual):
                preds.append(out["predicted_temp_60s"]); actuals.append(float(actual))
                ts_used.append(t)
        t += step

    if not preds:
        print("[backtest] no usable query points -- widen --hours or check CSV_1S_PATH")
        return

    preds, actuals = np.array(preds), np.array(actuals)
    err = preds - actuals
    mae, rmse, bias = np.mean(np.abs(err)), np.sqrt(np.mean(err ** 2)), np.mean(err)
    print(f"\n================ BACKTEST ({len(preds)} query points) ================")
    print(f"window: {q_start} -> {args.end}")
    meta = forecast._model()[1]
    train_mae = meta.get("test_metrics_absolute_degC", meta.get("test_metrics", {})).get("mae")
    print(f"MAE={mae:.3f} degC  RMSE={rmse:.3f} degC  BIAS(pred-actual)={bias:+.3f} degC  "
          f"(training-reported: MAE={train_mae:.3f})")
    if args.bias_correct is not None:
        c_err = (preds + args.bias_correct) - actuals
        print(f"AFTER +{args.bias_correct:.2f} correction: "
              f"MAE={np.mean(np.abs(c_err)):.3f}  RMSE={np.sqrt(np.mean(c_err**2)):.3f}")
    for tt, p, a in list(zip(ts_used, preds, actuals)):
        print(f"  {tt}  pred={p:7.1f}  actual={a:7.1f}  err={p-a:+6.1f}")


if __name__ == "__main__":
    main()
