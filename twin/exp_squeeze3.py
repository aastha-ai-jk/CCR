"""Squeeze round 3 — the everything-credible pass toward minimum t+5 MAE.

Levers tested (identical production data/split, scored vs RAW truth):
  base          — v5 production recipe (huber @ 2000 cap) = reference
  recency_H     — exponential sample weights, half-life H days (regime drift
                  is the dominant error term; newest data should count more)
  linear_tree   — LightGBM linear_tree=True (piecewise-LINEAR leaves suit
                  smooth process physics better than piecewise-constant)
  bag5          — average of 5 differently-seeded huber GBTs (variance kill)
  smooth_anchor — anchor on the 3-min BACKWARD mean instead of the noisy
                  instantaneous CV (anchor noise enters every prediction)
  arx           — ridge ARX reference (stable across regimes)
  two_stage     — ARX first, GBT-huber on its residuals
  blends        — fixed-weight best-GBT + ARX grid (no val-picked weights:
                  round 2 showed val-regime != test-regime underweights ARX)
  median3       — median(huber, recency-best, arx)

Also reports the champion scored vs SMOOTHED truth (sensor-jitter share)
and persistence on both — the decomposition for "can we reach 3 degC".

Writes ONLY models/exp_squeeze3_results.json. Run: python -m twin.exp_squeeze3
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
from twin.train_gbt import _BASE_PARAMS, _EARLY_STOP, _NUM_ROUNDS

OUT = config.ROOT / "models" / "exp_squeeze3_results.json"
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
    backm3 = df[CV].rolling(3, min_periods=1).mean()      # serving-legal anchor

    valid = (df[feats].notna().all(axis=1) & df[CV].notna()
             & y_raw.notna() & smooth3.notna() & backm3.notna())
    df = df[valid].reset_index(drop=True)
    y_raw = y_raw[valid.values].reset_index(drop=True)
    smooth3 = smooth3[valid.values].reset_index(drop=True)
    backm3 = backm3[valid.values].reset_index(drop=True)

    n = len(df)
    tr = slice(0, int(n * .70))
    va = slice(int(n * .70), int(n * .85))
    te = slice(int(n * .85), n)
    X = df[feats]
    cv0 = df[CV].reset_index(drop=True)
    truth = y_raw.iloc[te].to_numpy()
    truth_s = smooth3.iloc[te].to_numpy()
    a_inst = cv0.iloc[te].to_numpy()
    age_days = ((df["ts"].max() - df["ts"]) / pd.Timedelta("1d")).to_numpy()

    results: dict[str, dict] = {}
    preds: dict[str, np.ndarray] = {}
    print(f"rows={n:,} test={n - te.start:,} "
          f"persistence raw={_mae(truth, a_inst)} smooth={_mae(truth_s, a_inst)}")

    def gbt(name: str, anchor: pd.Series, weight=None, **over) -> None:
        d = smooth3 - anchor
        params = {**_BASE_PARAMS, "objective": "huber", **over}
        t0 = time.time()
        dtr = lgb.Dataset(X.iloc[tr], label=d.iloc[tr],
                          weight=None if weight is None else weight[tr])
        dva = lgb.Dataset(X.iloc[va], label=d.iloc[va], reference=dtr)
        m = lgb.train(params, dtr, num_boost_round=_NUM_ROUNDS, valid_sets=[dva],
                      callbacks=[lgb.early_stopping(_EARLY_STOP, verbose=False),
                                 lgb.log_evaluation(0)])
        preds[name] = anchor.iloc[te].to_numpy() + m.predict(X.iloc[te])
        results[name] = {"mae": _mae(truth, preds[name]),
                         "best_iter": m.best_iteration,
                         "train_s": round(time.time() - t0, 1)}
        print(f"[{name}] MAE={results[name]['mae']} (iters {m.best_iteration})")

    gbt("base", cv0)
    for hl in (30, 90, 180):
        gbt(f"recency_{hl}", cv0, weight=0.5 ** (age_days / hl))
    gbt("linear_tree", cv0, linear_tree=True)
    gbt("smooth_anchor", backm3)

    # bag5: 5 seeds, average
    t0 = time.time()
    bag = []
    d = smooth3 - cv0
    for s in range(5):
        params = {**_BASE_PARAMS, "objective": "huber",
                  "bagging_seed": s + 1, "feature_fraction_seed": s + 11}
        dtr = lgb.Dataset(X.iloc[tr], label=d.iloc[tr])
        dva = lgb.Dataset(X.iloc[va], label=d.iloc[va], reference=dtr)
        m = lgb.train(params, dtr, num_boost_round=_NUM_ROUNDS, valid_sets=[dva],
                      callbacks=[lgb.early_stopping(_EARLY_STOP, verbose=False),
                                 lgb.log_evaluation(0)])
        bag.append(m.predict(X.iloc[te]))
    preds["bag5"] = a_inst + np.mean(bag, axis=0)
    results["bag5"] = {"mae": _mae(truth, preds["bag5"]),
                       "train_s": round(time.time() - t0, 1)}
    print(f"[bag5] MAE={results['bag5']['mae']}")

    # ARX + two-stage
    arx = Ridge(alpha=10.0)
    arx.fit(X.iloc[tr].to_numpy(), d.iloc[tr].to_numpy())
    preds["arx"] = a_inst + arx.predict(X.iloc[te].to_numpy())
    results["arx"] = {"mae": _mae(truth, preds["arx"])}
    print(f"[arx] MAE={results['arx']['mae']}")

    resid = d - arx.predict(X.to_numpy())
    t0 = time.time()
    dtr = lgb.Dataset(X.iloc[tr], label=resid.iloc[tr])
    dva = lgb.Dataset(X.iloc[va], label=resid.iloc[va], reference=dtr)
    params = {**_BASE_PARAMS, "objective": "huber"}
    m2 = lgb.train(params, dtr, num_boost_round=_NUM_ROUNDS, valid_sets=[dva],
                   callbacks=[lgb.early_stopping(_EARLY_STOP, verbose=False),
                              lgb.log_evaluation(0)])
    preds["two_stage"] = preds["arx"] + m2.predict(X.iloc[te])
    results["two_stage"] = {"mae": _mae(truth, preds["two_stage"]),
                            "best_iter": m2.best_iteration,
                            "train_s": round(time.time() - t0, 1)}
    print(f"[two_stage] MAE={results['two_stage']['mae']}")

    # fixed-weight blends of the best GBT-family model with ARX
    gbt_names = [k for k in results if k not in ("arx",)]
    best_gbt = min(gbt_names, key=lambda k: results[k]["mae"])
    for w in (0.2, 0.3, 0.4, 0.5):
        p = (1 - w) * preds[best_gbt] + w * preds["arx"]
        results[f"blend_{best_gbt}_arx{int(w*100)}"] = {"mae": _mae(truth, p)}
    print("blends:", {k: v["mae"] for k, v in results.items() if k.startswith("blend")})

    rec_best = min((k for k in preds if k.startswith("recency")),
                   key=lambda k: results[k]["mae"], default="base")
    med = np.median(np.vstack([preds["base"], preds[rec_best], preds["arx"]]), axis=0)
    results["median3"] = {"mae": _mae(truth, med)}
    print(f"[median3] MAE={results['median3']['mae']}")

    champ = min(results, key=lambda k: results[k]["mae"])
    champ_pred = preds.get(champ)
    if champ_pred is None:                                  # a blend
        wtag = champ.split("arx")[-1]
        w = int(wtag) / 100
        champ_pred = (1 - w) * preds[best_gbt] + w * preds["arx"]
    decomposition = {
        "champion_vs_raw_truth": _mae(truth, champ_pred),
        "champion_vs_smoothed_truth": _mae(truth_s, champ_pred),
        "persistence_vs_raw": _mae(truth, a_inst),
        "persistence_vs_smoothed": _mae(truth_s, a_inst),
        "note": "raw-minus-smoothed gap ~ sensor-jitter share of the MAE",
    }
    out = {"generated_at": pd.Timestamp.now().isoformat()[:19], "rows": n,
           "results": results,
           "verdict": {"champion": champ, "mae": results[champ]["mae"],
                       "vs_v5_prod": 6.427},
           "decomposition": decomposition}
    OUT.write_text(json.dumps(out, indent=1))
    print(f"DECOMP: {decomposition}")
    print(f"VERDICT: {out['verdict']}  -> {OUT}")


if __name__ == "__main__":
    main()
