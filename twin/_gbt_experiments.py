"""Quick GBT-improvement experiments on the bench's exact evaluation split.

Tests, on the SAME common test rows as twin.bench:
  1. baseline        — the bench GBT (l2, 63 leaves)
  2. l1 objective    — optimise MAE directly
  3. tuned           — deeper/more trees + feature subsampling
  4. l1 + tuned      — both
  5. blend           — best GBT blended with the ARX (weight picked on val)
  6. smoothed target — predict the 3-min-average future temp (noise floor cut)

Run: python -m twin._gbt_experiments
"""
from __future__ import annotations

import numpy as np

from backend.analysis import CV
from twin import bench, bench_data


def _fit_gbt(X, dy, tr, va, **over):
    import lightgbm as lgb
    params = dict(objective="l2", num_leaves=63, learning_rate=0.05,
                  n_estimators=600, min_child_samples=40, verbose=-1)
    params.update(over)
    m = lgb.LGBMRegressor(**params)
    m.fit(X.iloc[tr], dy.iloc[tr], eval_set=[(X.iloc[va], dy.iloc[va])],
          callbacks=[__import__("lightgbm").early_stopping(40, verbose=False)])
    return m


def main() -> None:
    d = bench_data.build()
    df, y, cv0, split = d["df"], d["y"], d["cv0"], d["split"]
    common = d["valid_tab"] & bench._seq_valid(df) & bench._arx_valid(df)
    tr = np.flatnonzero((split == "train") & common)
    va = np.flatnonzero((split == "val") & common)
    ev = np.flatnonzero((split == "test") & common)
    X, dy = d["X_tab"], y - cv0
    y_ev, cv0_ev = y.iloc[ev].to_numpy(), cv0.iloc[ev].to_numpy()

    def score(pred, name):
        m = bench._metrics(y_ev, pred)
        w5 = 100 * float((np.abs(pred - y_ev) <= 5).mean())
        w10 = 100 * float((np.abs(pred - y_ev) <= 10).mean())
        print(f"{name:22} MAE={m['mae']:6.3f}  RMSE={m['rmse']:6.2f}  R2={m['r2']}"
              f"  within5C={w5:4.1f}%  within10C={w10:4.1f}%")
        return m["mae"], pred

    print(f"common: train={len(tr):,} val={len(va):,} test={len(ev):,}\n")

    base = _fit_gbt(X, dy, tr, va)
    _, p_base = score(cv0_ev + base.predict(X.iloc[ev]), "1 baseline (l2)")

    l1 = _fit_gbt(X, dy, tr, va, objective="l1")
    _, p_l1 = score(cv0_ev + l1.predict(X.iloc[ev]), "2 l1 objective")

    tuned = _fit_gbt(X, dy, tr, va, num_leaves=127, learning_rate=0.03,
                     n_estimators=2000, min_child_samples=20,
                     feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1)
    _, p_tuned = score(cv0_ev + tuned.predict(X.iloc[ev]), "3 tuned (l2)")

    both = _fit_gbt(X, dy, tr, va, objective="l1", num_leaves=127,
                    learning_rate=0.03, n_estimators=2000, min_child_samples=20,
                    feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1)
    mae_both, p_both = score(cv0_ev + both.predict(X.iloc[ev]), "4 l1 + tuned")

    # 5. blend the best GBT with the ARX, weight chosen on the val split
    Xf, _ = bench._arx_matrix(df)
    from sklearn.linear_model import Ridge
    arx = Ridge(alpha=100.0).fit(Xf[tr], dy.iloc[tr].to_numpy())
    p_arx_ev = cv0_ev + arx.predict(Xf[ev])
    gb_va = cv0.iloc[va].to_numpy() + both.predict(X.iloc[va])
    ar_va = cv0.iloc[va].to_numpy() + arx.predict(Xf[va])
    y_va = y.iloc[va].to_numpy()
    ws = np.linspace(0, 1, 21)
    maes = [np.abs(w * gb_va + (1 - w) * ar_va - y_va).mean() for w in ws]
    w_best = float(ws[int(np.argmin(maes))])
    score(w_best * p_both + (1 - w_best) * p_arx_ev, f"5 blend (w={w_best:.2f})")

    # 6. smoothed target: predict mean temp over t+4..t+6 (cuts sensor noise)
    y_s = df[CV].rolling(3, center=True).mean().shift(-bench_data.DELTA if hasattr(bench_data, "DELTA") else -5)
    from backend.analysis import DELTA
    y_s = df[CV].rolling(3, center=True).mean().shift(-DELTA)
    dy_s = y_s - cv0
    ok = dy_s.notna()
    tr2 = np.array([i for i in tr if ok.iloc[i]])
    va2 = np.array([i for i in va if ok.iloc[i]])
    ev2 = np.array([i for i in ev if ok.iloc[i]])
    sm = _fit_gbt(X, dy_s, tr2, va2, objective="l1", num_leaves=127,
                  learning_rate=0.03, n_estimators=2000, min_child_samples=20,
                  feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1)
    pred_s = cv0.iloc[ev2].to_numpy() + sm.predict(X.iloc[ev2])
    m = bench._metrics(y_s.iloc[ev2].to_numpy(), pred_s)
    raw = bench._metrics(y.iloc[ev2].to_numpy(), pred_s)
    print(f"{'6 smoothed target':22} MAE={m['mae']:6.3f} (vs smoothed truth) "
          f"| {raw['mae']:6.3f} vs raw truth")


if __name__ == "__main__":
    main()
