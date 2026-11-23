"""P9 experiment: tune the T+10 head the advisor relies on.

Variants (served mean = (1-w) * GBT + w * ridge ARX, trained on the smoothed
10-min change, scored against the RAW temperature):
  target smoothing SMOOTH_W in (1, 3, 5) min, ARX weight w in 0.2..0.6,
  GBT size num_leaves in (63, 127, 255) (at SMOOTH_W 3).
Baseline = today's recipe (SMOOTH_W 3, num_leaves 127, w 0.4).

Anti-luck: the test window is split in two halves (chronological). A variant
is CHOSEN on half A and CONFIRMED on half B; adopt only if it beats the
baseline by >= 0.1 degC on BOTH halves. Result: models/exp_p9_t10_results.json.

Run: python -m twin.exp_p9_t10
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from sklearn.linear_model import Ridge

from twin import features as F
from twin import train_gbt as T

H = 10
WS = (0.2, 0.3, 0.4, 0.5, 0.6)
OUT = Path(__file__).resolve().parent.parent / "models" / "exp_p9_t10_results.json"


def _fit_leaves(x_tr, y_tr, x_val, y_val, leaves: int):
    old = dict(T._BASE_PARAMS)
    T._BASE_PARAMS["num_leaves"] = leaves
    try:
        return T._fit(x_tr, y_tr, x_val, y_val, "huber")
    finally:
        T._BASE_PARAMS.clear()
        T._BASE_PARAMS.update(old)


def main() -> None:
    res = []
    for sw in (1, 3, 5):
        F.SMOOTH_W = sw
        d = F.build_training_data(None)
        x, y, split, cv0 = d["X"], d["y"], d["split"], d["cv0"]
        ds, dr = y[f"temp_smooth_h{H}"] - cv0, y[f"temp_h{H}"] - cv0
        m = (ds.notna() & dr.notna()).values
        f = T._folds(x[m], ds[m], split[m], extra=cv0[m])
        (x_tr, y_tr, _), (x_val, y_val, _), (x_te, _, cv_te) = f["train"], f["val"], f["test"]
        truth = (dr[m][(split[m] == "test").values] + cv_te).to_numpy(float)
        cv = cv_te.to_numpy(float)
        half = len(truth) // 2
        arx = Ridge(alpha=10.0).fit(x_tr, y_tr)
        arx_te = x_te.to_numpy(float) @ arx.coef_ + arx.intercept_
        for leaves in ((63, 127, 255) if sw == 3 else (127,)):
            gbt_te = _fit_leaves(x_tr, y_tr, x_val, y_val, leaves).predict(x_te)
            for w in WS:
                err = np.abs((1 - w) * gbt_te + w * arx_te + cv - truth)
                res.append({"smooth_w": sw, "num_leaves": leaves, "arx_w": w,
                            "mae": round(float(err.mean()), 3),
                            "mae_A": round(float(err[:half].mean()), 3),
                            "mae_B": round(float(err[half:].mean()), 3)})
                print("[p9]", res[-1], flush=True)
    F.SMOOTH_W = 3
    base = next(r for r in res if (r["smooth_w"], r["num_leaves"], r["arx_w"]) == (3, 127, 0.4))
    best = min(res, key=lambda r: r["mae_A"])
    adopt = (base["mae_A"] - best["mae_A"] >= 0.1 and base["mae_B"] - best["mae_B"] >= 0.1)
    out = {"baseline": base, "best_on_A": best, "adopt": adopt, "all": res}
    OUT.write_text(json.dumps(out, indent=2))
    print("[p9] baseline", base, "\n[p9] best", best, "\n[p9] adopt", adopt)


if __name__ == "__main__":
    main()
