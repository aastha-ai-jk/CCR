"""Round 2: recipe levers on the frozen Sep 7-20 window (same cache/rows as round 1)."""
import json, time, sys
from pathlib import Path
import numpy as np, pandas as pd, lightgbm as lgb
from sklearn.linear_model import Ridge
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from twin import features as F
from twin.train_gbt import _BASE_PARAMS, _NUM_ROUNDS, _EARLY_STOP, _ARX_W
OUT = ROOT / "models" / "exp_recent2_results.json"; CACHE = ROOT / "data" / "exp_recent_cache.parquet"
H = 5; THREADS = 22
TEST_FROM = pd.Timestamp("2026-09-07"); VAL_FROM = pd.Timestamp("2026-08-21")
res = {"results": {}}
def save(): OUT.write_text(json.dumps(res, indent=1, default=str))
def mae(y, p): return round(float(np.abs(np.asarray(p) - np.asarray(y)).mean()), 3)
def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)
df = pd.read_parquet(CACHE); df["ts"] = pd.to_datetime(df["ts"]); feats = F.feature_columns()
te = (df.ts >= TEST_FROM).values; va = ((df.ts >= VAL_FROM) & (df.ts < TEST_FROM)).values; tr = (df.ts < VAL_FROM).values
truth = df.y_raw[te].to_numpy(); truth_s = df.y_s[te].to_numpy(); cv0 = df.cv0
backm3 = df.cv0.rolling(3, min_periods=1).mean()
P = mae(truth, cv0[te]); res["results"]["persistence"] = {"mae_raw": P}
def score(name, pred, extra=None):
    r = {"mae_raw": mae(truth, pred), "mae_smooth": mae(truth_s, pred), "skill_raw_pct": round((1 - mae(truth, pred) / P) * 100, 1),
         "damping_slope": round(float(np.polyfit(truth - cv0[te].to_numpy(), pred - cv0[te].to_numpy(), 1)[0]), 3), **(extra or {})}
    res["results"][name] = r; save(); log(f"[{name}]", r)
def fit(name, cols=feats, target=None, anchor=None, objective="huber", params=None, rounds=_NUM_ROUNDS,
        train_mask=None, note=None, arx_w=_ARX_W, weight=None):
    t1 = time.time(); trm = tr if train_mask is None else train_mask
    tgt = df.y_s if target is None else target; anc = cv0 if anchor is None else anchor
    d = tgt - anc; X = df[cols]
    p = {**_BASE_PARAMS, "objective": objective, "num_threads": THREADS, **(params or {})}
    dtr = lgb.Dataset(X[trm], label=d[trm], weight=None if weight is None else weight[trm])
    dva = lgb.Dataset(X[va], label=d[va], reference=dtr)
    m = lgb.train(p, dtr, num_boost_round=rounds, valid_sets=[dva],
                  callbacks=[lgb.early_stopping(_EARLY_STOP, verbose=False), lgb.log_evaluation(0)])
    r = Ridge(alpha=10.0).fit(X[trm].to_numpy(float), d[trm].to_numpy(float), sample_weight=None if weight is None else weight[trm])
    pg = m.predict(X[te]) + anc[te].to_numpy(); pa = r.predict(X[te].to_numpy(float)) + anc[te].to_numpy()
    score(name, (1 - arx_w) * pg + arx_w * pa, {"gbt_only": mae(truth, pg), "arx_only": mae(truth, pa),
          "best_iter": int(m.best_iteration or m.num_trees()), "fit_min": round((time.time() - t1) / 60, 1), "note": note})
    return m, pg, pa

_, pg_h, pa_h = fit("R0_baseline_repro", note="= E1 recipe, reference for this round")
fit("R1_objective_l1", objective="regression_l1")
fit("R2_rounds4000_lr03", rounds=4000, note="cap lifted (best_iter hit 2000)")
fit("R3_lr06_rounds2000", params={"learning_rate": 0.06})
fit("R4_anchor_backm3", anchor=backm3, note="delta from 3-min backward mean; reconstruct +backm3")
fit("R5_target_raw", target=df.y_raw, note="unsmoothed target")
fit("R6_leaves255_mdl50", params={"num_leaves": 255, "min_data_in_leaf": 50})
fit("R7_leaves63_mdl100_ff06", params={"num_leaves": 63, "min_data_in_leaf": 100, "feature_fraction": 0.6, "lambda_l2": 5.0}, note="more regularised")
vol = df["423CI400_TT_4__roll30_std"]; thr = float(vol[tr].quantile(0.5))
fit("R8_specialist_highvol", train_mask=tr & (vol > thr).values, note=f"train only rows roll30_std>{thr:.1f} (top half volatility)")
fit("R9_arx_w0", arx_w=0.0, note="GBT alone"); fit("R9b_arx_w06", arx_w=0.6)
# blend weight sweep on the baseline preds (diagnostic only — picked on test)
sweep = {str(round(w, 1)): mae(truth, (1 - w) * pg_h + w * pa_h) for w in np.arange(0, 1.01, 0.1)}
res["results"]["arx_weight_sweep_on_test"] = sweep; save(); log("sweep", sweep)
res["done"] = True; save(); log("DONE")
