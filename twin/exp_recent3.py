"""Round 3 (decisive): train INCLUDING the first week of the Sep oscillation regime,
score on the second week (Sep 14-20). Same cache; production recipe."""
import json, time, sys
from pathlib import Path
import numpy as np, pandas as pd, lightgbm as lgb, joblib
from sklearn.linear_model import Ridge
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from twin import features as F
from twin.train_gbt import _BASE_PARAMS, _NUM_ROUNDS, _EARLY_STOP, _ARX_W
OUT = ROOT / "models" / "exp_recent3_results.json"; CACHE = ROOT / "data" / "exp_recent_cache.parquet"
PROD_PKL = ROOT / "models" / "twin_gbt.pkl"    # production bundle (isolated run: point ROOT at a copy)
H = 5; THREADS = 22
TEST_FROM = pd.Timestamp("2026-09-14"); VAL_FROM = pd.Timestamp("2026-09-11"); REGIME_FROM = pd.Timestamp("2026-09-07")
res = {"windows": {"test": "Sep14-20", "val": "Sep11-13", "regime_from": "Sep7"}, "results": {}}
def save(): OUT.write_text(json.dumps(res, indent=1, default=str))
def mae(y, p): return round(float(np.abs(np.asarray(p) - np.asarray(y)).mean()), 3)
def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)
df = pd.read_parquet(CACHE); df["ts"] = pd.to_datetime(df["ts"]); feats = F.feature_columns()
te = (df.ts >= TEST_FROM).values; va = ((df.ts >= VAL_FROM) & (df.ts < TEST_FROM)).values
truth = df.y_raw[te].to_numpy(); truth_s = df.y_s[te].to_numpy(); cv0 = df.cv0; cv_te = cv0[te].to_numpy()
d_s = df.y_s - cv0
P = mae(truth, cv_te); res["results"]["persistence"] = {"mae_raw": P}
res["rows"] = {"test": int(te.sum()), "val": int(va.sum()), "regime_rows_available_for_train": int(((df.ts >= REGIME_FROM) & (df.ts < VAL_FROM)).sum())}
log(res["rows"], "persistence", P); save()
def score(name, pred, extra=None):
    r = {"mae_raw": mae(truth, pred), "mae_smooth": mae(truth_s, pred), "skill_raw_pct": round((1 - mae(truth, pred) / P) * 100, 1),
         "damping_slope": round(float(np.polyfit(truth - cv_te, pred - cv_te, 1)[0]), 3), **(extra or {})}
    res["results"][name] = r; save(); log(f"[{name}]", r)
b = joblib.load(PROD_PKL); hb = b["temp_multi"][H]; X_te = df.loc[te, feats]
score("T0_prod_model", (1 - _ARX_W) * hb["mean"].predict(X_te) + _ARX_W * (X_te.to_numpy(float) @ hb["arx"]["coef"] + hb["arx"]["intercept"]) + cv_te)
def fit(name, train_mask, cols=feats, weight=None, params=None, note=None):
    t1 = time.time(); X = df[cols]
    p = {**_BASE_PARAMS, "objective": "huber", "num_threads": THREADS, **(params or {})}
    w = None if weight is None else weight[train_mask]
    dtr = lgb.Dataset(X[train_mask], label=d_s[train_mask], weight=w); dva = lgb.Dataset(X[va], label=d_s[va], reference=dtr)
    m = lgb.train(p, dtr, num_boost_round=_NUM_ROUNDS, valid_sets=[dva],
                  callbacks=[lgb.early_stopping(_EARLY_STOP, verbose=False), lgb.log_evaluation(0)])
    r = Ridge(alpha=10.0).fit(X[train_mask].to_numpy(float), d_s[train_mask].to_numpy(float), sample_weight=w)
    pg = m.predict(X[te]) + cv_te; pa = r.predict(X[te].to_numpy(float)) + cv_te
    score(name, (1 - _ARX_W) * pg + _ARX_W * pa, {"gbt_only": mae(truth, pg), "arx_only": mae(truth, pa), "n_train": int(train_mask.sum()),
          "best_iter": int(m.best_iteration or m.num_trees()), "fit_min": round((time.time() - t1) / 60, 1), "note": note})
    return m
old = (df.ts < pd.Timestamp("2026-08-21")).values
pre_val = (df.ts < VAL_FROM).values
age = (VAL_FROM - df.ts).dt.total_seconds() / 86400.0
def hl(h): return np.power(0.5, np.clip(age, 0, None) / h).to_numpy()
regime_w10 = np.where(df.ts >= REGIME_FROM, 10.0, 1.0)
fit("T1_old_recipe_train_lt_Aug21", old, note="no regime data (like prod), early-stop on Sep11-13")
fit("T2_train_lt_Sep11_unweighted", pre_val, note="includes Sep7-10 regime rows")
fit("T3_train_lt_Sep11_hl30d", pre_val, weight=hl(30))
fit("T4_train_lt_Sep11_hl14d", pre_val, weight=hl(14))
fit("T5_train_lt_Sep11_regime_x10", pre_val, weight=regime_w10, note="Sep7+ rows weight 10")
fit("T5b_train_lt_Sep11_regime_x30", pre_val, weight=np.where(df.ts >= REGIME_FROM, 30.0, 1.0))
cv = df["cv0"]; coal = df["467RL400_TPH"]
df["x_ewm60_dev"] = cv.ewm(span=60, min_periods=5).mean() - cv; df["x_roll60_std"] = cv.rolling(60, min_periods=10).std()
df["x_mom90"] = cv.shift(90) - cv; df["x_osc_amp30"] = cv.rolling(30, min_periods=10).max() - cv.rolling(30, min_periods=10).min()
df["x_vol_ratio"] = df["423CI400_TT_4__roll5_std"] / (df["423CI400_TT_4__roll30_std"] + 0.1)
df["x_coal_ewm30"] = coal.ewm(span=30, min_periods=5).mean(); df["x_coal_dev30"] = coal - df["x_coal_ewm30"]
xcols = feats + [c for c in df.columns if c.startswith("x_")]
for c in xcols[len(feats):]: df[c] = df[c].fillna(df.loc[pre_val, c].median())
fit("T6_T5_plus_osc_feats", pre_val, cols=xcols, weight=regime_w10)
vol = df["423CI400_TT_4__roll30_std"]; thr = float(vol[old].quantile(0.75))
fit("T7_specialist_regime_plus_highvol_hist", pre_val & (((df.ts >= REGIME_FROM) | (vol > thr)).values), weight=regime_w10, note=f"regime rows + history rows with roll30_std>{thr:.1f}")
res["done"] = True; save(); log("DONE")
