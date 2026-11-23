"""Estimate the irreducible error floor of the t+5min temp prediction task.

Uses the 1-second export to measure:
  1. sensor noise sigma on the CV thermocouple (from 1-lag 1s differences in
     quiet windows — process moves ~0 in 1s, so diff variance ~ 2*sigma^2)
  2. the MAE contribution of that noise when scoring vs RAW truth
  3. the distribution of the TRUE 5-min process change (what any model must
     predict) — its scale bounds achievable MAE for a given skill level

Read-only on data/ccr_clean_1s.csv; prints a report, writes
models/noise_floor.json. Run: python -m twin.noise_floor
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

import config
from backend.analysis import CV

CSV = config.ROOT / "data" / "ccr_clean_1s.csv"
OUT = config.ROOT / "models" / "noise_floor.json"


def main() -> None:
    df = pd.read_csv(CSV, usecols=["Time", CV, "353RL130_TPH"],
                     dtype={CV: "float32", "353RL130_TPH": "float32"})
    df["ts"] = pd.to_datetime(df["Time"], format="%Y-%m-%d %H:%M:%S")
    df = df.dropna().sort_values("ts").reset_index(drop=True)
    df = df[df["353RL130_TPH"] > config.KILN_FEED_MIN_TPH].reset_index(drop=True)
    cv = df[CV]

    # 1-second differences; keep only contiguous pairs
    gap = df["ts"].diff().dt.total_seconds()
    d1 = cv.diff().where(gap.between(0.5, 1.5)).dropna()
    # robust sigma of the diff (MAD) -> sensor sigma; quiet-window assumption:
    # true process drift in 1s is << noise, so var(diff) ~ 2 sigma^2
    mad = float(np.median(np.abs(d1 - d1.median())))
    sigma_diff = 1.4826 * mad
    sigma_noise = sigma_diff / np.sqrt(2)
    mae_noise = sigma_noise * np.sqrt(2 / np.pi)     # E|N(0,sigma)|

    # true 5-min change stats (denoised by 60s means at both ends)
    m60 = cv.rolling(60, min_periods=30).mean()
    ok = gap.rolling(300, min_periods=300).max().le(2.0)
    d5 = (m60.shift(-300) - m60).where(ok).dropna()
    out = {
        "n_1s_rows": int(len(df)),
        "sensor_sigma_degC": round(float(sigma_noise), 3),
        "mae_floor_from_noise_alone": round(float(mae_noise), 3),
        "true_5min_change": {
            "mean_abs": round(float(d5.abs().mean()), 3),
            "p50_abs": round(float(d5.abs().median()), 3),
            "p90_abs": round(float(d5.abs().quantile(0.9)), 3),
            "std": round(float(d5.std()), 3),
        },
        "note": ("MAE vs raw truth >= noise floor + unexplained share of the "
                 "true 5-min change. mean_abs(true change) is what a "
                 "zero-skill (persistence-on-denoised) model would score."),
    }
    OUT.write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
