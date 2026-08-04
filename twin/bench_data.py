"""Shared dataset for the 3-approach model bake-off ("Model Bench").

Single source of truth so every approach — A) GBT tabular, B) GRU sequence,
C) ARX system-ID — trains and tests on IDENTICAL rows from the NEW cleaned
historian export (``data/merged_kiln3_cleaned.csv``, ~405k 1-min rows,
Aug 2025 – Jul 2026, outliers removed upstream).

Reuses the production twin's feature engineering (``twin.features``) and the
domain constants from ``backend.analysis`` so approach A is a faithful retrain
of the served twin on the new data.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

import config
from backend.analysis import CV, DELTA, MV, STATE_PLC
from twin import features as F

CSV_PATH = Path(config.ROOT) / "data" / "merged_kiln3_cleaned.csv"

# Sequence channels for the GRU (mirrors twin/train_nn.py): 8 MVs + 3 states + CV.
SEQ_COLS: list[str] = [*MV, *STATE_PLC, CV]

_GAP_LO, _GAP_HI = 4 * 60, 7 * 60  # forward-gap guard (s), like the twin

# ---- enriched feature set (GBT v2) — all regime-safe by construction --------
_EXTRA_MOM = (10, 15, 30)          # longer temperature-momentum windows (min)
_DEV_TAGS = ["423CN150_TT_1", "423CN250_TT_1", "423KR405_TT_3",
             "423KR405_TT_4", "423DU415_TT"]   # hot-side temps as DEVIATION from CV
_RAW_EXTRA = ["443KH050_TT1", "423CN_TEMP"]     # secondary air + PH outlet temp
_SMOOTH_W = 3                      # smoothed target: 3-min centred mean at t+Δ


def extended_columns() -> list[str]:
    """Production feature set + the v2 enrichment columns (order fixed)."""
    return (F.feature_columns()
            + [f"{CV}__mom{n}" for n in _EXTRA_MOM]
            + [f"{CV}__roll15_std"]
            + [f"{t}__devCV" for t in _DEV_TAGS]
            + list(_RAW_EXTRA)
            + ["hod_sin", "hod_cos"])


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Add the v2 columns: long momentum, hot-side deviations, time-of-day."""
    for n in _EXTRA_MOM:
        df[f"{CV}__mom{n}"] = df[CV].shift(n) - df[CV]
    df[f"{CV}__roll15_std"] = df[CV].rolling(15, min_periods=5).std()
    for t in _DEV_TAGS:
        df[f"{t}__devCV"] = df[t] - df[CV]
    hour = df["ts"].dt.hour + df["ts"].dt.minute / 60.0
    df["hod_sin"] = np.sin(2 * np.pi * hour / 24)
    df["hod_cos"] = np.cos(2 * np.pi * hour / 24)
    return df


def _load_engineered() -> pd.DataFrame:
    """CSV -> ts-sorted, feed-gated, analyser-masked, feature-engineered frame."""
    df = pd.read_csv(CSV_PATH)
    df = df.rename(columns={"Time": "ts"}).drop(columns=["source_file"], errors="ignore")
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df = df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    for c in df.columns:
        if c != "ts":
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # feed gate (control actions only apply above this — same rule as the twin)
    df = df[df[config.KILN_FEED_TAG] > config.KILN_FEED_MIN_TPH].reset_index(drop=True)

    # analyser-valid masking + lags/rolls/rates from the production feature
    # module, then the v2 enrichment columns
    return _enrich(F._engineer(df))


def build(sample: int | None = None) -> dict:
    """Assemble the bake-off dataset.

    Returns a dict with:
      df       engineered frame (all columns, ts-indexed rows kept in order)
      X_tab    tabular feature matrix (twin feature_columns) for A and C
      y        absolute temp at t+DELTA (gap-guarded)   cv0  current temp
      split    'train'/'val'/'test' labels (chronological 70/15/15)
      valid_tab  boolean mask: complete tabular features + known y + known cv0
      meta     dataset descriptors for the report
    """
    df = _load_engineered()

    dt_fwd = (df["ts"].shift(-DELTA) - df["ts"]).dt.total_seconds()
    fwd_ok = dt_fwd.between(_GAP_LO, _GAP_HI)
    y = df[CV].shift(-DELTA).where(fwd_ok)
    # noise-reduced target: 3-min centred mean of CV around t+Δ (scored vs raw y)
    y_smooth = (df[CV].rolling(_SMOOTH_W, center=True).mean()
                .shift(-DELTA).where(fwd_ok))

    feats = F.feature_columns()
    ext = extended_columns()
    valid_tab = df[feats].notna().all(axis=1) & df[CV].notna() & y.notna()
    valid_ext = valid_tab & df[ext].notna().all(axis=1)

    if sample is not None and len(df) > sample:
        # keep the most-recent contiguous block — decimation would destroy the
        # 1-min contiguity that sequence windows and ARX lags depend on
        df = df.tail(sample).reset_index(drop=True)
        y = y.tail(sample).reset_index(drop=True)
        y_smooth = y_smooth.tail(sample).reset_index(drop=True)
        valid_tab = valid_tab.tail(sample).reset_index(drop=True)
        valid_ext = valid_ext.tail(sample).reset_index(drop=True)

    n = len(df)
    split = pd.Series(np.where(np.arange(n) < int(n * .70), "train",
                      np.where(np.arange(n) < int(n * .85), "val", "test")))

    return {
        "df": df,
        "X_tab": df[feats],
        "X_ext": df[ext],
        "y": y,
        "y_smooth": y_smooth,
        "cv0": df[CV],
        "split": split,
        "valid_tab": valid_tab,
        "valid_ext": valid_ext,
        "feature_columns": feats,
        "extended_columns": ext,
        "meta": {
            "csv": CSV_PATH.name,
            "rows_after_gate": int(n),
            "span": [str(df["ts"].iloc[0]), str(df["ts"].iloc[-1])],
            "horizon_min": DELTA,
            "target": CV,
            "split_counts": split.value_counts().to_dict(),
        },
    }
