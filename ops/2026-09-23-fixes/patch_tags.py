"""Add 43 more Kiln-3 pyro tags (all already exposed by KEPServer) to the live feed.

Safety: etl/live_feed.write_snapshot used to fall back to to_sql(if_exists="replace")
when the column set changed, which DROPS the whole process_live history. This patch
(1) adds the missing columns with ALTER TABLE first, (2) replaces that fallback with
an ALTER-based one, (3) appends EXTRA_PYRO to the tag list read every 2 s.
Nothing here touches process_1min or the model — capture first, evaluate later.
"""
import shutil
import sqlite3
import time
from pathlib import Path

ROOT = Path("/home/antpc/CCR-Project")
DB = ROOT / "data" / "digital_twin.sqlite"
TS = time.strftime("%Y%m%d-%H%M%S")

EXTRA_PYRO = [
    # calciner / preheater: setpoints, drafts, NOx, PH fan, cyclone levels
    "467RL400_SPSPM", "467RL300_SPSPM",
    "423CI400_PT_2", "423CI400_PT_3", "423CI400_PT_4",
    "423CN150_PT_1", "423CN250_PT_1", "423CN140_PT_1", "423CN240_PT_1", "423CN240_PT_2",
    "423CN110_PT_1", "423CN210_PT_1", "423CN_PT",
    "423DU415_PT", "423DU415_PT1", "423DU415_TT1",
    "423AM823_NO",
    "423FN010_JT", "423FN010_SPSPM", "423FN010_PT_1", "423FN010_PT_2",
    "423CN150_LS", "423CN250_LS", "423CN140_LS", "423CN240_LS",
    # kiln: drive current / power / speed SP, shell pyrometer, hood pressure
    "433MD140_IT1", "433MD140_JT1", "433MD140_SPC", "433MD140A_SPSPM",
    "433KL100_BT_02", "443KH050_PT",
    # production / feed setpoint (M&V needs clinker rate)
    "353RL130_KF_TPH", "353_CLNK_TPH",
    # cooler: undergrate pressure, grate speed, exhaust temp, analyser, vent fan
    "443CC100_P1", "443CC100_SP1SPM", "443CC100_TT3", "443AM597", "443FN590_ST",
    # coal mill: raw coal feeders + mill outlet temperature (coal moisture proxy)
    "463WF035_TPH", "463WF045_TPH", "463RM100_TT2",
]

# 1) schema first: add columns so the feed never hits the destructive fallback
if DB.exists():
    con = sqlite3.connect(DB, timeout=30)
    have = {r[1] for r in con.execute("PRAGMA table_info(process_live)")}
    added = 0
    for t in EXTRA_PYRO:
        if t not in have:
            con.execute(f'ALTER TABLE process_live ADD COLUMN "{t}" REAL')
            added += 1
    con.commit()
    n0 = con.execute("SELECT COUNT(*) FROM process_live").fetchone()[0]
    con.close()
    print(f"process_live: +{added} columns, rows preserved = {n0:,}")
else:
    print("no sqlite DB at", DB, "- schema step skipped (code-only patch)")

# 2) code patch
p = ROOT / "etl" / "live_feed.py"
src = p.read_text()
anchor_list = '    "423CN250_TT_2",\n]\n'
anchor_loop = '            for code in EXTRA_CYCLONE:                       # per-cyclone temps\n'
anchor_fallback = (
    '    try:\n'
    '        hist.to_sql("process_live", engine, if_exists="append", index=False)\n'
    '    except Exception:\n'
    '        # column set changed (e.g. cyclone temps added) -> recreate history table\n'
    '        hist.to_sql("process_live", engine, if_exists="replace", index=False)\n'
)
for a in (anchor_list, anchor_loop, anchor_fallback):
    assert src.count(a) == 1, f"anchor not found/unique: {a[:60]!r}"
lst = "EXTRA_PYRO = [\n" + "".join(f'    "{t}",\n' for t in EXTRA_PYRO) + "]\n"
src = src.replace(anchor_list, anchor_list +
    "\n# 2026-09-23: more Kiln-3 pyro tags found on the OPC server (setpoints, drafts,\n"
    "# NOx, kiln drive, hood pressure, clinker rate, cooler, coal mill). Captured for\n"
    "# evaluation; NOT yet in process_1min / the model.\n" + lst)
src = src.replace(anchor_loop,
    '            for code in EXTRA_CYCLONE + EXTRA_PYRO:          # extra server tags\n')
src = src.replace(anchor_fallback,
    '    try:\n'
    '        hist.to_sql("process_live", engine, if_exists="append", index=False)\n'
    '    except Exception:\n'
    '        # 2026-09-23: column set changed -> ADD the new columns; never "replace"\n'
    '        # (that dropped the whole live history when cyclone temps were added).\n'
    '        with engine.begin() as con:\n'
    '            have = {r[1] for r in con.exec_driver_sql("PRAGMA table_info(process_live)")}\n'
    '            for c in hist.columns:\n'
    '                if c not in have:\n'
    '                    con.exec_driver_sql(f\'ALTER TABLE process_live ADD COLUMN "{c}" REAL\')\n'
    '        hist.to_sql("process_live", engine, if_exists="append", index=False)\n')
shutil.copy2(p, p.with_name(p.name + f".bak-{TS}"))
p.write_text(src)
print("patched etl/live_feed.py ->", p.name + f".bak-{TS}")
