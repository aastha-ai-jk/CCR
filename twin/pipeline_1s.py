"""ISOLATED experimental pipeline: train on the 1-second historian export.

Answers ONE question, as a controlled experiment: does native 1-second data
beat the current 1-minute pipeline at the same task (calciner temp at t+5 min)?

Two arms, both trained from the SAME source file (data/ccr_clean_1s.csv,
28.8M rows, Aug-2025 -> Aug-2026) over the SAME chronological windows, with the
SAME LightGBM recipe, evaluated on the SAME minute-marks:

  A. "gbt_1s"   — native 1-second features (seconds-scale momentum/rolling/rates)
  B. "current"  — the production module's approach: 1-min downsample + the
                  production feature engineering (twin.features._engineer is
                  imported as a PURE function; nothing in production is touched)

ISOLATION GUARANTEES: reads only the CSV; writes only
models/pipeline_1s_results.json (+ optional models/pipeline_1s.pkl).
Never touches process_1min, twin_gbt.pkl, or any running service.

Run:  python -m twin.pipeline_1s [--stride 3] [--sample-days N]
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

import config
from backend.analysis import CV, MV

ROOT = Path(config.ROOT)
CSV_1S = ROOT / "data" / "ccr_clean_1s.csv"
OUT = ROOT / "models" / "pipeline_1s_results.json"

H_SEC = 300                       # the task: temp at t+5 min
FEED_TAG = config.KILN_FEED_TAG
FEED_MIN = config.KILN_FEED_MIN_TPH
DV = ["433MD140_TQ", "423AM822_NO", "423FN010_IT"]
SAFETY = ["423AM823_CO_PPM", "423AM823_O2", "423AM822_CO_PPM", "423AM822_O2"]
DEV_TAGS = ["423CN250_TT_1", "423DU415_TT"]          # live-available only
RAW_EXTRA = ["443KH050_TT1", "423CN_TEMP"]
RUN_GROUPS = {"423AM823_RUN": ["423AM823_CO_PPM", "423AM823_O2"],
              "423AM822_RUN": ["423AM822_CO_PPM", "423AM822_O2"]}

GBT = dict(objective="l1", num_leaves=127, learning_rate=0.03,
           n_estimators=2000, min_child_samples=40, feature_fraction=0.8,
           bagging_fraction=0.8, bagging_freq=1, verbose=-1)


def _metrics(y: np.ndarray, p: np.ndarray) -> dict:
    err = p - y
    ss = float(((y - y.mean()) ** 2).sum())
    return {"mae": round(float(np.abs(err).mean()), 3),
            "rmse": round(float(np.sqrt((err ** 2).mean())), 3),
            "r2": round(float(1 - (err ** 2).sum() / ss), 4) if ss > 0 else None,
            "n": int(len(y))}


def _fit(X_tr, y_tr, X_va, y_va, patience: int = 40):
    import lightgbm as lgb
    m = lgb.LGBMRegressor(**GBT)
    m.fit(X_tr, y_tr, eval_set=[(X_va, y_va)],
          callbacks=[lgb.early_stopping(patience, verbose=False)])
    print(f"    best_iteration={m.best_iteration_}")
    return m


# ------------------------------------------------------------------ load ----
def load_1s(sample_days: int | None) -> pd.DataFrame:
    t0 = time.time()
    cols = pd.read_csv(CSV_1S, nrows=0).columns
    dmap: dict[str, str] = {c: "float32" for c in cols if c != "Time"}
    df = pd.read_csv(CSV_1S, dtype=dmap)
    df = df.rename(columns={"Time": "ts"})
    df["ts"] = pd.to_datetime(df["ts"], format="%Y-%m-%d %H:%M:%S", errors="coerce")
    df = (df.dropna(subset=["ts"]).sort_values("ts")
          .drop_duplicates(subset="ts", keep="last").reset_index(drop=True))
    if sample_days:
        df = df[df["ts"] >= df["ts"].max() - pd.Timedelta(days=sample_days)]
        df = df.reset_index(drop=True)
    df = df[df[FEED_TAG] > FEED_MIN].reset_index(drop=True)   # feed gate
    # analyser validity: RUN on & O2 below ambient, else NaN -> short ffill
    for run, tags in RUN_GROUPS.items():
        o2 = tags[1]
        ok = (df[run] >= 0.5) & (df[o2] < 15) & (df[o2] >= 0)
        for t in tags:
            df[t] = df[t].where(ok).ffill(limit=120)
    print(f"[1s] loaded {len(df):,} gated rows "
          f"({df['ts'].iloc[0]} -> {df['ts'].iloc[-1]}) in {time.time()-t0:.0f}s")
    return df


# ------------------------------------------------- arm A: native 1s features -
def engineer_1s(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    t0 = time.time()
    f: dict[str, pd.Series] = {}
    cv = df[CV]
    for s in (30, 60, 180, 300, 600):                 # momentum (seconds)
        f[f"mom{s}s"] = (cv.shift(s) - cv).astype("float32")
    r5 = cv.rolling(300, min_periods=60)
    f["roll300_dev"] = (r5.mean() - cv).astype("float32")
    f["roll300_std"] = r5.std().astype("float32")
    f["roll900_std"] = cv.rolling(900, min_periods=120).std().astype("float32")
    coal = df["467RL400_TPH"]
    for s in (60, 180, 300):
        f[f"coal_lag{s}s"] = coal.shift(s)
    f["coal_roll300"] = coal.rolling(300, min_periods=60).mean().astype("float32")
    for mv in MV:                                     # lever rates over 3 min
        f[f"{mv}_rate180s"] = (df[mv] - df[mv].shift(180)).astype("float32")
    for t in DEV_TAGS:
        f[f"{t}_dev"] = (df[t] - cv).astype("float32")
    hour = df["ts"].dt.hour + df["ts"].dt.minute / 60.0
    f["hod_sin"] = np.sin(2 * np.pi * hour / 24).astype("float32")
    f["hod_cos"] = np.cos(2 * np.pi * hour / 24).astype("float32")
    X = pd.concat([df[[*MV, *DV, *SAFETY, *RAW_EXTRA]], pd.DataFrame(f)], axis=1)
    print(f"[1s] engineered {X.shape[1]} features in {time.time()-t0:.0f}s")
    return X, list(X.columns)


# --------------------------------------------- arm B: current-module (1-min) -
def build_minute_arm(df: pd.DataFrame):
    """Downsample the SAME source to 1-min and run the PRODUCTION feature
    engineering (pure-function import; production state untouched)."""
    from twin import features as F
    m = df.set_index("ts").resample("1min").mean().reset_index()
    m = F._engineer(m)
    feats = F.feature_columns()
    return m, feats


# ------------------------------------------- arm C: 5-min downsample ---------
def build_5min_arm(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """5-min BACKWARD-looking bins (label/closed right => no intra-bin future
    leakage); anchor and target come from the 1s frame so the task is
    identical — only the feature resolution changes."""
    g = (df.set_index("ts").resample("5min", label="right", closed="right")
         .mean().reset_index())
    f: dict[str, pd.Series] = {}
    cv5 = g[CV]
    for k in (1, 2, 3, 6):                          # momentum at 5/10/15/30 min
        f[f"mom{k*5}m"] = cv5.shift(k) - cv5
    r6 = cv5.rolling(6, min_periods=2)
    f["roll30_dev"] = r6.mean() - cv5
    f["roll30_std"] = r6.std()
    f["roll90_std"] = cv5.rolling(18, min_periods=4).std()
    coal5 = g["467RL400_TPH"]
    for k in (1, 2, 3):
        f[f"coal_lag{k*5}m"] = coal5.shift(k)
    f["coal_roll30"] = coal5.rolling(6, min_periods=2).mean()
    for mv in MV:
        f[f"{mv}_rate5m"] = g[mv] - g[mv].shift(1)
    for t in DEV_TAGS:
        f[f"{t}_dev"] = g[t] - cv5
    hour = g["ts"].dt.hour + g["ts"].dt.minute / 60.0
    f["hod_sin"] = np.sin(2 * np.pi * hour / 24)
    f["hod_cos"] = np.cos(2 * np.pi * hour / 24)
    X = pd.concat([g[[*MV, *DV, *SAFETY, *RAW_EXTRA]], pd.DataFrame(f)], axis=1)
    return g, X


# ------------------------------------------------------------------- main ----
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stride", type=int, default=3,
                    help="train-row thinning for the 1s arm (eval unaffected)")
    ap.add_argument("--sample-days", type=int, default=None)
    ap.add_argument("--patience", type=int, default=40,
                    help="early-stopping patience (applied to BOTH arms)")
    args = ap.parse_args()

    df = load_1s(args.sample_days)

    # shared chronological cuts (identical wall-clock for both arms)
    t_train_end = df["ts"].iloc[int(len(df) * .70)]
    t_val_end = df["ts"].iloc[int(len(df) * .85)]

    # contiguity + forward-target validity on the 1s grid
    step = df["ts"].diff().dt.total_seconds()
    contig = step.rolling(900, min_periods=900).max().le(2.0).fillna(False)
    dt_fwd = (df["ts"].shift(-H_SEC) - df["ts"]).dt.total_seconds()
    fwd_ok = dt_fwd.between(H_SEC - 5, H_SEC + 30)
    y_raw = df[CV].shift(-H_SEC).where(fwd_ok)
    y_smooth = (df[CV].rolling(180, center=True, min_periods=60).mean()
                .shift(-H_SEC).where(fwd_ok))

    X1, feats1 = engineer_1s(df)
    ok1 = X1.notna().all(axis=1) & contig & y_smooth.notna() & y_raw.notna()

    # evaluation grid: minute marks in the test region, valid in the 1s arm
    is_min = df["ts"].dt.second == 0
    ev1 = np.flatnonzero(ok1 & is_min & (df["ts"] > t_val_end))
    ev_ts = set(df["ts"].iloc[ev1].dt.floor("min"))

    tr1 = np.flatnonzero(ok1 & (df["ts"] <= t_train_end))[:: args.stride]
    va1 = np.flatnonzero(ok1 & (df["ts"] > t_train_end)
                         & (df["ts"] <= t_val_end))[:: args.stride]
    cv0_1 = df[CV].to_numpy()

    print(f"[1s] train={len(tr1):,} (stride {args.stride}) val={len(va1):,} "
          f"eval-minutes={len(ev1):,}")
    t0 = time.time()
    m1 = _fit(X1.iloc[tr1], (y_smooth - df[CV]).iloc[tr1],
              X1.iloc[va1], (y_smooth - df[CV]).iloc[va1], args.patience)
    t_1s = time.time() - t0
    pred1 = cv0_1[ev1] + m1.predict(X1.iloc[ev1])
    truth = y_raw.iloc[ev1].to_numpy()
    persist = cv0_1[ev1]

    # ---- arm B: current module on 1-min downsample of the SAME data --------
    mdf, featsB = build_minute_arm(df)
    dtf = (mdf["ts"].shift(-5) - mdf["ts"]).dt.total_seconds()
    okf = dtf.between(240, 420)
    yB_raw = mdf[CV].shift(-5).where(okf)
    yB_s = mdf[CV].rolling(3, center=True).mean().shift(-5).where(okf)
    okB = mdf[featsB].notna().all(axis=1) & yB_s.notna() & yB_raw.notna()
    evB = np.flatnonzero(okB & mdf["ts"].isin(ev_ts))
    trB = np.flatnonzero(okB & (mdf["ts"] <= t_train_end))
    vaB = np.flatnonzero(okB & (mdf["ts"] > t_train_end) & (mdf["ts"] <= t_val_end))

    # STRICT identical eval minutes across arms
    common = sorted(set(mdf["ts"].iloc[evB]) & set(df["ts"].iloc[ev1].dt.floor("min")))
    selB = mdf["ts"].isin(common)
    sel1 = df["ts"].dt.floor("min").isin(common) & is_min & pd.Series(ok1)
    evB = np.flatnonzero(pd.Series(okB) & selB)
    ev1 = np.flatnonzero(sel1)
    assert len(evB) == len(ev1) > 500, f"eval mismatch {len(evB)} vs {len(ev1)}"

    pred1 = cv0_1[ev1] + m1.predict(X1.iloc[ev1])
    truth = y_raw.iloc[ev1].to_numpy()
    persist = cv0_1[ev1]

    print(f"[1min] train={len(trB):,} val={len(vaB):,} eval={len(evB):,}")
    t0 = time.time()
    mB = _fit(mdf[featsB].iloc[trB], (yB_s - mdf[CV]).iloc[trB],
              mdf[featsB].iloc[vaB], (yB_s - mdf[CV]).iloc[vaB], args.patience)
    t_1m = time.time() - t0
    predB = mdf[CV].to_numpy()[evB] + mB.predict(mdf[featsB].iloc[evB])
    truthB = yB_raw.iloc[evB].to_numpy()

    # ---- arm C: 5-min resolution, instantaneous anchor, same 1s truth ------
    g, X5 = build_5min_arm(df)
    pos = pd.Series(np.arange(len(df)), index=df["ts"])
    i5 = pos.reindex(g["ts"]).to_numpy()            # 1s row at each 5-min mark
    has = ~np.isnan(i5)
    i5i = np.where(has, i5, 0).astype(int)
    anchor5 = np.where(has, cv0_1[i5i], np.nan)     # instantaneous CV at mark
    X5 = X5.assign(anchor_dev=anchor5 - g[CV])      # instant vs bin mean
    y5_s = np.where(has, y_smooth.to_numpy()[i5i], np.nan)
    y5_raw = np.where(has, y_raw.to_numpy()[i5i], np.nan)
    ok5 = (X5.notna().all(axis=1).to_numpy() & has
           & np.where(has, contig.to_numpy()[i5i], False)
           & ~np.isnan(y5_s) & ~np.isnan(y5_raw))
    ts5 = g["ts"]
    tr5 = np.flatnonzero(ok5 & (ts5 <= t_train_end))
    va5 = np.flatnonzero(ok5 & (ts5 > t_train_end) & (ts5 <= t_val_end))
    print(f"[5min] train={len(tr5):,} val={len(va5):,}")
    t0 = time.time()
    d5 = pd.Series(y5_s - anchor5)
    m5 = _fit(X5.iloc[tr5], d5.iloc[tr5], X5.iloc[va5], d5.iloc[va5],
              args.patience)
    t_5m = time.time() - t0

    # three-way eval: 5-min marks shared by ALL arms in the test region
    common3 = sorted(set(common) & set(ts5[ok5 & (ts5 > t_val_end)]))
    _c3 = set(common3)
    mark5 = np.array([t in _c3 for t in common])
    ev5 = np.flatnonzero(ok5 & ts5.isin(common3).to_numpy())
    assert len(ev5) == int(mark5.sum()) > 200, \
        f"3-way eval mismatch {len(ev5)} vs {mark5.sum()}"
    pred5 = anchor5[ev5] + m5.predict(X5.iloc[ev5])
    truth3 = truth[mark5]
    three_way = {
        "gbt_1s": _metrics(truth3, pred1[mark5]),
        "current_module_1min": _metrics(truth3, predB[mark5]),
        "gbt_5min": {**_metrics(truth3, pred5), "features": X5.shape[1],
                     "train_rows": int(len(tr5)), "train_time_s": round(t_5m, 1)},
        "persistence": _metrics(truth3, persist[mark5]),
    }

    out = {
        "generated_at": pd.Timestamp.now().isoformat()[:19],
        "source": CSV_1S.name,
        "span": [str(df['ts'].iloc[0])[:16], str(df['ts'].iloc[-1])[:16]],
        "rows_1s": int(len(df)), "task": f"{CV} @ t+{H_SEC}s",
        "identical_eval_minutes": int(len(ev1)),
        "arms": {
            "gbt_1s": {**_metrics(truth, pred1), "features": len(feats1),
                       "train_rows": int(len(tr1)), "train_time_s": round(t_1s, 1)},
            "current_module_1min": {**_metrics(truthB, predB), "features": len(featsB),
                                    "train_rows": int(len(trB)),
                                    "train_time_s": round(t_1m, 1),
                                    # same-yardstick score: vs the raw 1s truth
                                    "mae_vs_1s_truth": _metrics(truth, predB)["mae"]},
        },
        "persistence": _metrics(truth, persist),
        "three_way_on_5min_marks": three_way,
    }
    a = out["arms"]["gbt_1s"]["mae"]
    b = out["arms"]["current_module_1min"]["mae_vs_1s_truth"]   # same yardstick
    p = out["persistence"]["mae"]
    out["verdict"] = {"winner": "gbt_1s" if a < b else "current_module_1min",
                      "delta_pct": round(100 * (b - a) / b, 2),
                      "note": "both scored vs raw 1s truth on identical minutes; "
                              "positive delta_pct = 1s arm better"}
    OUT.write_text(json.dumps(out, indent=1))
    print(f"\n[compare] identical eval minutes: {len(ev1):,}")
    print(f"[compare] 1s-native      MAE={a}  ({out['arms']['gbt_1s']['train_time_s']}s)")
    print(f"[compare] current(1min)  MAE={b}  ({out['arms']['current_module_1min']['train_time_s']}s)")
    print(f"[compare] persistence    MAE={p}")
    print(f"[compare] 3-way on {len(ev5):,} shared 5-min marks: "
          f"1s={three_way['gbt_1s']['mae']}  "
          f"1min={three_way['current_module_1min']['mae']}  "
          f"5min={three_way['gbt_5min']['mae']}  "
          f"persist={three_way['persistence']['mae']}")
    print(f"[compare] VERDICT: {out['verdict']}  -> {OUT}")


if __name__ == "__main__":
    main()
