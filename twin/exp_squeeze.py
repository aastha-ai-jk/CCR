"""ISOLATED accuracy-squeeze experiment: how much t+5 MAE is left on the table?

Context: production v4 = 6.672 degC on its own test window. The 5.62 from the
resolution study was an easier window (persistence 7.21 there vs 7.95 here) —
not a better model. This experiment tests every remaining legitimate lever on
IDENTICAL production data/split, scored vs RAW truth:

  base        — current v4 recipe (reference)
  huber       — Huber objective instead of L1
  smooth5     — heavier training-target smoothing (5-min centred, scored vs raw)
  slow_lr     — lr 0.015, longer boosting
  arx         — velocity-form ridge ARX (transparent baseline)
  blend       — GBT + ARX, weight chosen on the validation fold

Reads the DB read-only; writes ONLY models/exp_squeeze_results.json.
Run: python -m twin.exp_squeeze
"""
from __future__ import annotations

import json
import time

import lightgbm as lgb
import numpy as np
import pandas as pd

import config
from backend.analysis import CV
from twin import features as F
from twin.train_gbt import _BASE_PARAMS, _EARLY_STOP, _NUM_ROUNDS

OUT = config.ROOT / "models" / "exp_squeeze_results.json"
H = 5


def _fit(x_tr, y_tr, x_va, y_va, **over) -> lgb.Booster:
    params = {**_BASE_PARAMS, "objective": "l1", **over}
    rounds = over.pop("_rounds", _NUM_ROUNDS)
    dtr = lgb.Dataset(x_tr, label=y_tr)
    dva = lgb.Dataset(x_va, label=y_va, reference=dtr)
    return lgb.train(params, dtr, num_boost_round=rounds, valid_sets=[dva],
                     callbacks=[lgb.early_stopping(_EARLY_STOP, verbose=False),
                                lgb.log_evaluation(0)])


def _mae(y, p) -> float:
    return round(float(np.abs(np.asarray(p) - np.asarray(y)).mean()), 3)


def main() -> None:
    df = F._load()
    feats = F.feature_columns()

    dt_h = (df["ts"].shift(-H) - df["ts"]).dt.total_seconds()
    ok_h = dt_h.between(H * 60 - 60, H * 60 + 120)
    y_raw = df[CV].shift(-H).where(ok_h)
    smooth3 = df[CV].rolling(3, center=True).mean().shift(-H).where(ok_h)
    smooth5 = df[CV].rolling(5, center=True, min_periods=3).mean().shift(-H).where(ok_h)

    valid = (df[feats].notna().all(axis=1) & df[CV].notna()
             & y_raw.notna() & smooth3.notna() & smooth5.notna())
    df = df[valid].reset_index(drop=True)
    y_raw = y_raw[valid.values].reset_index(drop=True)
    smooth3 = smooth3[valid.values].reset_index(drop=True)
    smooth5 = smooth5[valid.values].reset_index(drop=True)

    n = len(df)
    tr = slice(0, int(n * .70))
    va = slice(int(n * .70), int(n * .85))
    te = slice(int(n * .85), n)
    cv0 = df[CV].reset_index(drop=True)
    X = df[feats]
    truth = y_raw.iloc[te].to_numpy()
    base_anchor = cv0.iloc[te].to_numpy()
    print(f"rows={n:,}  test={n - te.start:,}  "
          f"persistence MAE={_mae(truth, base_anchor)}")

    results: dict[str, dict] = {}

    def run(name: str, target: pd.Series, **over) -> np.ndarray:
        d = target - cv0
        t0 = time.time()
        m = _fit(X.iloc[tr], d.iloc[tr], X.iloc[va], d.iloc[va], **over)
        pred = base_anchor + m.predict(X.iloc[te])
        results[name] = {"mae": _mae(truth, pred), "best_iter": m.best_iteration,
                         "train_s": round(time.time() - t0, 1)}
        print(f"[{name}] MAE={results[name]['mae']} (iters {m.best_iteration})")
        return pred

    pred_base = run("base", smooth3)
    run("huber", smooth3, objective="huber")
    run("smooth5", smooth5)
    run("slow_lr", smooth3, learning_rate=0.015, _rounds=4000)

    # velocity-form ridge ARX on the same features (linear, transparent)
    from sklearn.linear_model import Ridge
    t0 = time.time()
    arx = Ridge(alpha=10.0)
    d3 = (smooth3 - cv0)
    arx.fit(X.iloc[tr].to_numpy(), d3.iloc[tr].to_numpy())
    pred_arx = base_anchor + arx.predict(X.iloc[te].to_numpy())
    results["arx"] = {"mae": _mae(truth, pred_arx),
                      "train_s": round(time.time() - t0, 1)}
    print(f"[arx] MAE={results['arx']['mae']}")

    # blend: weight chosen on the VALIDATION fold, applied unseen to test
    ws = np.linspace(0, 0.5, 11)
    m_tmp = _fit(X.iloc[tr], d3.iloc[tr], X.iloc[va], d3.iloc[va])
    pv_gbt = cv0.iloc[va].to_numpy() + m_tmp.predict(X.iloc[va])
    pv_arx = cv0.iloc[va].to_numpy() + arx.predict(X.iloc[va].to_numpy())
    tv = y_raw.iloc[va].to_numpy()
    best_w = min(ws, key=lambda w: np.abs((1 - w) * pv_gbt + w * pv_arx - tv).mean())
    pred_blend = (1 - best_w) * pred_base + best_w * pred_arx
    results["blend"] = {"mae": _mae(truth, pred_blend), "w_arx": round(float(best_w), 2)}
    print(f"[blend] MAE={results['blend']['mae']} (w_arx={best_w:.2f})")

    results["persistence"] = {"mae": _mae(truth, base_anchor)}
    best = min((k for k in results if k != "persistence"),
               key=lambda k: results[k]["mae"])
    out = {"generated_at": pd.Timestamp.now().isoformat()[:19], "rows": n,
           "test_rows": n - te.start, "task": f"{CV} @ t+{H}min vs raw truth",
           "results": results,
           "verdict": {"best": best, "mae": results[best]["mae"],
                       "vs_base_pct": round(100 * (results['base']['mae']
                                            - results[best]['mae'])
                                            / results['base']['mae'], 2)}}
    OUT.write_text(json.dumps(out, indent=1))
    print(f"VERDICT: {out['verdict']}  -> {OUT}")


if __name__ == "__main__":
    main()
