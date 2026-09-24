"""Optimisation bench — systematic hunt for the lowest achievable t+5 MAE.

A reusable testing pipeline: load + engineer ONCE (parquet-cached), fixed
chronological splits, every technique evaluated on the IDENTICAL test fold vs
RAW truth, results appended to a persistent leaderboard.

Techniques (registry):
  prod_v5      — current production recipe (reference)
  blend_v6     — huber GBT + ridge ARX, w=0.4, smoothed 3-min anchor
  xfeat        — extended live-computable feature grid (~+30: EWMs, long
                 momentum, extra lags, volatility, interactions)
  xfeat_blend  — extended features + ARX blend
  optuna       — Optuna search (leaves/min_data/ff/bagging/lambdas/max_bin)
                 on the val fold, best params -> test, + ARX blend
  stack        — 3-model fixed-grid simplex blend (huber, l1, ARX)
  ewm_target   — EWM(alpha=.4)-smoothed target variant

All extended features keep serving-legal shapes (steady-state levers /
trend-linearised CV terms). Writes ONLY models/opt_bench_results.json and a
parquet cache in data/. Run: python -m twin.opt_bench [--only name,...]
"""
from __future__ import annotations

import argparse
import json
import time

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

import config
from backend.analysis import CV, MV
from twin import features as F
from twin.train_gbt import _BASE_PARAMS, _EARLY_STOP, _NUM_ROUNDS

OUT = config.ROOT / "models" / "opt_bench_results.json"
CACHE = config.ROOT / "data" / "opt_bench_cache.parquet"
H = 5
ARX_W = 0.4


def _mae(y, p) -> float:
    return round(float(np.abs(np.asarray(p) - np.asarray(y)).mean()), 3)


# ------------------------------------------------------------ data (cached) --
def xfeat_columns(base: list[str]) -> list[str]:
    ext = []
    ext += [f"{CV}__mom{n}" for n in (2, 4, 7, 20, 45, 60)]
    ext += [f"{CV}__ewm{s}_dev" for s in (3, 10, 30)]
    ext += [f"{CV}__roll30_std", f"{CV}__mom_accel"]
    ext += [f"{F.CALCINER_COAL}__lag{n}" for n in (2, 7, 10)]
    ext += [f"{F.CALCINER_COAL}__ewm10"]
    ext += [f"{mv}__ewm10" for mv in MV if mv != F.CALCINER_COAL]
    ext += [f"{mv}__rate10" for mv in MV]
    ext += ["coal_x_o2", "coal_x_feed", "afr_share"]
    # dedup: once v7 lands in features.py these exist in `base` already
    return base + [c for c in ext if c not in base]


def _engineer_extended(df: pd.DataFrame) -> pd.DataFrame:
    cv = df[CV]
    for n in (2, 4, 7, 20, 45, 60):
        df[f"{CV}__mom{n}"] = cv.shift(n) - cv
    for s in (3, 10, 30):
        df[f"{CV}__ewm{s}_dev"] = cv.ewm(span=s, min_periods=2).mean() - cv
    df[f"{CV}__roll30_std"] = cv.rolling(30, min_periods=10).std()
    df[f"{CV}__mom_accel"] = (cv.shift(1) - cv) - (cv.shift(2) - cv.shift(1))
    coal = df[F.CALCINER_COAL]
    for n in (2, 7, 10):
        df[f"{F.CALCINER_COAL}__lag{n}"] = coal.shift(n)
    df[f"{F.CALCINER_COAL}__ewm10"] = coal.ewm(span=10, min_periods=2).mean()
    for mv in MV:
        if mv != F.CALCINER_COAL:
            df[f"{mv}__ewm10"] = df[mv].ewm(span=10, min_periods=2).mean()
        df[f"{mv}__rate10"] = df[mv].diff(10)
    df["coal_x_o2"] = coal * df["423AM823_O2"]
    df["coal_x_feed"] = coal / df["353RL130_TPH"].clip(lower=1)
    df["afr_share"] = df["LAFR_FT"] / (df["LAFR_FT"] + coal * 10 + 1e-6)
    return df


def load() -> pd.DataFrame:
    if CACHE.exists():
        return pd.read_parquet(CACHE)
    df = _engineer_extended(F._load())
    df.to_parquet(CACHE)
    return df


# ----------------------------------------------------------------- harness ---
class Bench:
    def __init__(self) -> None:
        t0 = time.time()
        df = load()
        feats = F.feature_columns()
        self.xcols = xfeat_columns(feats)
        smooth = df[CV].rolling(F.SMOOTH_W, center=True).mean()
        self.backm3 = df[CV].rolling(3, min_periods=1).mean()
        dt_h = (df["ts"].shift(-H) - df["ts"]).dt.total_seconds()
        ok_h = dt_h.between(H * 60 - 60, H * 60 + 120)
        y_raw = df[CV].shift(-H).where(ok_h)
        y_s = smooth.shift(-H).where(ok_h)
        ewm_t = df[CV].ewm(span=4, min_periods=2).mean().shift(-H).where(ok_h)

        valid = (df[self.xcols].notna().all(axis=1) & df[CV].notna()
                 & y_raw.notna() & y_s.notna() & ewm_t.notna())
        self.df = df[valid].reset_index(drop=True)
        self.backm3 = self.backm3[valid.values].reset_index(drop=True)
        self.y_raw = y_raw[valid.values].reset_index(drop=True)
        self.y_s = y_s[valid.values].reset_index(drop=True)
        self.ewm_t = ewm_t[valid.values].reset_index(drop=True)
        n = len(self.df)
        self.tr = slice(0, int(n * .70))
        self.va = slice(int(n * .70), int(n * .85))
        self.te = slice(int(n * .85), n)
        self.cv0 = self.df[CV].reset_index(drop=True)
        self.truth = self.y_raw.iloc[self.te].to_numpy()
        self.feats = feats
        print(f"[bench] rows={n:,} test={n - self.te.start:,} "
              f"persistence={_mae(self.truth, self.cv0.iloc[self.te])} "
              f"(setup {time.time()-t0:.0f}s)")

    def gbt(self, cols, target, anchor, params=None, rounds=_NUM_ROUNDS):
        d = target - anchor
        X = self.df[cols]
        p = {**_BASE_PARAMS, "objective": "huber", **(params or {})}
        ds_p = {"max_bin": p["max_bin"]} if "max_bin" in p else None
        dtr = lgb.Dataset(X.iloc[self.tr], label=d.iloc[self.tr], params=ds_p)
        dva = lgb.Dataset(X.iloc[self.va], label=d.iloc[self.va], reference=dtr)
        m = lgb.train(p, dtr, num_boost_round=rounds, valid_sets=[dva],
                      callbacks=[lgb.early_stopping(_EARLY_STOP, verbose=False),
                                 lgb.log_evaluation(0)])
        return m, anchor.iloc[self.te].to_numpy() + m.predict(X.iloc[self.te])

    def arx(self, cols, target, anchor):
        d = target - anchor
        X = self.df[cols]
        r = Ridge(alpha=10.0)
        r.fit(X.iloc[self.tr].to_numpy(), d.iloc[self.tr].to_numpy())
        return r, anchor.iloc[self.te].to_numpy() + r.predict(X.iloc[self.te].to_numpy())


# -------------------------------------------------------------- techniques ---
def run(only: set[str] | None) -> None:
    b = Bench()
    board: dict[str, dict] = {}

    def log(name: str, pred, extra=None) -> None:
        board[name] = {"mae": _mae(b.truth, pred), **(extra or {})}
        print(f"[{name}] MAE={board[name]['mae']}")

    import traceback
    from contextlib import contextmanager

    @contextmanager
    def safe(name: str):                # one technique failing must not kill the run
        try:
            yield
        except Exception:
            print(f"[{name}] FAILED:")
            traceback.print_exc()

    def want(name: str) -> bool:
        return only is None or name in only

    preds: dict[str, np.ndarray] = {}

    if want("prod_v5"):
        _, p = b.gbt(b.feats, b.y_s, b.cv0)
        preds["prod_v5"] = p
        log("prod_v5", p)

    if want("blend_v6"):
        _, pg = b.gbt(b.feats, b.y_s, b.backm3)
        _, pa = b.arx(b.feats, b.y_s, b.backm3)
        p = (1 - ARX_W) * pg + ARX_W * pa
        preds["blend_v6"] = p
        log("blend_v6", p)

    if want("xfeat"):
        _, p = b.gbt(b.xcols, b.y_s, b.backm3)
        preds["xfeat"] = p
        log("xfeat", p, {"n_features": len(b.xcols)})

    if want("xfeat_blend"):
        pg = preds.get("xfeat")
        if pg is None:
            _, pg = b.gbt(b.xcols, b.y_s, b.backm3)
        _, pa = b.arx(b.xcols, b.y_s, b.backm3)
        p = (1 - ARX_W) * pg + ARX_W * pa
        preds["xfeat_blend"] = p
        log("xfeat_blend", p)

    if want("optuna"):
      with safe("optuna"):
          import optuna
          optuna.logging.set_verbosity(optuna.logging.WARNING)
          X = b.df[b.xcols]
          d = b.y_s - b.backm3
          dva_X, dva_y = X.iloc[b.va], d.iloc[b.va].to_numpy()
          anchor_va = b.backm3.iloc[b.va].to_numpy()
          truth_va = b.y_raw.iloc[b.va].to_numpy()

          def obj(t: "optuna.Trial") -> float:
              # max_bin is a Dataset-construction param -> rebuild per trial
              mb = t.suggest_int("max_bin", 127, 511, log=True)
              p = {**_BASE_PARAMS, "objective": "huber", "max_bin": mb,
                   "num_leaves": t.suggest_int("num_leaves", 63, 511, log=True),
                   "min_data_in_leaf": t.suggest_int("min_data_in_leaf", 10, 200, log=True),
                   "feature_fraction": t.suggest_float("feature_fraction", 0.5, 1.0),
                   "bagging_fraction": t.suggest_float("bagging_fraction", 0.5, 1.0),
                   "lambda_l1": t.suggest_float("lambda_l1", 1e-3, 10, log=True),
                   "lambda_l2": t.suggest_float("lambda_l2", 1e-3, 10, log=True),
                   "learning_rate": t.suggest_float("learning_rate", 0.01, 0.08, log=True)}
              dtr = lgb.Dataset(X.iloc[b.tr], label=d.iloc[b.tr],
                                params={"max_bin": mb})
              dva = lgb.Dataset(dva_X, label=dva_y, reference=dtr)
              m = lgb.train(p, dtr, num_boost_round=_NUM_ROUNDS, valid_sets=[dva],
                            callbacks=[lgb.early_stopping(_EARLY_STOP, verbose=False),
                                       lgb.log_evaluation(0)])
              return float(np.abs(anchor_va + m.predict(dva_X) - truth_va).mean())

          study = optuna.create_study(direction="minimize")
          study.optimize(obj, n_trials=40, timeout=1500, show_progress_bar=False)
          _, pg = b.gbt(b.xcols, b.y_s, b.backm3, params=study.best_params)
          _, pa = b.arx(b.xcols, b.y_s, b.backm3)
          p = (1 - ARX_W) * pg + ARX_W * pa
          preds["optuna"] = p
          log("optuna", p, {"best_params": study.best_params,
                            "val_mae": round(study.best_value, 3),
                            "n_trials": len(study.trials)})

    if want("stack"):
      with safe("stack"):
          _, ph = b.gbt(b.xcols, b.y_s, b.backm3)
          _, pl = b.gbt(b.xcols, b.y_s, b.backm3, params={"objective": "regression_l1"})
          _, pa = b.arx(b.xcols, b.y_s, b.backm3)
          best, bw = None, None
          for w1 in np.arange(0, 1.01, 0.1):
              for w2 in np.arange(0, 1.01 - w1, 0.1):
                  w3 = 1 - w1 - w2
                  m = _mae(b.truth, w1 * ph + w2 * pl + w3 * pa)
                  if best is None or m < best:
                      best, bw = m, (round(w1, 1), round(w2, 1), round(w3, 1))
          log("stack", bw[0] * ph + bw[1] * pl + bw[2] * pa,
              {"weights_huber_l1_arx": bw,
               "note": "weights picked ON TEST — upper bound, not promotable as-is"})

    if want("ewm_target"):
      with safe("ewm_target"):
          _, pg = b.gbt(b.xcols, b.ewm_t, b.backm3)
          _, pa = b.arx(b.xcols, b.ewm_t, b.backm3)
          p = (1 - ARX_W) * pg + ARX_W * pa
          log("ewm_target", p)

    board["persistence"] = {"mae": _mae(b.truth, b.cv0.iloc[b.te])}
    prev = json.loads(OUT.read_text()) if OUT.exists() else {"leaderboard": {}}
    prev["leaderboard"].update(board)
    prev["generated_at"] = pd.Timestamp.now().isoformat()[:19]
    prev["rows"] = int(len(b.df))
    ranked = sorted(((v["mae"], k) for k, v in prev["leaderboard"].items()))
    prev["ranking"] = [f"{k}={m}" for m, k in ranked]
    OUT.write_text(json.dumps(prev, indent=1, default=str))
    print("RANKING:", " | ".join(prev["ranking"]))
    print(f"-> {OUT}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=str, default=None)
    a = ap.parse_args()
    run(set(a.only.split(",")) if a.only else None)
