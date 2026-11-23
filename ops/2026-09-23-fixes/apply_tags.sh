#!/bin/bash
# Add the 43 extra Kiln-3 OPC tags to the live feed (schema first, then code, then restart).
set -e
cd /home/antpc/CCR-Project
.venv/bin/python /home/antpc/patch_tags_20260923.py
.venv/bin/python -m py_compile etl/live_feed.py && echo COMPILE_OK
find etl -name "*.pyc" -delete
pm2 restart ccr-livefeed --update-env >/dev/null && echo livefeed_restarted
sleep 25
echo "=== live_now: new tags with current values (None = not resolved on server) ==="
.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("data/digital_twin.sqlite", timeout=30)
row = c.execute("SELECT * FROM live_now").fetchone()
cols = [d[0] for d in c.execute("SELECT * FROM live_now").description]
d = dict(zip(cols, row))
new = ["467RL400_SPSPM","423CI400_PT_4","423AM823_NO","433MD140_JT1","443KH050_PT","353_CLNK_TPH","443CC100_P1","463WF035_TPH"]
for k in new: print(f"  {k:18s} {d.get(k)}")
print("live_now columns:", len(cols), "| process_live rows:", c.execute("SELECT COUNT(*) FROM process_live").fetchone()[0])
PY
pm2 logs ccr-livefeed --lines 3 --nostream | tail -3
echo DONE
