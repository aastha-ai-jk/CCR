"""Guarded weekly retrain: ingest live data, retrain, promote only if not worse.

Pipeline (run weekly by PM2 cron, or manually):
  1. ingest_live  — fold matured live-feed minutes into the training historian
  2. backup       — current model + metrics -> models/prev/
  3. retrain      — full twin.train_gbt on the extended historian
  4. GUARD        — the new model must not be >15% worse on ITS held-out test
                    than the previous model was on its own (the test window
                    shifts to the newest, hardest regime, so modest slack).
                    If worse: previous model is restored, event logged.
  5. reload       — restart serving + shadow so the promoted bundle is live
Every run appends one JSON line to models/retrain_log.jsonl.

Run: python -m twin.retrain [--no-reload]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path

MODELS = Path(__file__).resolve().parent.parent / "models"
PKL = MODELS / "twin_gbt.pkl"
MET = MODELS / "metrics.json"
PREV = MODELS / "prev"
LOG = MODELS / "retrain_log.jsonl"
GUARD_SLACK = 1.15          # legacy rule (different windows): <=15% worse
SAME_WINDOW_SLACK = 1.02    # 2026-09-23: old vs new scored on the SAME test rows
MIN_GAP_DAYS = 5            # PM2 resurrect re-launches this job on every reboot


def _last_run() -> dt.datetime | None:
    try:
        last = LOG.read_text().strip().splitlines()[-1]
        return dt.datetime.fromisoformat(json.loads(last)["ts"])
    except Exception:
        return None


def _score_prev_same_window(test: dict) -> float | None:
    """Previous model's served-mean MAE on the NEW model's test rows (or None)."""
    try:
        import joblib
        import numpy as np
        from twin import features as F
        b = joblib.load(PREV / PKL.name)
        if list(b["feature_columns"]) != test["feature_columns"]:
            return None                      # features changed -> not comparable
        hb = b["temp_multi"][F.HORIZON_MIN]
        w = float(hb["arx"]["w"])
        gbt = hb["mean"].predict(test["x"])
        arx = test["x"].to_numpy(float) @ hb["arx"]["coef"] + hb["arx"]["intercept"]
        pred = (1 - w) * gbt + w * arx + test["cv"]
        return round(float(np.mean(np.abs(pred - test["y_raw"]))), 3)
    except Exception as e:                   # never let the guard itself crash
        print(f"[retrain] same-window scoring unavailable: {e}")
        return None


def _mae() -> float | None:
    try:
        return float(json.loads(MET.read_text())["temp"]["mae"])
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-reload", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="run even if the last retrain was < MIN_GAP_DAYS ago")
    args = ap.parse_args()

    last = _last_run()
    if last and not args.force and (dt.datetime.now() - last).days < MIN_GAP_DAYS:
        print(f"[retrain] skipped: last run {last.isoformat(timespec='minutes')} "
              f"< {MIN_GAP_DAYS} days ago (boot resurrect / manual re-run; use --force)")
        return

    entry: dict = {"ts": dt.datetime.now().isoformat(timespec="seconds")}

    from twin import ingest_live
    entry["ingested_minutes"] = ingest_live.run()

    old_mae = _mae()
    entry["old_mae"] = old_mae
    PREV.mkdir(exist_ok=True)
    if PKL.exists():
        shutil.copy2(PKL, PREV / PKL.name)
        shutil.copy2(MET, PREV / MET.name)

    from twin import train_gbt
    test: dict = {}
    train_gbt.train(None, test_out=test)
    new_mae = _mae()
    entry["new_mae"] = new_mae

    # Same-window comparison: the old model re-scored on the new test rows, so
    # the guard judges the MODEL, not the regime shift of a moving test window.
    old_same = _score_prev_same_window(test) if test else None
    entry["old_mae_same_window"] = old_same
    try:
        entry["persistence_mae"] = json.loads(MET.read_text())["baseline_persistence"]["temp"]["mae"]
    except Exception:
        pass
    if old_same is not None:
        worse = new_mae is not None and new_mae > old_same * SAME_WINDOW_SLACK
        entry["guard"] = "same_window"
    else:
        worse = old_mae is not None and new_mae is not None and new_mae > old_mae * GUARD_SLACK
        entry["guard"] = "legacy_15pct"

    promoted = True
    if worse:
        shutil.copy2(PREV / PKL.name, PKL)          # restore previous model
        shutil.copy2(PREV / MET.name, MET)
        promoted = False
        print(f"[retrain] GUARD TRIPPED ({entry['guard']}): new {new_mae} vs old "
              f"{old_same if old_same is not None else old_mae} — previous model restored")
    entry["promoted"] = promoted

    if promoted and not args.no_reload:
        try:
            subprocess.run(["pm2", "restart", "ccr-backend", "ccr-shadow"],
                           check=False, capture_output=True, timeout=60)
            entry["reloaded"] = True
        except Exception:
            entry["reloaded"] = False

    with LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[retrain] {entry}")


if __name__ == "__main__":
    main()
