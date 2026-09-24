"""Short-horizon test: does the twin add value at t+1/t+2/t+3 min?

Same v7 recipe (89 features, huber GBT + ridge ARX blend w=0.4, smoothed
target scored vs RAW truth, standard chronological split). At very short
horizons the plant barely moves, so persistence is a much harder baseline —
this measures where the twin's edge begins.

Writes ONLY models/exp_short_h_results.json. Run: python -m twin.exp_short_h
"""
from __future__ import annotations

import json

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

import config
from backend.analysis import CV
from twin import features as F
from twin.train_gbt import _ARX_W, _BASE_PARAMS, _EARLY_STOP, _NUM_ROUNDS

OUT = config.ROOT / "models" / "exp_short_h_results.json"
HS = (1, 2, 3)


def _mae(y, p) -> float:
    return round(float(np.abs(np.asarray(p) - np.asarray(y)).mean()), 3)


def main() -> None:
    df = F._load()
    feats = F.feature_columns()
    smooth = df[CV].rolling(F.SMOOTH_W, center=True).mean()

    y_raw, y_s = {}, {}
    for h in HS:
        dt_h = (df["ts"].shift(-h) - df["ts"]).dt.total_seconds()
        ok_h = dt_h.between(h * 60 - 30, h * 60 + 120)
        y_raw[h] = df[CV].shift(-h).where(ok_h)
        y_s[h] = smooth.shift(-h).where(ok_h)

    valid = df[feats].notna().all(axis=1) & df[CV].notna()
    for h in HS:
        valid &= y_raw[h].notna() & y_s[h].notna()
    df = df[valid].reset_index(drop=True)
    y_raw = {h: v[valid.values].reset_index(drop=True) for h, v in y_raw.items()}
    y_s = {h: v[valid.values].reset_index(drop=True) for h, v in y_s.items()}

    n = len(df)
    tr = slice(0, int(n * .70))
    va = slice(int(n * .70), int(n * .85))
    te = slice(int(n * .85), n)
    X = df[feats]
    cv0 = df[CV].reset_index(drop=True)
    print(f"rows={n:,} test={n - te.start:,}")

    out = {"generated_at": pd.Timestamp.now().isoformat()[:19], "rows": n,
           "recipe": "v7: 89 feats, huber GBT + ARX blend w=0.4", "horizons": {}}
    for h in HS:
        d = y_s[h] - cv0
        params = {**_BASE_PARAMS, "objective": "huber"}
        dtr = lgb.Dataset(X.iloc[tr], label=d.iloc[tr])
        dva = lgb.Dataset(X.iloc[va], label=d.iloc[va], reference=dtr)
        m = lgb.train(params, dtr, num_boost_round=_NUM_ROUNDS, valid_sets=[dva],
                      callbacks=[lgb.early_stopping(_EARLY_STOP, verbose=False),
                                 lgb.log_evaluation(0)])
        arx = Ridge(alpha=10.0)
        arx.fit(X.iloc[tr].to_numpy(), d.iloc[tr].to_numpy())
        anchor = cv0.iloc[te].to_numpy()
        truth = y_raw[h].iloc[te].to_numpy()
        pg = anchor + m.predict(X.iloc[te])
        pa = anchor + arx.predict(X.iloc[te].to_numpy())
        pb = (1 - _ARX_W) * pg + _ARX_W * pa
        pers = _mae(truth, anchor)
        row = {"gbt": _mae(truth, pg), "arx": _mae(truth, pa),
               "blend": _mae(truth, pb), "persistence": pers,
               "skill_pct": round(100 * (1 - _mae(truth, pb) / pers), 1),
               "best_iter": m.best_iteration}
        out["horizons"][f"t+{h}"] = row
        print(f"[t+{h}] blend={row['blend']} gbt={row['gbt']} arx={row['arx']} "
              f"persist={pers} skill={row['skill_pct']}%")

    OUT.write_text(json.dumps(out, indent=1))
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
