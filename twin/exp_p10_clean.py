"""P10 experiment: does training on cleaned data (impossible values and frozen
instruments removed, twin/features._bad_rows) make the twin more accurate?

Backs up the served model, retrains on the cleaned data, then scores the OLD
and the NEW model on the SAME cleaned test rows at T+5 and T+10 (served blend:
GBT + ridge ARX). Keeps the new model only if T+10 is not worse; otherwise the
backup is restored. Result: models/exp_p10_clean_results.json.

Run: python -m twin.exp_p10_clean
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

import joblib
import numpy as np

from twin import features as F
from twin import train_gbt

MODELS = Path(__file__).resolve().parent.parent / "models"
PKL, MET = MODELS / "twin_gbt.pkl", MODELS / "metrics.json"


def _test_rows(data: dict, h: int):
    y, split, cv0 = data["y"], data["split"], data["cv0"]
    raw = y[f"temp_h{h}"] - cv0
    ok = (y[f"temp_smooth_h{h}"] - cv0).notna() & raw.notna() & (split == "test")
    return data["X"][ok.values], (raw[ok] + cv0[ok]).to_numpy(float), cv0[ok].to_numpy(float)


def _mae(bundle: dict, x, y_true, cv) -> float:
    hb = bundle["temp_multi"][h_key(bundle, H)]
    w = float(hb["arx"]["w"])
    pred = (1 - w) * hb["mean"].predict(x) + w * (x.to_numpy(float) @ hb["arx"]["coef"]
                                                  + hb["arx"]["intercept"]) + cv
    return round(float(np.mean(np.abs(pred - y_true))), 3)


def h_key(bundle: dict, h: int):
    return h if h in bundle["temp_multi"] else str(h)


def main() -> None:
    global H
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    bak = MODELS / f"backup-before-P10-{stamp}"
    bak.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PKL, bak / PKL.name)
    shutil.copy2(MET, bak / MET.name)
    old = joblib.load(PKL)

    data = F.build_training_data(None)           # cleaned
    tests = {h: _test_rows(data, h) for h in (5, 10)}
    res = {"ts": stamp, "backup": bak.name, "n_rows_clean": int(len(data["X"])), "old": {}, "new": {}}
    for H in (5, 10):
        x, yt, cv = tests[H]
        res["old"][f"t{H}"] = _mae(old, x, yt, cv)
        res.setdefault("persistence", {})[f"t{H}"] = round(float(np.mean(np.abs(cv - yt))), 3)
        res.setdefault("n_test", {})[f"t{H}"] = int(len(yt))

    train_gbt.train(None)                         # writes the new model
    new = joblib.load(PKL)
    for H in (5, 10):
        x, yt, cv = tests[H]
        res["new"][f"t{H}"] = _mae(new, x, yt, cv)

    keep = res["new"]["t10"] <= res["old"]["t10"]
    res["kept_new"] = keep
    if not keep:
        shutil.copy2(bak / PKL.name, PKL)
        shutil.copy2(bak / MET.name, MET)
    (MODELS / "exp_p10_clean_results.json").write_text(json.dumps(res, indent=2))
    print("[p10]", json.dumps(res))


H = 5
if __name__ == "__main__":
    main()
