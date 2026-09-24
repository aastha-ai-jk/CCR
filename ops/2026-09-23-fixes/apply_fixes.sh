#!/bin/bash
# Apply the 2026-09-23 CCR twin bug fixes on the GPU box.
# Backs up each edited file as *.bak-<timestamp>, compiles, smoke-tests, restarts the monitor cron entry.
set -e
cd /home/antpc/CCR-Project
.venv/bin/python /home/antpc/patch_fixes_20260923.py
.venv/bin/python -m py_compile twin/train_gbt.py twin/retrain.py twin/monitor.py && echo COMPILE_OK
find twin -name "*.pyc" -delete
echo "=== monitor smoke test (appends one line to models/monitor_log.jsonl) ==="
.venv/bin/python -m twin.monitor 2>&1 | grep -v Warning | tail -6
echo "=== retrain skip test (expect: skipped, last run < 5 days ago) ==="
.venv/bin/python -m twin.retrain --no-reload 2>&1 | grep -v Warning | tail -2
pm2 restart ccr-monitor --update-env >/dev/null && echo monitor_restarted
echo DONE
