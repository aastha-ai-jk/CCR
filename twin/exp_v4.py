"""ISOLATED offline A/B: do 5-min-averaged features (v4) beat production v3?

Motivated by the 1s-data resolution study (models/pipeline_1s_results.json):
5-min-averaged inputs beat the 1-min recipe by ~3% — averaging denoises at the
5-min horizon. v4 = the exact v3 feature set + rolling 5-min means of every
continuous input (levers, disturbances, context temps) + a 15-min CV anchor.
All additions are live-computable from 1-min history at serving time.

Both arms: SAME production dataset (process_1min via twin.features._load, which
includes the live-regime rows), SAME rows, SAME chronological split, SAME
train_gbt recipe, trained on the smoothed delta and scored vs RAW truth.

Reads the DB read-only; writes ONLY models/exp_v4_results.json.
Run: python -m twin.exp_v4
"""
from __future__ import annotations

import json
import time

import lightgbm as lgb
import numpy as np
import pandas as pd

import config
from backend.analysis import CV, MV
from twin import features as F
from twin.train_gbt import _BASE_PARAMS, _EARLY_STOP, _NUM_ROUNDS

OUT = config.ROOT / "models" / "exp_v4_results.json"
TEST_HORIZONS = (5, 30)

# ---- v4 additions: 5-min-averaged inputs (denoised) + 15-min CV anchor -----
# calciner coal excluded: its roll5_mean already exists in the v3 set
_ROLL5_TAGS = [*[m for m in MV if m != F.CALCINER_COAL],
               *F.DV_STATE, *F._RAW_EXTRA]           # 7 + 3 + 2 = 12


_V4_ONLY = [f"{t}__roll5_mean" for t in _ROLL5_TAGS] + [f"{CV}__roll15_dev"]


def v3_columns() -> list[str]:
    """The pre-v4 production set — stable even after features.py adds v4."""
    return [c for c in F.feature_columns() if c not in _V4_ONLY]


def v4_columns() -> list[str]:
    return v3_columns() + _V4_ONLY


def add_v4(df: pd.DataFrame) -> pd.DataFrame:
    for t in _ROLL5_TAGS:
        df[f"{t}__roll5_mean"] = df[t].rolling(5, min_periods=2).mean()
    df[f"{CV}__roll15_dev"] = df[CV].rolling(15, min_periods=5).mean() - df[CV]
    return df


def _fit(x_tr, y_tr, x_va, y_va) -> lgb.Booster:
    params = {**_BASE_PARAMS, "objective": "l1"}
    dtr = lgb.Dataset(x_tr, label=y_tr)
    dva = lgb.Dataset(x_va, label=y_va, reference=dtr)
    return lgb.train(params, dtr, num_boost_round=_NUM_ROUNDS, valid_sets=[dva],
                     callbacks=[lgb.early_stopping(_EARLY_STOP, verbose=False),
                                lgb.log_evaluation(0)])


def _mae(y, p) -> float:
    return round(float(np.abs(np.asarray(p) - np.asarray(y)).mean()), 3)


def main() -> None:
    df = add_v4(F._load())                       # v3 engineering + v4 columns
    cols3, cols4 = v3_columns(), v4_columns()

    smooth = df[CV].rolling(F.SMOOTH_W, center=True).mean()
    y_raw, y_s = {}, {}
    for h in TEST_HORIZONS:
        dt_h = (df["ts"].shift(-h) - df["ts"]).dt.total_seconds()
        ok_h = dt_h.between(h * 60 - 60, h * 60 + 120)
        y_raw[h] = df[CV].shift(-h).where(ok_h)
        y_s[h] = smooth.shift(-h).where(ok_h)

    # identical rows for both arms: full v4 vector + all targets present
    valid = df[cols4].notna().all(axis=1) & df[CV].notna()
    for h in TEST_HORIZONS:
        valid &= y_raw[h].notna() & y_s[h].notna()
    df = df[valid].reset_index(drop=True)
    y_raw = {h: v[valid.values].reset_index(drop=True) for h, v in y_raw.items()}
    y_s = {h: v[valid.values].reset_index(drop=True) for h, v in y_s.items()}

    n = len(df)
    tr = slice(0, int(n * .70))
    va = slice(int(n * .70), int(n * .85))
    te = slice(int(n * .85), n)
    cv0 = df[CV].reset_index(drop=True)
    print(f"rows={n:,} (train {tr.stop:,} / val {va.stop - va.start:,} "
          f"/ test {n - te.start:,})  span {df['ts'].iloc[0]} -> {df['ts'].iloc[-1]}")

    out = {"generated_at": pd.Timestamp.now().isoformat()[:19], "rows": n,
           "test_rows": n - te.start, "arms": {}}
    for name, cols in (("v3_production", cols3), ("v4_roll5", cols4)):
        arm = {"features": len(cols)}
        for h in TEST_HORIZONS:
            d = y_s[h] - cv0                      # smoothed delta target
            t0 = time.time()
            m = _fit(df[cols].iloc[tr], d.iloc[tr], df[cols].iloc[va], d.iloc[va])
            pred = cv0.iloc[te].to_numpy() + m.predict(df[cols].iloc[te])
            arm[f"t+{h}"] = {"mae": _mae(y_raw[h].iloc[te], pred),
                             "best_iter": m.best_iteration,
                             "train_s": round(time.time() - t0, 1)}
            print(f"[{name}] t+{h}: MAE={arm[f't+{h}']['mae']} "
                  f"(iters {m.best_iteration}, {arm[f't+{h}']['train_s']}s)")
        out["arms"][name] = arm
    out["persistence"] = {f"t+{h}": {"mae": _mae(y_raw[h].iloc[te], cv0.iloc[te])}
                          for h in TEST_HORIZONS}
    for h in TEST_HORIZONS:
        print(f"[persistence] t+{h}: MAE={out['persistence'][f't+{h}']['mae']}")

    a5 = out["arms"]["v4_roll5"]["t+5"]["mae"]
    b5 = out["arms"]["v3_production"]["t+5"]["mae"]
    out["verdict"] = {"winner_t5": "v4" if a5 < b5 else "v3",
                      "delta_pct_t5": round(100 * (b5 - a5) / b5, 2)}
    OUT.write_text(json.dumps(out, indent=1))
    print(f"VERDICT: {out['verdict']}  -> {OUT}")


if __name__ == "__main__":
    main()
