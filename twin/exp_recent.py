"""MAE-reduction experiment: does training on RECENT data (and weighting it) cut
t+5 error on the current (Sep 7-20) regime?  Frozen windows, identical rows,
production recipe (huber GBT 0.6 + ridge ARX 0.4, smoothed delta target,
scored vs RAW truth).  Writes ONLY models/exp_recent_results.json + a parquet
cache.  Isolated dir; production untouched."""
import json, time, sys
from pathlib import Path
import numpy as np, pandas as pd, lightgbm as lgb, joblib
from sklearn.linear_model import Ridge
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from twin import features as F
from twin.train_gbt import _BASE_PARAMS, _NUM_ROUNDS, _EARLY_STOP, _ARX_W
OUT = ROOT / "models" / "exp_recent_results.json"
CACHE = ROOT / "data" / "exp_recent_cache.parquet"
PROD_PKL = ROOT / "models" / "twin_gbt.pkl"    # production bundle (isolated run: point ROOT at a copy)
H = 5; THREADS = 22
TEST_FROM = pd.Timestamp("2026-09-07"); VAL_FROM = pd.Timestamp("2026-08-21")
res = {"windows": {"test_from": str(TEST_FROM), "val_from": str(VAL_FROM)}, "results": {}}
def save(): OUT.write_text(json.dumps(res, indent=1, default=str))
def mae(y, p): return round(float(np.abs(np.asarray(p) - np.asarray(y)).mean()), 3)
def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)

t0 = time.time()
if CACHE.exists():
    df = pd.read_parquet(CACHE); log("cache loaded", df.shape)
else:
    d = F.build_training_data(None)
    df = d["X"].copy(); df["ts"] = d["ts"]; df["cv0"] = d["cv0"]
    df["y_s"] = d["y"][f"temp_smooth_h{H}"]; df["y_raw"] = d["y"][f"temp_h{H}"]
    df = df.dropna(subset=["y_s", "y_raw"]).reset_index(drop=True)
    df.to_parquet(CACHE); log("built+cached", df.shape, f"{time.time()-t0:.0f}s")
feats = F.feature_columns()
df["ts"] = pd.to_datetime(df["ts"])
te = (df.ts >= TEST_FROM).values; va = ((df.ts >= VAL_FROM) & (df.ts < TEST_FROM)).values; tr = (df.ts < VAL_FROM).values
d_s = (df.y_s - df.cv0); truth = df.y_raw[te].to_numpy(); cv_te = df.cv0[te].to_numpy()
truth_s = df.y_s[te].to_numpy()
res["rows"] = {"train": int(tr.sum()), "val": int(va.sum()), "test": int(te.sum()),
               "test_span": [str(df.ts[te].min()), str(df.ts[te].max())]}
res["results"]["persistence"] = {"mae_raw": mae(truth, cv_te), "mae_smooth": mae(truth_s, cv_te)}
log("rows", res["rows"], "persistence", res["results"]["persistence"]); save()

def score(name, pred, extra=None):
    r = {"mae_raw": mae(truth, pred), "mae_smooth": mae(truth_s, pred),
         "skill_raw_pct": round((1 - mae(truth, pred) / res["results"]["persistence"]["mae_raw"]) * 100, 1),
         "damping_slope": round(float(np.polyfit(truth - cv_te, pred - cv_te, 1)[0]), 3), **(extra or {})}
    res["results"][name] = r; save(); log(f"[{name}]", r)

# ---- E0: production model scored on the SAME test rows -------------------
b = joblib.load(PROD_PKL); hb = b["temp_multi"][H]
X_te = df.loc[te, feats]
gbt_d = hb["mean"].predict(X_te); arx_d = X_te.to_numpy(float) @ hb["arx"]["coef"] + hb["arx"]["intercept"]
score("E0_prod_sep20_model", (1 - _ARX_W) * gbt_d + _ARX_W * arx_d + cv_te,
      {"note": "prod model (positional split; train ended ~Jun) on Sep7-20 rows"})

def fit(name, cols, weight=None, params=None, extra_note=None, train_mask=None, fixed_rounds=None):
    t1 = time.time()
    trm = tr if train_mask is None else train_mask
    X = df[cols]; p = {**_BASE_PARAMS, "objective": "huber", "num_threads": THREADS, **(params or {})}
    w_tr = None if weight is None else weight[trm]
    dtr = lgb.Dataset(X[trm], label=d_s[trm], weight=w_tr)
    if fixed_rounds:
        m = lgb.train(p, dtr, num_boost_round=fixed_rounds)
    else:
        dva = lgb.Dataset(X[va], label=d_s[va], reference=dtr)
        m = lgb.train(p, dtr, num_boost_round=_NUM_ROUNDS, valid_sets=[dva],
                      callbacks=[lgb.early_stopping(_EARLY_STOP, verbose=False), lgb.log_evaluation(0)])
    r = Ridge(alpha=10.0); r.fit(X[trm].to_numpy(float), d_s[trm].to_numpy(float), sample_weight=w_tr)
    pg = m.predict(X[te]); pa = r.predict(X[te].to_numpy(float))
    score(name, (1 - _ARX_W) * pg + _ARX_W * pa + cv_te,
          {"gbt_only": mae(truth, pg + cv_te), "arx_only": mae(truth, pa + cv_te),
           "best_iter": int(m.best_iteration or m.num_trees()), "n_feats": len(cols),
           "fit_min": round((time.time() - t1) / 60, 1), "note": extra_note})
    return m

age_days = (VAL_FROM - df.ts).dt.total_seconds() / 86400.0
def halflife(hl): return np.power(0.5, np.clip(age_days, 0, None) / hl).to_numpy()

fit("E1_recent_split_unweighted", feats, extra_note="train<Aug21, val Aug21-Sep6")
fit("E2_recency_hl90d", feats, weight=halflife(90))
fit("E3_recency_hl30d", feats, weight=halflife(30))
vol = df["423CI400_TT_4__roll30_std"]; wv = (1 + vol / vol[tr].median()).clip(upper=4).to_numpy()
fit("E4_volatility_weight", feats, weight=wv, extra_note="w=1+roll30_std/median, cap 4")

# ---- E5: oscillation-regime features (row-based windows on the CV series) --
cv = df["cv0"]; coal = df["467RL400_TPH"]
df["x_ewm60_dev"] = cv.ewm(span=60, min_periods=5).mean() - cv
df["x_roll60_std"] = cv.rolling(60, min_periods=10).std()
df["x_mom90"] = cv.shift(90) - cv; df["x_mom120"] = cv.shift(120) - cv
df["x_osc_amp30"] = cv.rolling(30, min_periods=10).max() - cv.rolling(30, min_periods=10).min()
df["x_vol_ratio"] = df["423CI400_TT_4__roll5_std"] / (df["423CI400_TT_4__roll30_std"] + 0.1)
df["x_coal_ewm30"] = coal.ewm(span=30, min_periods=5).mean()
df["x_coal_roll30_std"] = coal.rolling(30, min_periods=10).std()
df["x_coal_dev30"] = coal - df["x_coal_ewm30"]
xcols = feats + [c for c in df.columns if c.startswith("x_")]
okx = df[xcols].notna().all(axis=1).values
# keep identical test rows: fill the few NaNs at series start with medians
for c in xcols[len(feats):]: df[c] = df[c].fillna(df.loc[tr, c].median())
fit("E5_hl90_plus_osc_feats", xcols, weight=halflife(90))

# ---- E6: final refit on ALL pre-test rows (train+val) at E2's best_iter ----
it = res["results"]["E2_recency_hl90d"]["best_iter"]
age2 = (TEST_FROM - df.ts).dt.total_seconds() / 86400.0
w2 = np.power(0.5, np.clip(age2, 0, None) / 90).to_numpy()
fit("E6_refit_all_preSep7_hl90", feats, weight=w2, train_mask=(tr | va), fixed_rounds=it,
    extra_note=f"production-style final refit, fixed {it} rounds")
res["done"] = True; res["total_min"] = round((time.time() - t0) / 60, 1); save(); log("DONE")
