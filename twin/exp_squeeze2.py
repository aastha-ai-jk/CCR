"""Squeeze round 2: push the round-1 winners (huber hit the round cap; ARX
nearly matched GBT). Same isolated setup as twin/exp_squeeze.py.

  huber_6k     — huber objective, 6000-round cap
  huber_slow   — huber, lr 0.015, 8000-round cap
  huber_arx    — best huber + ridge ARX, blend weight chosen on val

Writes ONLY models/exp_squeeze2_results.json.  Run: python -m twin.exp_squeeze2
"""
from __future__ import annotations

import json
import time

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

import config
from backend.analysis import CV
from twin import features as F
from twin.train_gbt import _BASE_PARAMS, _EARLY_STOP

OUT = config.ROOT / "models" / "exp_squeeze2_results.json"
H = 5


def _mae(y, p) -> float:
    return round(float(np.abs(np.asarray(p) - np.asarray(y)).mean()), 3)


def main() -> None:
    df = F._load()
    feats = F.feature_columns()
    dt_h = (df["ts"].shift(-H) - df["ts"]).dt.total_seconds()
    ok_h = dt_h.between(H * 60 - 60, H * 60 + 120)
    y_raw = df[CV].shift(-H).where(ok_h)
    smooth3 = df[CV].rolling(3, center=True).mean().shift(-H).where(ok_h)
    valid = (df[feats].notna().all(axis=1) & df[CV].notna()
             & y_raw.notna() & smooth3.notna())
    df = df[valid].reset_index(drop=True)
    y_raw = y_raw[valid.values].reset_index(drop=True)
    smooth3 = smooth3[valid.values].reset_index(drop=True)

    n = len(df)
    tr = slice(0, int(n * .70))
    va = slice(int(n * .70), int(n * .85))
    te = slice(int(n * .85), n)
    cv0 = df[CV].reset_index(drop=True)
    X = df[feats]
    d3 = smooth3 - cv0
    truth = y_raw.iloc[te].to_numpy()
    anchor_te = cv0.iloc[te].to_numpy()
    anchor_va = cv0.iloc[va].to_numpy()
    truth_va = y_raw.iloc[va].to_numpy()
    print(f"rows={n:,} test={n - te.start:,} persistence={_mae(truth, anchor_te)}")

    results: dict[str, dict] = {}

    def fit_gbt(name: str, rounds: int, **over):
        params = {**_BASE_PARAMS, "objective": "huber", **over}
        t0 = time.time()
        dtr = lgb.Dataset(X.iloc[tr], label=d3.iloc[tr])
        dva = lgb.Dataset(X.iloc[va], label=d3.iloc[va], reference=dtr)
        m = lgb.train(params, dtr, num_boost_round=rounds, valid_sets=[dva],
                      callbacks=[lgb.early_stopping(_EARLY_STOP, verbose=False),
                                 lgb.log_evaluation(0)])
        pred = anchor_te + m.predict(X.iloc[te])
        results[name] = {"mae": _mae(truth, pred), "best_iter": m.best_iteration,
                         "train_s": round(time.time() - t0, 1)}
        print(f"[{name}] MAE={results[name]['mae']} (iters {m.best_iteration})")
        return m, pred

    m6k, pred_h = fit_gbt("huber_6k", 6000)
    fit_gbt("huber_slow", 8000, learning_rate=0.015)

    arx = Ridge(alpha=10.0)
    arx.fit(X.iloc[tr].to_numpy(), d3.iloc[tr].to_numpy())
    pred_arx = anchor_te + arx.predict(X.iloc[te].to_numpy())
    results["arx"] = {"mae": _mae(truth, pred_arx)}
    print(f"[arx] MAE={results['arx']['mae']}")

    pv_h = anchor_va + m6k.predict(X.iloc[va])
    pv_a = anchor_va + arx.predict(X.iloc[va].to_numpy())
    ws = np.linspace(0, 0.6, 13)
    best_w = min(ws, key=lambda w: np.abs((1 - w) * pv_h + w * pv_a - truth_va).mean())
    pred_bl = (1 - best_w) * pred_h + best_w * pred_arx
    results["huber_arx"] = {"mae": _mae(truth, pred_bl), "w_arx": round(float(best_w), 2)}
    print(f"[huber_arx] MAE={results['huber_arx']['mae']} (w_arx={best_w:.2f})")

    results["persistence"] = {"mae": _mae(truth, anchor_te)}
    best = min((k for k in results if k != "persistence"),
               key=lambda k: results[k]["mae"])
    out = {"generated_at": pd.Timestamp.now().isoformat()[:19], "rows": n,
           "results": results, "verdict": {"best": best, "mae": results[best]["mae"]}}
    OUT.write_text(json.dumps(out, indent=1))
    print(f"VERDICT: {out['verdict']}  -> {OUT}")


if __name__ == "__main__":
    main()
