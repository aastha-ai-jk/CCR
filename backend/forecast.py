"""ML twin: calciner-temperature forecast, 60 s ahead.

This is the real trained model (validated held-out test MAE = 5.73 degC,
RMSE = 8.15, R2 = 0.69 -- see backend/models/gbt_forecast_meta.json) -- a
LightGBM regressor trained on 1-second-resolution PLC data (nn_twin/plc_pipeline.py,
a sandbox outside this app) using lag(1/5/10/60 s) and rolling-window
(5-min mean/std/skew + EMA) features of all 31 PLC tags, plus cyclical time
encodings. Predicts the ABSOLUTE calciner outlet temperature 60 seconds ahead.

Why this is a SEPARATE capability from backend.analysis.simulate():
simulate() answers a hypothetical "what if I set these levers to X" query, so
it only ever has the CURRENT instant to work from -- there is no real recent
trajectory for a lever setting that hasn't happened. This model needs the
opposite: real recent per-second history (lags, rolling stats) is exactly
what makes it accurate, so it can only run against REAL trailing data -- the
live feed (etl.live_feed -> process_live) or a historical backtest, not a
what-if. Trying to fake trajectory features for a hypothetical would silently
put the model outside the regime it was validated on.

Public entry points:
    forecast_from_recent(df) -> dict      # df: recent-history frame, >= ts + tags
    forecast_from_live_buffer(minutes_back=20) -> dict   # pulls backend.db process_live
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from backend import db

MODEL_PATH = Path(__file__).resolve().parent / "models" / "gbt_forecast_60s.txt"
META_PATH = Path(__file__).resolve().parent / "models" / "gbt_forecast_meta.json"

LAGS_S = (1, 5, 10, 60)
ROLL_WINDOW_S = 300              # 5-minute rolling window (matches training)
ROLL_MIN_PERIODS = 30            # == max(5, ROLL_WINDOW_S // 10), matches training
SAVGOL_WINDOW = 11               # matches training's denoise pass
SAVGOL_POLY = 2
MIN_BUFFER_S = 360               # >= ROLL_WINDOW_S + max(LAGS_S), with margin
CV = "423CI400_TT_4"


@lru_cache(maxsize=1)
def _model():
    import lightgbm as lgb
    bst = lgb.Booster(model_file=str(MODEL_PATH))
    meta = json.loads(META_PATH.read_text())
    return bst, meta


@lru_cache(maxsize=1)
def _base_tags() -> list[str]:
    bst, _ = _model()
    return sorted({f.split("__")[0] for f in bst.feature_name() if "__" in f})


@lru_cache(maxsize=1)
def _tag_medians() -> dict[str, float]:
    """Long-run median per tag from process_1min -- the fallback value used
    when a tag isn't present in the live buffer's schema (see module docstring:
    the live feed is missing 423CN150_TT_1 and K3V10_F1 in this deployment)."""
    tags = _base_tags()
    cols = ", ".join(f'"{t}"' for t in tags)
    df = db.q(f"SELECT {cols} FROM process_1min WHERE feed_gate=1")
    return {t: float(pd.to_numeric(df[t], errors="coerce").median()) for t in tags
            if t in df.columns}


def _prep_1s_grid(df: pd.DataFrame, tags: list[str]) -> pd.DataFrame | None:
    """Reindex to a complete 1-second grid + ffill/interpolate + Savitzky-Golay
    denoise -- exactly the preprocessing the model was trained on (see
    nn_twin/plc_pipeline.py's preprocess()), so a live buffer at ANY native
    cadence (this deployment's process_live is ~2 s) gets turned into the same
    representation the model saw during training."""
    from scipy.signal import savgol_filter

    df = df.sort_values("ts").drop_duplicates(subset="ts")
    if len(df) < 5:
        return None
    full = pd.date_range(df["ts"].min(), df["ts"].max(), freq="1s")
    if len(full) < MIN_BUFFER_S:
        return None
    g = df.set_index("ts")[tags].reindex(full)
    g.index.name = "ts"
    g = g.ffill().interpolate(method="linear", limit_direction="both")
    if g.isna().any().any():
        return None
    w = SAVGOL_WINDOW | 1
    if len(g) >= w:
        for c in tags:
            g[c] = savgol_filter(g[c].to_numpy(), w, SAVGOL_POLY)
    return g


def _feature_row(g: pd.DataFrame, feat_names: list[str]) -> np.ndarray:
    """Compute every model feature at the LAST row of `g` (the 'now' instant)."""
    last = g.index[-1]
    vals: dict[str, float] = {}
    for tag in g.columns:
        s = g[tag]
        for k in LAGS_S:
            vals[f"{tag}__lag{k}"] = float(s.iloc[-1 - k]) if len(s) > k else float(s.iloc[0])
        window = s.iloc[-ROLL_WINDOW_S:]
        vals[f"{tag}__rmean{ROLL_WINDOW_S}"] = float(window.mean())
        vals[f"{tag}__rstd{ROLL_WINDOW_S}"] = float(window.std())
        vals[f"{tag}__rskew{ROLL_WINDOW_S}"] = float(window.skew())
        # EWM is recursive from series start; using the available buffer is the
        # practical live approximation (see forecast_from_recent docstring).
        vals[f"{tag}__ema{ROLL_WINDOW_S}"] = float(
            s.ewm(span=ROLL_WINDOW_S, min_periods=ROLL_MIN_PERIODS).mean().iloc[-1])
    hour, dow, month = last.hour, last.dayofweek, last.month - 1
    vals["hour_sin"], vals["hour_cos"] = np.sin(2 * np.pi * hour / 24), np.cos(2 * np.pi * hour / 24)
    vals["dow_sin"], vals["dow_cos"] = np.sin(2 * np.pi * dow / 7), np.cos(2 * np.pi * dow / 7)
    vals["month_sin"], vals["month_cos"] = np.sin(2 * np.pi * month / 12), np.cos(2 * np.pi * month / 12)
    return np.array([[vals[f] for f in feat_names]], dtype=float)


def forecast_from_recent(df: pd.DataFrame) -> dict:
    """df: columns = 'ts' + as many of the 31 base tags as available, any
    native cadence, covering >= MIN_BUFFER_S seconds of REAL recent history
    (not a what-if -- see module docstring)."""
    bst, meta = _model()
    feat_names = bst.feature_name()
    tags = _base_tags()

    have = [t for t in tags if t in df.columns and df[t].notna().sum() >= 5]
    missing = [t for t in tags if t not in have]
    if missing:
        med = _tag_medians()
        df = df.copy()
        for t in missing:
            df[t] = med.get(t)   # constant fallback -> lag/mean/std=0 naturally for that tag

    g = _prep_1s_grid(df, tags)
    if g is None:
        return {"available": False,
                "reason": f"need >= {MIN_BUFFER_S}s of contiguous recent history "
                          f"(got {len(df)} raw rows)"}

    x = _feature_row(g, feat_names)
    pred_temp = float(bst.predict(x)[0])
    now_temp = float(g[CV].iloc[-1]) if CV in g.columns else None

    return {
        "available": True,
        "predicted_temp_60s": round(pred_temp, 1),
        "current_temp": None if now_temp is None else round(now_temp, 1),
        "predicted_change": None if now_temp is None else round(pred_temp - now_temp, 1),
        "horizon_s": 60,
        "method": "gbt-lag-rolling",
        "model_test_mae": meta["test_metrics"]["mae"],
        "model_test_r2": meta["test_metrics"]["r2"],
        "as_of": str(g.index[-1]),
        "imputed_tags": missing,
    }


def forecast_from_live_buffer(minutes_back: int = 20) -> dict:
    """Pull the most recent `minutes_back` minutes from process_live (the
    near-real-time buffer etl.live_feed writes to) and forecast from it."""
    bst, _ = _model()
    tags = [t for t in _base_tags() if t not in ("423CN150_TT_1", "K3V10_F1")]
    cols = ", ".join(f'"{t}"' for t in tags)
    df = db.q(f'SELECT ts, {cols} FROM process_live ORDER BY ts DESC LIMIT :n',
             {"n": minutes_back * 60})
    if df.empty:
        return {"available": False, "reason": "process_live is empty -- "
                "is `python -m etl.live_feed` (or --replay) running?"}
    df["ts"] = pd.to_datetime(df["ts"])
    return forecast_from_recent(df)
