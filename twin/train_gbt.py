"""Train the LightGBM digital-twin core.

Fits gradient-boosted regressors for calciner temperature (mean + p10/p90
quantiles), each combustion-safety signal, and lab quality (free lime, degree
of calcination), then writes ``models/twin_gbt.pkl`` and merges test metrics
into ``models/metrics.json``. Run: ``python -m twin.train_gbt [--sample N]``.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd

from backend.analysis import SAFETY
from twin import features as F

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
PKL_PATH = MODELS_DIR / "twin_gbt.pkl"
METRICS_PATH = MODELS_DIR / "metrics.json"

# v2 recipe (validated on the Model Bench, 2026-08-04): L1 objective for the
# temperature mean, deeper trees, more rounds with early stopping.
_BASE_PARAMS = {
    "learning_rate": 0.03,
    "num_leaves": 127,
    "min_data_in_leaf": 20,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "max_depth": -1,
    "verbose": -1,
    "num_threads": _THREADS,
}
_NUM_ROUNDS = 2000
# 2026-09-23: cap threads. Without it LightGBM grabbed all 32 cores while the
# shadow (~8) + optimizer (~5) loops were busy -> OpenMP oversubscription made
# the weekly retrain take ~9 h (02:00 -> 10:50). Capped fits run in 1-3 min each.
_THREADS = 20
_EARLY_STOP = 40
_ARX_W = 0.4        # v6 served-mean blend weight for the ridge ARX (exp_squeeze4)


def _fit(x_tr: pd.DataFrame, y_tr: pd.Series, x_val: pd.DataFrame,
         y_val: pd.Series, objective: str, alpha: float | None = None) -> lgb.Booster:
    """Train one Booster with early stopping on the validation fold."""
    params = {**_BASE_PARAMS, "objective": objective}
    if alpha is not None:
        params["alpha"] = alpha
    dtrain = lgb.Dataset(x_tr, label=y_tr)
    dval = lgb.Dataset(x_val, label=y_val, reference=dtrain)
    return lgb.train(params, dtrain, num_boost_round=_NUM_ROUNDS,
                     valid_sets=[dval],
                     callbacks=[lgb.early_stopping(_EARLY_STOP, verbose=False),
                                lgb.log_evaluation(0)])


def _scores(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    err = y_pred - y_true
    ss_res = float(np.sum(err ** 2))
    ss_tot = float(np.sum((y_true - y_true.mean()) ** 2)) or 1.0
    return {
        "mae": round(float(np.mean(np.abs(err))), 3),
        "rmse": round(float(np.sqrt(np.mean(err ** 2))), 3),
        "r2": round(1.0 - ss_res / ss_tot, 4),
    }


def _folds(x: pd.DataFrame, y: pd.Series, split: pd.Series,
           extra: pd.Series | None = None):
    """Return train/val/test frames for a single target, dropping NaN labels.

    When ``extra`` is given each fold is ``(X, y, extra)`` (e.g. CV(t) for the
    delta reconstruction); otherwise ``(X, y)``.
    """
    out = {}
    for name in ("train", "val", "test"):
        mask = ((split == name) & y.notna()).values
        if extra is None:
            out[name] = (x[mask], y[mask])
        else:
            out[name] = (x[mask], y[mask], extra[mask])
    return out


def train(sample: int | None, test_out: dict | None = None) -> dict:
    data = F.build_training_data(sample)
    x, y, split, cv0 = data["X"], data["y"], data["split"], data["cv0"]
    print(f"[twin] rows={len(x)} features={x.shape[1]} "
          f"split={split.value_counts().to_dict()}")

    metrics: dict = {}
    bundle: dict = {
        "feature_columns": F.feature_columns(),
        "horizon_min": F.HORIZON_MIN,
    }

    # --- temperature: TRAJECTORY heads (mean + p10/p90 per horizon) --------
    # v3: one head-set per horizon in features.HORIZONS. Each TRAINS on the
    # noise-smoothed *change* over its horizon (regime-invariant) but is
    # REPORTED against the raw target — no metric flattery.
    # NB: boosters output temp(t+h) - CV(t); callers add current_temp (CV(t)).
    bundle["temp_multi"] = {}
    bundle["temp_delta"] = True
    bundle["version"] = "v3"
    metrics["temp_horizons"] = {}
    for h in F.HORIZONS:
        delta_s = y[f"temp_smooth_h{h}"] - cv0
        delta_raw = y[f"temp_h{h}"] - cv0
        mask_t = delta_s.notna() & delta_raw.notna()
        temp = _folds(x[mask_t.values], delta_s[mask_t.values],
                      split[mask_t.values], extra=cv0[mask_t.values])
        raw_te = delta_raw[mask_t.values][(split[mask_t.values] == "test").values]
        (x_tr, dy_tr, _), (x_val, dy_val, _), (x_te, _, cv_te) = (
            temp["train"], temp["val"], temp["test"])
        # v5 recipe (exp_squeeze, 2026-08-27): huber beats l1 for the temp mean
        # UNDER THE 2000-ROUND CAP (6.43 vs 6.73 on identical data). The cap is
        # load-bearing regularisation — uncapped huber drifts worse (6.74 at
        # 6k rounds), so never raise _NUM_ROUNDS without re-running exp_squeeze.
        mean = _fit(x_tr, dy_tr, x_val, dy_val, "huber")
        p10 = _fit(x_tr, dy_tr, x_val, dy_val, "quantile", alpha=0.1)
        p90 = _fit(x_tr, dy_tr, x_val, dy_val, "quantile", alpha=0.9)
        # v6 (exp_squeeze3/4, 2026-08-27): the served mean is a fixed-weight
        # blend of the GBT with a ridge ARX fit on the same delta target. The
        # two make differently-shaped errors (ARX is regime-stable where the
        # trees overfit) — the blend beat GBT-alone at EVERY horizon. Weight
        # is FIXED, not val-picked: val-regime != test-regime systematically
        # underweights the ARX (see models/exp_squeeze{2,4}_results.json).
        from sklearn.linear_model import Ridge
        arx = Ridge(alpha=10.0)
        arx.fit(x_tr, dy_tr)
        bundle["temp_multi"][h] = {
            "mean": mean, "p10": p10, "p90": p90,
            "arx": {"coef": arx.coef_.astype(float),
                    "intercept": float(arx.intercept_), "w": _ARX_W,
                    "x_mean": x_tr.mean().to_numpy(dtype=float)},
        }

        cv_np = cv_te.to_numpy(float)
        yt = raw_te.to_numpy(float) + cv_np                        # RAW absolute truth
        # metrics reflect the SERVED (blended) mean, so the retrain guard
        # judges exactly what production will run
        gbt_d = mean.predict(x_te)
        arx_d = x_te.to_numpy(float) @ arx.coef_ + arx.intercept_
        pred = (1 - _ARX_W) * gbt_d + _ARX_W * arx_d + cv_np
        lo, hi = p10.predict(x_te) + cv_np, p90.predict(x_te) + cv_np
        hm = _scores(yt, pred)
        hm["interval_coverage"] = round(float(np.mean((yt >= lo) & (yt <= hi))), 4)
        hm["baseline_mae"] = _scores(yt, cv_np)["mae"]              # persistence
        metrics["temp_horizons"][str(h)] = hm
        print(f"[twin] t+{h:>2}min  mae={hm['mae']}  r2={hm['r2']}  "
              f"cov={hm['interval_coverage']}  persist={hm['baseline_mae']}")
        if h == F.HORIZON_MIN:                                      # back-compat
            bundle["temp"] = bundle["temp_multi"][h]
            metrics["temp"] = hm
            metrics["baseline_persistence"] = {
                "temp": {"mae": hm["baseline_mae"], "rmse": _scores(yt, cv_np)["rmse"]}}
            metrics["n_train"] = int(len(x_tr))
            metrics["n_test"] = int(len(x_te))
            if test_out is not None:            # same-window retrain guard
                test_out.update({"x": x_te, "y_raw": yt, "cv": cv_np,
                                 "feature_columns": list(x_te.columns)})

    # --- safety signals ----------------------------------------------------
    bundle["safety"] = {}
    metrics["safety"] = {}
    for tag in SAFETY:
        f = _folds(x, y[f"safety_{tag}"], split)
        booster = _fit(*f["train"], *f["val"], "regression")
        bundle["safety"][tag] = booster
        xs, ys = f["test"]
        metrics["safety"][tag] = _scores(ys.to_numpy(float), booster.predict(xs))

    # --- lab quality -------------------------------------------------------
    bundle["quality"] = {}
    metrics["quality"] = {}
    for key, col in (("free_lime", "quality_free_lime"), ("doc", "quality_doc")):
        f = _folds(x, y[col], split)
        if len(f["train"][0]) < 50 or len(f["test"][0]) == 0:
            print(f"[twin] quality {key}: insufficient labelled rows, skipped")
            continue
        booster = _fit(*f["train"], *f["val"], "regression")
        bundle["quality"][key] = booster
        xq, yq = f["test"]
        metrics["quality"][key] = _scores(yq.to_numpy(float), booster.predict(xq))

    bundle["trained_at"] = datetime.now(timezone.utc).isoformat()
    metrics["trained_at"] = bundle["trained_at"]

    _save(bundle, metrics)
    return metrics


def _save(bundle: dict, metrics: dict) -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, PKL_PATH)
    merged: dict = {}
    if METRICS_PATH.exists():
        try:
            merged = json.loads(METRICS_PATH.read_text())
        except json.JSONDecodeError:
            merged = {}
    merged.update(metrics)                       # preserve foreign keys (e.g. "nn")
    METRICS_PATH.write_text(json.dumps(merged, indent=2))
    print(f"[twin] wrote {PKL_PATH} and {METRICS_PATH}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Train the LightGBM digital-twin core.")
    ap.add_argument("--sample", type=int, default=None,
                    help="decimate to N chronological rows (default: all)")
    args = ap.parse_args()

    m = train(args.sample)
    t, b = m["temp"], m["baseline_persistence"]["temp"]
    print(f"[twin] TEMP  mae={t['mae']}  rmse={t['rmse']}  r2={t['r2']}  "
          f"coverage={t['interval_coverage']}")
    print(f"[twin] PERSIST baseline  mae={b['mae']}  rmse={b['rmse']}  "
          f"(twin cuts MAE {(1 - t['mae'] / b['mae']) * 100:.0f}%)")


if __name__ == "__main__":
    main()
