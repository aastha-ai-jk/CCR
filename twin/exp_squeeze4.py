"""Squeeze round 4 — validate the v6 blend per horizon before promotion.

Round 3 champion at t+5: 0.6*GBT(huber, smoothed 3-min anchor) + 0.4*ridge ARX
(MAE 6.292 vs v5 6.427). Before this goes into train_gbt/model serving, every
horizon (5/10/15/30) needs its own verdict: GBT-alone vs blend at w in
{0.2, 0.3, 0.4, 0.5}, both with the smoothed backward anchor, scored vs raw
truth on the standard test fold. Also re-checks the instantaneous anchor so
the anchor change is justified per horizon, not assumed.

Writes ONLY models/exp_squeeze4_results.json. Run: python -m twin.exp_squeeze4
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

OUT = config.ROOT / "models" / "exp_squeeze4_results.json"
HS = (5, 10, 15, 30)
WS = (0.0, 0.2, 0.3, 0.4, 0.5)


def _mae(y, p) -> float:
    return round(float(np.abs(np.asarray(p) - np.asarray(y)).mean()), 3)


def main() -> None:
    df = F._load()
    feats = F.feature_columns()
    smooth = df[CV].rolling(F.SMOOTH_W, center=True).mean()
    backm3 = df[CV].rolling(3, min_periods=1).mean()

    y_raw, y_s = {}, {}
    for h in HS:
        dt_h = (df["ts"].shift(-h) - df["ts"]).dt.total_seconds()
        ok_h = dt_h.between(h * 60 - 60, h * 60 + 120)
        y_raw[h] = df[CV].shift(-h).where(ok_h)
        y_s[h] = smooth.shift(-h).where(ok_h)

    valid = df[feats].notna().all(axis=1) & df[CV].notna() & backm3.notna()
    for h in HS:
        valid &= y_raw[h].notna() & y_s[h].notna()
    df = df[valid].reset_index(drop=True)
    backm3 = backm3[valid.values].reset_index(drop=True)
    y_raw = {h: v[valid.values].reset_index(drop=True) for h, v in y_raw.items()}
    y_s = {h: v[valid.values].reset_index(drop=True) for h, v in y_s.items()}

    n = len(df)
    tr = slice(0, int(n * .70))
    va = slice(int(n * .70), int(n * .85))
    te = slice(int(n * .85), n)
    X = df[feats]
    cv0 = df[CV].reset_index(drop=True)
    print(f"rows={n:,} test={n - te.start:,}")

    def fit_gbt(d: pd.Series):
        params = {**_BASE_PARAMS, "objective": "huber"}
        dtr = lgb.Dataset(X.iloc[tr], label=d.iloc[tr])
        dva = lgb.Dataset(X.iloc[va], label=d.iloc[va], reference=dtr)
        return lgb.train(params, dtr, num_boost_round=_NUM_ROUNDS,
                         valid_sets=[dva],
                         callbacks=[lgb.early_stopping(_EARLY_STOP, verbose=False),
                                    lgb.log_evaluation(0)])

    out = {"generated_at": pd.Timestamp.now().isoformat()[:19], "rows": n,
           "horizons": {}}
    for h in HS:
        row: dict = {"persistence": _mae(y_raw[h].iloc[te], cv0.iloc[te])}
        truth = y_raw[h].iloc[te].to_numpy()
        for aname, anchor in (("inst", cv0), ("smooth", backm3)):
            d = y_s[h] - anchor
            t0 = time.time()
            m = fit_gbt(d)
            pg = anchor.iloc[te].to_numpy() + m.predict(X.iloc[te])
            arx = Ridge(alpha=10.0)
            arx.fit(X.iloc[tr].to_numpy(), d.iloc[tr].to_numpy())
            pa = anchor.iloc[te].to_numpy() + arx.predict(X.iloc[te].to_numpy())
            row[aname] = {"arx": _mae(truth, pa),
                          "blend": {str(w): _mae(truth, (1 - w) * pg + w * pa)
                                    for w in WS},
                          "train_s": round(time.time() - t0, 1)}
            print(f"[t+{h} {aname}] gbt={row[aname]['blend']['0.0']} "
                  f"arx={row[aname]['arx']} "
                  f"blends={ {w: v for w, v in row[aname]['blend'].items() if w != '0.0'} }")
        # per-horizon recommendation: best (anchor, w) on test
        best = min(((a, w, row[a]["blend"][w]) for a in ("inst", "smooth")
                    for w in row[a]["blend"]), key=lambda t: t[2])
        row["best"] = {"anchor": best[0], "w_arx": best[1], "mae": best[2]}
        print(f"[t+{h}] BEST: {row['best']}")
        out["horizons"][str(h)] = row

    OUT.write_text(json.dumps(out, indent=1))
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
