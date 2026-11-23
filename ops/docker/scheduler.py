"""Docker replacement for the PM2 cron entries of the CCR twin.

PM2 ran   ccr-monitor  "0 */6 * * *"   (every 6 h on the hour)
          ccr-retrain  "0 2 * * 0"     (Sunday 02:00)
This loop runs the same modules at the same plant-local times (TZ is set in
the image), each in its own process so a failure never stops the scheduler.
The retrain's own guard (>= 5 days apart, promote only if not worse) still
applies; serving reloads the new model by itself (twin.model checks the file).

    python ops/docker/scheduler.py            # run forever
    python ops/docker/scheduler.py --once monitor|retrain
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
import time

JOBS = {
    # name: (module, due(now) -> bool, slot key so a job runs once per slot)
    "monitor": ("twin.monitor", lambda n: n.hour % 6 == 0 and n.minute == 0,
                lambda n: n.strftime("%Y-%m-%d %H")),
    "retrain": ("twin.retrain", lambda n: n.weekday() == 6 and n.hour == 2 and n.minute == 0,
                lambda n: n.strftime("%Y-%m-%d")),
}


def run(name: str) -> int:
    module = JOBS[name][0]
    print(f"[scheduler] {dt.datetime.now():%Y-%m-%d %H:%M:%S} start {name} ({module})", flush=True)
    rc = subprocess.run([sys.executable, "-m", module], check=False).returncode
    print(f"[scheduler] {dt.datetime.now():%Y-%m-%d %H:%M:%S} end {name} rc={rc}", flush=True)
    return rc


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--once":
        sys.exit(run(sys.argv[2]))
    done: dict[str, str] = {}
    print("[scheduler] up: monitor every 6 h, retrain Sunday 02:00", flush=True)
    while True:
        now = dt.datetime.now()
        for name, (_, due, slot) in JOBS.items():
            if due(now) and done.get(name) != slot(now):
                done[name] = slot(now)
                run(name)
        time.sleep(20)


if __name__ == "__main__":
    main()
