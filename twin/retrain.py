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
GUARD_SLACK = 1.15          # new test MAE may be at most 15% above the old


def _mae() -> float | None:
    try:
        return float(json.loads(MET.read_text())["temp"]["mae"])
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-reload", action="store_true")
    args = ap.parse_args()

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
    train_gbt.train(None)
    new_mae = _mae()
    entry["new_mae"] = new_mae

    promoted = True
    if old_mae is not None and new_mae is not None and new_mae > old_mae * GUARD_SLACK:
        shutil.copy2(PREV / PKL.name, PKL)          # restore previous model
        shutil.copy2(PREV / MET.name, MET)
        promoted = False
        print(f"[retrain] GUARD TRIPPED: new {new_mae} > {old_mae}*{GUARD_SLACK} "
              "— previous model restored")
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
