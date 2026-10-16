"""ML twin: calciner-temperature forecast, 60 s ahead.

DELTA-TARGET model (nn_twin/plc_pipeline_delta.py, a sandbox outside this
app): predicts temperature CHANGE over 60s, not the absolute value, using
regime-invariant MOMENTUM features (deviation-from-now) for the target tag's
own history -- lag/rolling-mean/EMA become "how far is history from right
now", not an absolute level. Every OTHER tag (levers, disturbances, safety)
stays absolute, since their level is causally meaningful. Serving reconstructs
the absolute prediction as current_temp + predicted_delta.

Why: the PRIOR absolute-value model (still in git history as
gbt_forecast_60s.txt / gbt_forecast_meta.json) hit 5.69 degC on held-out test
but showed a same-direction, different-magnitude bias across two independent
real backtests (8.29 degC on a volatile morning vs 3.66 degC on a calm
afternoon) -- the classic signature of a model that implicitly encoded "what
operating regime was I trained in" via absolute temperature levels. The
delta-target design removes that regime leak (same fix already proven in a
colleague's feature/digital-twin-ml branch, which found absolute-target
models scored WORSE than persistence). Retrained test MAE: 2.53 degC.

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

MODEL_PATH = Path(__file__).resolve().parent / "models" / "gbt_delta_60s.txt"
META_PATH = Path(__file__).resolve().parent / "models" / "gbt_delta_meta.json"

LAGS_S = (1, 5, 10, 60)
ROLL_WINDOW_S = 300              # 5-minute rolling window (matches training)
ROLL_MIN_PERIODS = 30            # == max(5, ROLL_WINDOW_S // 10), matches training
# CAUSAL denoise window (seconds) -- must equal CFG.smooth_window in
# nn_twin/plc_pipeline.py. A trailing rolling mean only looks backward, so
# serving computes exactly what training scored.
SMOOTH_WINDOW = 11
MIN_BUFFER_S = 360               # >= ROLL_WINDOW_S + max(LAGS_S), with margin
CV = "423CI400_TT_4"
# The target tag's own lag/rolling-mean/EMA features are MOMENTUM (deviation
# from "now"), not absolute levels -- must match plc_pipeline_delta.py's
# MOM_STATS/momentum_tag exactly, or serving silently diverges from training.
MOMENTUM_TAG = CV
MOMENTUM_ROLL_STATS = ("rmean",)   # rstd/rskew stay absolute even for CV (level-free already)


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
    """Reindex to a complete 1-second grid + ffill/interpolate + CAUSAL denoise
    (trailing rolling mean) -- exactly the preprocessing the model was trained
    on (see nn_twin/plc_pipeline.py's preprocess()), so a live buffer at ANY
    native cadence (this deployment's process_live is ~2 s) gets turned into
    the same representation the model saw during training. Trailing-only means
    every value here is computable in real time -- no future samples used."""
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
    if SMOOTH_WINDOW > 1:
        for c in tags:
            g[c] = g[c].rolling(SMOOTH_WINDOW, min_periods=1).mean()
    return g


def _feature_row(g: pd.DataFrame, feat_names: list[str]) -> np.ndarray:
    """Compute every model feature at the LAST row of `g` (the 'now' instant).
    For MOMENTUM_TAG (the target's own history), lag/rolling-mean/EMA are
    DEVIATIONS from the current value -- must mirror plc_pipeline_delta.py's
    add_features_delta() exactly, or serving silently diverges from training."""
    last = g.index[-1]
    vals: dict[str, float] = {}
    for tag in g.columns:
        s = g[tag]
        cur = float(s.iloc[-1])
        is_mom = (tag == MOMENTUM_TAG)
        for k in LAGS_S:
            raw = float(s.iloc[-1 - k]) if len(s) > k else float(s.iloc[0])
            vals[f"{tag}__lag{k}"] = (raw - cur) if is_mom else raw
        window = s.iloc[-ROLL_WINDOW_S:]
        rmean = float(window.mean())
        vals[f"{tag}__rmean{ROLL_WINDOW_S}"] = (rmean - cur) if (is_mom and "rmean" in MOMENTUM_ROLL_STATS) else rmean
        vals[f"{tag}__rstd{ROLL_WINDOW_S}"] = float(window.std())     # always absolute (level-free already)
        vals[f"{tag}__rskew{ROLL_WINDOW_S}"] = float(window.skew())   # always absolute (level-free already)
        # EWM is recursive from series start; using the available buffer is the
        # practical live approximation (see forecast_from_recent docstring).
        ema = float(s.ewm(span=ROLL_WINDOW_S, min_periods=ROLL_MIN_PERIODS).mean().iloc[-1])
        vals[f"{tag}__ema{ROLL_WINDOW_S}"] = (ema - cur) if is_mom else ema
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
    now_temp = float(g[CV].iloc[-1]) if CV in g.columns else None
    pred_delta = float(bst.predict(x)[0])
    pred_temp = None if now_temp is None else now_temp + pred_delta

    m = meta["test_metrics_absolute_degC"]
    return {
        "available": True,
        "predicted_temp_60s": None if pred_temp is None else round(pred_temp, 1),
        "current_temp": None if now_temp is None else round(now_temp, 1),
        "predicted_change": round(pred_delta, 1),
        "horizon_s": 60,
        "method": "gbt-delta-momentum",
        "model_test_mae": m["mae"],
        "model_test_r2": m["r2"],
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
