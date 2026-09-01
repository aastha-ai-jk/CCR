"""Train the ML digital twin: LightGBM model of calciner-temperature CHANGE.

Predicts y = temp(t+DELTA) - temp(t) from EXACTLY the feature space
backend.analysis.simulate() already has on every call: current temperature,
its recent trend, the 3 PLC state-setting variables, and the 8 manipulated
variables (levers). Importing those names from backend.analysis (rather than
redefining them) is deliberate: it makes drift between what this script trains
on and what simulate() feeds the model at inference time impossible.

This is the "production twin (LightGBM ...) replaces the kNN core" step the
analysis.py module docstring calls out as the Phase 2 target -- trained here
on the 1-second-resolution CSV export (etl.load_csv_1s), which is more
current and finer-grained than the monthly PLC_DIR .xlsx history.

Run:  python -m etl.train_gbt_twin
Out:  backend/models/gbt_twin_dtemp.txt   (LightGBM booster)
      backend/models/gbt_twin_meta.json   (features, calibration, metrics)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lightgbm as lgb                      # noqa: E402
from sqlalchemy import create_engine        # noqa: E402

import config                               # noqa: E402
from backend.analysis import CV, MV, STATE_PLC, MV_TEMP, DELTA   # noqa: E402

MODELS = Path(__file__).resolve().parent.parent / "backend" / "models"
MODELS.mkdir(parents=True, exist_ok=True)

FEATURES = ["temp_now", "temp_trend"] + STATE_PLC + MV   # == 2 + 3 + 8 == 13
TEST_FRAC = 0.15          # last 15% of in-load history, by time -> held-out test
VALID_FRAC = 0.15         # the 15% before that -> validation (bias calibration)


def build_frame() -> pd.DataFrame:
    eng = create_engine(config.database_url())
    cols = list(dict.fromkeys([CV] + MV + STATE_PLC))
    sel = ", ".join(f'"{c}"' for c in cols)
    print("[train] reading process_1min (in-load only) ...")
    df = pd.read_sql(f'SELECT ts, {sel} FROM process_1min WHERE feed_gate=1 '
                     "ORDER BY ts", eng)
    df["ts"] = pd.to_datetime(df["ts"])
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.set_index("ts")

    # SAME confounder/target definitions as backend.analysis._matrix(), so the
    # trained model matches exactly what simulate() will feed it.
    temp = df[CV]
    df["temp_now"] = temp
    df["temp_trend"] = temp - temp.shift(DELTA)
    df["dt_past"] = (df.index.to_series() - df.index.to_series().shift(DELTA)).dt.total_seconds()
    df["y_dtemp"] = temp.shift(-DELTA) - temp
    df["dt_fwd"] = (df.index.to_series().shift(-DELTA) - df.index.to_series()).dt.total_seconds()
    df = df[df["dt_fwd"].between(4 * 60, 7 * 60) & df["dt_past"].between(4 * 60, 7 * 60)]

    df = df.dropna(subset=FEATURES + ["y_dtemp"])
    print(f"[train] usable rows: {len(df):,}   {df.index.min()} -> {df.index.max()}")
    return df


def monotone_vector() -> list[int]:
    """+1/-1/0 per feature from analysis.MV_TEMP (temp_now/trend/state unconstrained)."""
    vec = []
    for f in FEATURES:
        if f in MV_TEMP:
            vec.append(int(MV_TEMP[f][0]))
        else:
            vec.append(0)
    return vec


def evaluate(y, pred) -> dict:
    err = pred - y
    return {"mae": float(np.mean(np.abs(err))), "rmse": float(np.sqrt(np.mean(err ** 2))),
            "bias": float(np.mean(err))}


def main() -> None:
    df = build_frame()
    n = len(df)
    n_test = int(n * TEST_FRAC)
    n_valid = int(n * VALID_FRAC)
    te = df.iloc[n - n_test:]
    va = df.iloc[n - n_test - n_valid: n - n_test]
    tr = df.iloc[: n - n_test - n_valid]
    print(f"[train] TIME SPLIT  train={len(tr):,} ({tr.index.min().date()} -> "
          f"{tr.index.max().date()})  valid={len(va):,}  "
          f"test={len(te):,} ({te.index.min().date()} -> {te.index.max().date()})")
    if min(len(tr), len(va), len(te)) < 500:
        raise SystemExit("a split is too small -- not enough in-load history")

    Xtr, Xva, Xte = tr[FEATURES], va[FEATURES], te[FEATURES]
    ytr, yva, yte = tr["y_dtemp"], va["y_dtemp"], te["y_dtemp"]

    base = evaluate(yte.values, np.zeros(len(yte)))
    print(f"[train] baseline (persistence, dtemp=0)  MAE={base['mae']:.3f} degC  "
          f"RMSE={base['rmse']:.3f}")

    lo, hi = np.percentile(ytr, [1, 99])            # winsorize rare excursions
    ytr_c = np.clip(ytr, lo, hi)
    params = dict(objective="regression", metric="l1", learning_rate=0.05,
                  num_leaves=63, min_data_in_leaf=200, feature_fraction=0.9,
                  bagging_fraction=0.85, bagging_freq=1, lambda_l2=1.0,
                  monotone_constraints=monotone_vector(),
                  monotone_constraints_method="advanced",
                  verbosity=-1, seed=1, num_threads=0)
    dtr = lgb.Dataset(Xtr, ytr_c, feature_name=FEATURES)
    dva = lgb.Dataset(Xva, yva, feature_name=FEATURES, reference=dtr)
    bst = lgb.train(params, dtr, num_boost_round=1500, valid_sets=[dva],
                    callbacks=[lgb.early_stopping(60, verbose=False)])

    calib = float(np.mean(bst.predict(Xva) - yva.values))   # bias, fit on VALID only
    pred_te = bst.predict(Xte) - calib
    m = evaluate(yte.values, pred_te)
    gain = 100 * (1 - m["mae"] / base["mae"])
    print(f"[train] GBT twin  MAE={m['mae']:.3f} degC  RMSE={m['rmse']:.3f}  "
          f"bias={m['bias']:+.3f}  vs baseline: {gain:+.1f}%  "
          f"calib_offset={-calib:+.2f} degC  best_iter={bst.best_iteration}")

    # sanity check: does raising calciner coal raise predicted temperature?
    sample = Xte.sample(min(2000, len(Xte)), random_state=7).copy()
    lo_c, hi_c = sample["467RL400_TPH"].quantile([0.1, 0.9])
    s_lo, s_hi = sample.copy(), sample.copy()
    s_lo["467RL400_TPH"], s_hi["467RL400_TPH"] = lo_c, hi_c
    d_lo, d_hi = bst.predict(s_lo).mean(), bst.predict(s_hi).mean()
    print(f"[train] sanity: coal {lo_c:.1f}->{hi_c:.1f} t/h  =>  dtemp "
          f"{d_lo:+.2f}->{d_hi:+.2f} degC  "
          f"({'PHYSICAL (rises)' if d_hi > d_lo else '*** INVERTED ***'})")

    bst.save_model(str(MODELS / "gbt_twin_dtemp.txt"))
    (MODELS / "gbt_twin_meta.json").write_text(json.dumps({
        "features": FEATURES, "target": "temp(t+DELTA) - temp(t)",
        "delta_min": DELTA, "cv": CV, "mv": MV, "state_plc": STATE_PLC,
        "calibration_offset": round(-calib, 3),
        "test_metrics": m, "baseline_metrics": base,
        "improve_vs_baseline_pct": round(gain, 2),
        "trained_rows": len(tr), "test_period": [str(te.index.min()), str(te.index.max())],
    }, indent=2))
    print(f"\n[train] saved -> {MODELS / 'gbt_twin_dtemp.txt'}")
    print(f"[train] saved -> {MODELS / 'gbt_twin_meta.json'}")


if __name__ == "__main__":
    main()
