"""Feature engineering for the GBT/NN digital-twin core.

Builds a dead-time-aligned supervised dataset from ``process_1min`` (feed_gate==1
only), predicting calciner outlet temperature, combustion-safety signals and lab
quality at ``t + HORIZON_MIN``. Domain constants come from ``backend.analysis`` /
``config`` — never redefined here.

Design note — regime invariance: the calciner runs in distinct temperature
states (bands of ~880-940 degC). Absolute temperatures encode the *state* and do
not transfer to an unseen held-out regime, so temperature history is expressed as
*momentum* (deviation from the current CV) and the temperature target is trained
as a 5-min *change* (see ``train_gbt``). Physical, level-free disturbances
(torque, NOx, fan current), the actuators and the safety readings stay absolute.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

from backend import db
from backend.analysis import CV, DELTA, MV, SAFETY

HORIZON_MIN = DELTA  # == analysis.DELTA (5 min calciner thermal dead-time)
# v3: the twin predicts a TRAJECTORY — heads at each of these horizons (min).
HORIZONS: tuple[int, ...] = (5, 10, 15, 30)

# --- named process columns used as features -------------------------------
CALCINER_COAL = "467RL400_TPH"          # strongest lever; gets its own lags/rolls
DV_STATE = ["433MD140_TQ", "423AM822_NO", "423FN010_IT"]  # level-free disturbances
# analyser group -> (RUN gate tag, member CO/O2 tags, O2 tag for sanity mask)
_SAFETY_GROUPS = {
    "823": ("423AM823_RUN", ["423AM823_CO_PPM", "423AM823_O2"], "423AM823_O2"),
    "822": ("423AM822_RUN", ["423AM822_CO_PPM", "423AM822_O2"], "423AM822_O2"),
}
_LAG_MIN = (1, 3, 5)                     # short lags for CV momentum + calciner coal
_ROLL_WIN = 5                           # 5-min rolling window
_RATE_MIN = 3                           # rate-of-change span per lever
# --- v2 enrichment (promoted from the Model Bench winner, 2026-08-04) -------
_EXTRA_MOM = (10, 15, 30)               # longer CV-momentum windows (min)
# Hot-side temps as DEVIATION from CV. ONLY tags that exist on the LIVE OPC
# server may be used: 423CN150_TT_1 / 423KR405_TT_3 / 423KR405_TT_4 exist in
# the historical export but are dead on the server (BadNodeIdUnknown), so any
# feature built on them makes every live row invalid for training and freezes
# to a median at serving. (Re-add if the plant ever restores those sensors.)
_DEV_TAGS = ["423CN250_TT_1", "423DU415_TT"]
_RAW_EXTRA = ["443KH050_TT1", "423CN_TEMP"]     # secondary air + PH outlet temp
# --- v4 enrichment (validated by the 1s/1min/5min resolution study + the
# exp_v4 A/B, 2026-08-27): 5-min-averaged (denoised) copies of every
# continuous input + a 15-min CV anchor. Averaging removes sensor noise the
# 5-min-horizon physics never responds to. Calciner coal is excluded — its
# roll5_mean already exists in the v1 coal block above.
_ROLL5_TAGS: list[str] = [*[m for m in MV if m != CALCINER_COAL],
                          *DV_STATE, *_RAW_EXTRA]           # 7 + 3 + 2 = 12
# --- v7 enrichment (opt_bench, 2026-08-27: xfeat 6.10 / xfeat+blend 5.99 vs
# blend-alone 6.30 on identical data): long CV memory, EWM smoothers, lever
# EWMs + 10-min rates, physical interactions. Served from REAL live history
# when available (_live_history), linearised/steady-state otherwise.
_X_MOM = (2, 4, 7, 20, 45, 60)          # extra CV momentum windows (min)
_X_EWM = (3, 10, 30)                    # CV EWM deviation spans (min)
_X_COAL_LAGS = (2, 7, 10)               # extra calciner-coal lags (min)
_HIST_MIN = 66                          # minutes of history needed for exact serving
SMOOTH_W = 3                            # training target: 3-min centred mean at t+Δ
# live tags the serving layer should pass through in `conditions` so the
# deviation features use real values instead of medians
SERVE_CONTEXT_TAGS: list[str] = [*_DEV_TAGS, *_RAW_EXTRA]
_O2_MAX = 15.0                          # analyser reading above this = purge/invalid
_GAP_LO, _GAP_HI = 4 * 60, 7 * 60       # forward-gap guard (s), like analysis._matrix
_DOC_LO, _DOC_HI = 85.0, 100.0          # valid hot-meal degree-of-calcination window
_FL_LO, _FL_HI = 0.2, 5.0               # valid clinker free-lime window (%)
_LAB_TOL = pd.Timedelta("90min")        # nearest-lab join tolerance


# --------------------------------------------------------------------------- #
# Feature schema
# --------------------------------------------------------------------------- #
def feature_columns() -> list[str]:
    """Ordered feature names — the exact column order every model consumes."""
    cols: list[str] = list(MV)                       # 8 manipulated variables
    cols += list(DV_STATE)                            # level-free disturbances
    cols += list(SAFETY)                             # masked CO / O2 readings
    cols += [f"{CV}__mom{n}" for n in _LAG_MIN]      # CV momentum (regime-invariant)
    cols += [f"{CV}__roll{_ROLL_WIN}_dev", f"{CV}__roll{_ROLL_WIN}_std"]
    cols += [f"{CALCINER_COAL}__lag{n}" for n in _LAG_MIN]
    cols += [f"{CALCINER_COAL}__roll{_ROLL_WIN}_mean",
             f"{CALCINER_COAL}__roll{_ROLL_WIN}_std"]
    cols += [f"{mv}__rate{_RATE_MIN}" for mv in MV]  # lever rate-of-change
    # v2 enrichment
    cols += [f"{CV}__mom{n}" for n in _EXTRA_MOM]
    cols += [f"{CV}__roll15_std"]
    cols += [f"{t}__devCV" for t in _DEV_TAGS]
    cols += list(_RAW_EXTRA)
    cols += ["hod_sin", "hod_cos"]
    # v4 enrichment (appended so earlier bundles' column order is unchanged)
    cols += [f"{t}__roll5_mean" for t in _ROLL5_TAGS]
    cols += [f"{CV}__roll15_dev"]
    # v7 enrichment (appended, same rule)
    cols += [f"{CV}__mom{n}" for n in _X_MOM]
    cols += [f"{CV}__ewm{s}_dev" for s in _X_EWM]
    cols += [f"{CV}__roll30_std", f"{CV}__mom_accel"]
    cols += [f"{CALCINER_COAL}__lag{n}" for n in _X_COAL_LAGS]
    cols += [f"{CALCINER_COAL}__ewm10"]
    cols += [f"{mv}__ewm10" for mv in MV if mv != CALCINER_COAL]
    cols += [f"{mv}__rate10" for mv in MV]
    cols += ["coal_x_o2", "coal_x_feed", "afr_share"]
    return cols


def _raw_columns() -> list[str]:
    """DB columns needed to engineer features (deduplicated, order-preserving)."""
    need = [CV, *MV, *DV_STATE, *_DEV_TAGS, *_RAW_EXTRA]
    for run_tag, tags, o2_tag in _SAFETY_GROUPS.values():
        need += [run_tag, o2_tag, *tags]
    return list(dict.fromkeys(need))


# --------------------------------------------------------------------------- #
# Engineering
# --------------------------------------------------------------------------- #
def _engineer(df: pd.DataFrame) -> pd.DataFrame:
    """Add every feature column to a ts-sorted, numeric ``process_1min`` frame."""
    # analyser-valid mask: RUN on AND O2 in-range; invalid -> NaN then ffill
    for run_tag, tags, o2_tag in _SAFETY_GROUPS.values():
        valid = (df[run_tag] >= 0.5) & (df[o2_tag] < _O2_MAX) & (df[o2_tag] >= 0)
        for t in tags:
            df[t] = df[t].where(valid).ffill()

    roll = df[CV].rolling(_ROLL_WIN, min_periods=2)
    for n in _LAG_MIN:                                # CV history as momentum
        df[f"{CV}__mom{n}"] = df[CV].shift(n) - df[CV]
    df[f"{CV}__roll{_ROLL_WIN}_dev"] = roll.mean() - df[CV]
    df[f"{CV}__roll{_ROLL_WIN}_std"] = roll.std()

    for n in _LAG_MIN:                                # calciner coal (absolute lever)
        df[f"{CALCINER_COAL}__lag{n}"] = df[CALCINER_COAL].shift(n)
    coal_roll = df[CALCINER_COAL].rolling(_ROLL_WIN, min_periods=2)
    df[f"{CALCINER_COAL}__roll{_ROLL_WIN}_mean"] = coal_roll.mean()
    df[f"{CALCINER_COAL}__roll{_ROLL_WIN}_std"] = coal_roll.std()

    for mv in MV:
        df[f"{mv}__rate{_RATE_MIN}"] = df[mv].diff(_RATE_MIN)

    # v2 enrichment: long momentum, 15-min volatility, hot-side deviations,
    # time-of-day encoding (all regime-safe by construction)
    for n in _EXTRA_MOM:
        df[f"{CV}__mom{n}"] = df[CV].shift(n) - df[CV]
    df[f"{CV}__roll15_std"] = df[CV].rolling(15, min_periods=5).std()
    for t in _DEV_TAGS:
        df[f"{t}__devCV"] = df[t] - df[CV]
    hour = df["ts"].dt.hour + df["ts"].dt.minute / 60.0
    df["hod_sin"] = np.sin(2 * np.pi * hour / 24)
    df["hod_cos"] = np.cos(2 * np.pi * hour / 24)

    # v4 enrichment: denoised 5-min means of continuous inputs + 15-min anchor
    for t in _ROLL5_TAGS:
        df[f"{t}__roll5_mean"] = df[t].rolling(_ROLL_WIN, min_periods=2).mean()
    df[f"{CV}__roll15_dev"] = df[CV].rolling(15, min_periods=5).mean() - df[CV]

    # v7 enrichment: long memory, EWM smoothers, lever dynamics, interactions
    cv = df[CV]
    for n in _X_MOM:
        df[f"{CV}__mom{n}"] = cv.shift(n) - cv
    for s in _X_EWM:
        df[f"{CV}__ewm{s}_dev"] = cv.ewm(span=s, min_periods=2).mean() - cv
    df[f"{CV}__roll30_std"] = cv.rolling(30, min_periods=10).std()
    df[f"{CV}__mom_accel"] = (cv.shift(1) - cv) - (cv.shift(2) - cv.shift(1))
    coal = df[CALCINER_COAL]
    for n in _X_COAL_LAGS:
        df[f"{CALCINER_COAL}__lag{n}"] = coal.shift(n)
    df[f"{CALCINER_COAL}__ewm10"] = coal.ewm(span=10, min_periods=2).mean()
    for mv in MV:
        if mv != CALCINER_COAL:
            df[f"{mv}__ewm10"] = df[mv].ewm(span=10, min_periods=2).mean()
        df[f"{mv}__rate10"] = df[mv].diff(10)
    df["coal_x_o2"] = coal * df["423AM823_O2"]
    df["coal_x_feed"] = coal / df["353RL130_TPH"].clip(lower=1)
    df["afr_share"] = df["LAFR_FT"] / (df["LAFR_FT"] + coal * 10 + 1e-6)
    return df


def _load() -> pd.DataFrame:
    """Query in-load rows, coerce numeric, sort by ts, engineer features."""
    cols = ", ".join(f'"{c}"' for c in _raw_columns())
    df = db.q(f"SELECT ts, {cols} FROM process_1min WHERE feed_gate=1 ORDER BY ts")
    df["ts"] = pd.to_datetime(df["ts"], format="mixed")
    for c in _raw_columns():
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # Levers hold their last setpoint until moved — forward-fill gaps in the
    # manipulated variables (e.g. K3V10_F1 was uncaptured by the live feed for
    # a month due to a tag-name typo; rates correctly read 0 across the fill).
    for mv in MV:
        df[mv] = df[mv].ffill()
    return _engineer(df)


def _quality_targets(ts: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Nearest-in-time lab join: clinker free-lime and hot-meal degree-of-calc."""
    base = pd.DataFrame({"ts": pd.to_datetime(ts).values}).reset_index(drop=True)

    def _asof(lab: pd.DataFrame, col: str) -> pd.Series:
        lab = lab.dropna(subset=[col]).sort_values("ts")
        merged = pd.merge_asof(base, lab[["ts", col]], on="ts",
                               direction="nearest", tolerance=_LAB_TOL)
        return merged[col]

    cl = db.q("SELECT ts, free_lime FROM lab_clinker")
    cl["ts"] = pd.to_datetime(cl["ts"])
    cl["free_lime"] = pd.to_numeric(cl["free_lime"], errors="coerce")
    cl.loc[~cl["free_lime"].between(_FL_LO, _FL_HI), "free_lime"] = np.nan
    free_lime = _asof(cl, "free_lime")

    hm = db.q("SELECT ts, doc FROM lab_hotmeal")
    hm["ts"] = pd.to_datetime(hm["ts"])
    hm["doc"] = pd.to_numeric(hm["doc"], errors="coerce")
    hm.loc[~hm["doc"].between(_DOC_LO, _DOC_HI), "doc"] = np.nan
    hm = hm.groupby("ts", as_index=False)["doc"].mean()   # average kiln sides
    doc = _asof(hm, "doc")
    return free_lime, doc


def _chronological_split(n: int) -> pd.Series:
    """70 / 15 / 15 split by row position (data is already time-ordered)."""
    labels = np.full(n, "train", dtype=object)
    labels[int(n * 0.70):int(n * 0.85)] = "val"
    labels[int(n * 0.85):] = "test"
    return pd.Series(labels)


def build_training_data(sample: int | None = None) -> dict:
    """Assemble the dead-time-aligned supervised dataset (see CONTRACT.md).

    Extra key ``cv0`` (current CV) is returned so temperature models can be
    trained/served on the regime-invariant 5-min change and reconstruct the
    absolute value as ``cv0 + delta``.
    """
    df = _load()
    feats = feature_columns()

    # dead-time-aligned targets: value at t+HORIZON, gap guarded to 4-7 min
    y: dict[str, pd.Series] = {}
    smooth_cv = df[CV].rolling(SMOOTH_W, center=True).mean()
    for h in HORIZONS:
        # per-horizon forward-gap guard: the row h steps ahead must really be
        # ~h minutes ahead (rows are 1-min; tolerate one missing minute)
        dt_h = (df["ts"].shift(-h) - df["ts"]).dt.total_seconds()
        ok_h = dt_h.between(h * 60 - 60, h * 60 + 120)
        y[f"temp_h{h}"] = df[CV].shift(-h).where(ok_h)              # raw truth
        y[f"temp_smooth_h{h}"] = smooth_cv.shift(-h).where(ok_h)    # training target
    # back-compat aliases for the primary 5-min horizon
    y["temp"] = y[f"temp_h{DELTA}"]
    y["temp_smooth"] = y[f"temp_smooth_h{DELTA}"]
    dt_fwd = (df["ts"].shift(-DELTA) - df["ts"]).dt.total_seconds()
    fwd_ok = dt_fwd.between(_GAP_LO, _GAP_HI)
    for tag in SAFETY:
        y[f"safety_{tag}"] = df[tag].shift(-DELTA).where(fwd_ok)

    # keep rows with a complete feature vector and a known current temp
    valid = df[feats].notna().all(axis=1) & df[CV].notna()
    df = df[valid].reset_index(drop=True)
    y = {k: v[valid.values].reset_index(drop=True) for k, v in y.items()}

    free_lime, doc = _quality_targets(df["ts"])
    y["quality_free_lime"] = free_lime.reset_index(drop=True)
    y["quality_doc"] = doc.reset_index(drop=True)

    # optional decimation preserving full chronological span (features precomputed)
    if sample is not None and len(df) > sample:
        idx = np.unique(np.linspace(0, len(df) - 1, sample).astype(int))
        df = df.iloc[idx].reset_index(drop=True)
        y = {k: v.iloc[idx].reset_index(drop=True) for k, v in y.items()}

    return {
        "X": df[feats].reset_index(drop=True),
        "y": y,
        "ts": df["ts"].reset_index(drop=True),
        "cv0": df[CV].reset_index(drop=True),
        "split": _chronological_split(len(df)),
    }


# --------------------------------------------------------------------------- #
# Single-row serving
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _defaults() -> dict[str, float]:
    """Recent-median default for every feature column (serving fallback)."""
    cols = ", ".join(f'"{c}"' for c in _raw_columns())
    df = db.q(f"SELECT ts, {cols} FROM (SELECT * FROM process_1min "
              f"WHERE feed_gate=1 ORDER BY ts DESC LIMIT 30000) t ORDER BY ts")
    df["ts"] = pd.to_datetime(df["ts"])
    for c in _raw_columns():
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = _engineer(df)
    med = df[feature_columns()].median(numeric_only=True)
    out = {c: float(med.get(c, 0.0)) for c in feature_columns()}
    out["_cv"] = float(df[CV].median())              # current-temp fallback
    return out


_HIST_TTL_S = 15.0        # live-history cache TTL (shadow ticks every 30s)


def _live_history() -> pd.DataFrame | None:
    """Last ~80 min of live rows (process_live, 2s) aggregated to 1-min.

    Returns a raw-tag frame ready for ``_engineer`` or None when the live feed
    is unavailable/too short (e.g. laptop dev, feed outage). TTL-cached so
    concurrent what-if calls don't hammer the DB.
    """
    import time as _time
    cached = getattr(_live_history, "_cache", None)
    if cached and _time.time() - cached[0] < _HIST_TTL_S:
        return cached[1]
    out = None
    try:
        from datetime import datetime, timedelta
        cut = (datetime.now() - timedelta(minutes=85)).isoformat(timespec="seconds")
        cols = ", ".join(f'"{c}"' for c in _raw_columns())
        df = db.q(f'SELECT ts, {cols} FROM process_live WHERE ts > :cut '
                  "ORDER BY ts", {"cut": cut})
        if len(df) >= _HIST_MIN * 10:               # ~2s cadence sanity floor
            df["ts"] = pd.to_datetime(df["ts"], format="mixed")
            for c in _raw_columns():
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.set_index("ts").resample("1min").mean().reset_index()
            for mv in MV:
                df[mv] = df[mv].ffill()
            if len(df) >= _HIST_MIN and df[CV].notna().iloc[-1]:
                out = df
    except Exception:
        out = None
    _live_history._cache = (_time.time(), out)
    return out


def _row_from_history(hist: pd.DataFrame, levers: dict,
                      conditions: dict) -> pd.DataFrame | None:
    """EXACT feature row from real live history (v7 serving path).

    The requested levers overwrite the newest row before engineering, so a
    what-if reads as a just-applied step: raw values and rates move, lags and
    rolling context stay on the true recent past — matching how the model saw
    steps in training.
    """
    try:
        df = hist.copy()
        last = df.index[-1]
        for mv in MV:
            if (levers or {}).get(mv) is not None:
                df.loc[last, mv] = float(levers[mv])
        for tag in (*DV_STATE, *_DEV_TAGS, *_RAW_EXTRA):
            if (conditions or {}).get(tag) is not None:
                df.loc[last, tag] = float(conditions[tag])
        row = _engineer(df).iloc[-1]
        feats = feature_columns()
        if row[feats].isna().any():                 # gaps -> median fallback
            d = _defaults()
            row = row.copy()
            for c in feats:
                if pd.isna(row[c]):
                    row[c] = d[c]
        return pd.DataFrame([[float(row[c]) for c in feats]], columns=feats)
    except Exception:
        return None


def build_feature_row(levers: dict, conditions: dict,
                      use_live_history: bool = True) -> pd.DataFrame:
    """One feature row in exact ``feature_columns`` order.

    v7: preferred path builds from REAL live history (exact momentum/EWM/lag
    features). Fallback reconstructs temperature history from ``current_temp``
    and ``temp_trend`` (== CV(t) - CV(t-DELTA)) with steady-lever assumptions.

    ``use_live_history=False`` forces the fallback — REQUIRED for historical
    replays (e.g. the backtest walk): those describe a past moment, and mixing
    in the current plant's live history corrupts every prediction.
    """
    if use_live_history:
        hist = _live_history()
        if hist is not None:
            exact = _row_from_history(hist, levers, conditions)
            if exact is not None:
                return exact

    d = _defaults()
    row = {c: d[c] for c in feature_columns()}

    for mv in MV:                                    # levers
        if levers.get(mv) is not None:
            row[mv] = float(levers[mv])
    for tag in DV_STATE:                             # disturbance context
        if conditions.get(tag) is not None:
            row[tag] = float(conditions[tag])

    trend = float(conditions.get("temp_trend", 0.0) or 0.0)  # CV(t) - CV(t-DELTA)
    for n in _LAG_MIN:                               # CV(t-n) - CV(t) ~ -trend*n/DELTA
        row[f"{CV}__mom{n}"] = -trend * (n / DELTA)
    for n in _EXTRA_MOM:                             # same linearised momentum, v2
        row[f"{CV}__mom{n}"] = -trend * (n / DELTA)
    row[f"{CV}__roll{_ROLL_WIN}_dev"] = -trend / 2

    # v2 serving: hot-side deviations + raw context from live conditions when
    # provided (see SERVE_CONTEXT_TAGS); otherwise the recent-median default.
    cur = conditions.get("current_temp")
    for t in _DEV_TAGS:
        if conditions.get(t) is not None and cur is not None:
            row[f"{t}__devCV"] = float(conditions[t]) - float(cur)
    for t in _RAW_EXTRA:
        if conditions.get(t) is not None:
            row[t] = float(conditions[t])
    from datetime import datetime as _dt
    _h = _dt.now().hour + _dt.now().minute / 60.0
    row["hod_sin"] = float(np.sin(2 * np.pi * _h / 24))
    row["hod_cos"] = float(np.cos(2 * np.pi * _h / 24))

    coal = row[CALCINER_COAL]                         # assume steady coal history
    for n in _LAG_MIN:
        row[f"{CALCINER_COAL}__lag{n}"] = coal
    row[f"{CALCINER_COAL}__roll{_ROLL_WIN}_mean"] = coal
    for mv in MV:                                     # steady-state: no ramp
        row[f"{mv}__rate{_RATE_MIN}"] = 0.0

    # v4 serving: steady recent history -> the 5-min mean IS the current value
    # (row[t] already holds the live lever/condition or the median default);
    # 15-min anchor linearised from the trend like roll5_dev above.
    for t in _ROLL5_TAGS:
        row[f"{t}__roll5_mean"] = row[t]
    row[f"{CV}__roll15_dev"] = -trend * 1.5

    # v7 fallback serving: momentum/EWM linearised from the trend (an EWM of
    # span s lags a linear ramp by ~(s-1)/2 min); levers steady; interactions
    # recomputed from the row's own values. roll30_std keeps its median.
    for n in _X_MOM:
        row[f"{CV}__mom{n}"] = -trend * (n / DELTA)
    for s in _X_EWM:
        row[f"{CV}__ewm{s}_dev"] = -trend * (s - 1) / (2 * DELTA)
    row[f"{CV}__mom_accel"] = 0.0
    for n in _X_COAL_LAGS:
        row[f"{CALCINER_COAL}__lag{n}"] = coal
    row[f"{CALCINER_COAL}__ewm10"] = coal
    for mv in MV:
        if mv != CALCINER_COAL:
            row[f"{mv}__ewm10"] = row[mv]
        row[f"{mv}__rate10"] = 0.0
    row["coal_x_o2"] = coal * row["423AM823_O2"]
    row["coal_x_feed"] = coal / max(row["353RL130_TPH"], 1.0)
    row["afr_share"] = row["LAFR_FT"] / (row["LAFR_FT"] + coal * 10 + 1e-6)

    return pd.DataFrame([[row[c] for c in feature_columns()]], columns=feature_columns())
