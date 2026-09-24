# 2026-09-23 fixes — deploy to the GPU box

The deployed tree (`antpc@10.53.2.10:/home/antpc/CCR-Project`) is not a git checkout,
so code changes are applied there with these idempotent patch scripts (each edited
file is backed up as `*.bak-<timestamp>`). The same patches are already applied to
this repository, so GitHub is the source of truth.

| script | what it does |
|---|---|
| `patch_fixes.py` | `train_gbt`: cap LightGBM at 20 threads (retrain took ~9 h from OpenMP oversubscription) and expose the t+5 test fold. `retrain`: same-window guard (old model re-scored on the new test rows, ≤2% worse) and skip runs <5 days apart (PM2 resurrect relaunches the job on every boot; `--force` overrides). `monitor`: accuracy vs the 3-min-centred 1-min-mean truth the model is trained on. |
| `patch_tags.py` | Adds 43 more Kiln-3 OPC tags to the live feed (coal setpoints, calciner/cyclone/TAD/hood pressures, calciner NOx, PH fan power, kiln drive, clinker rate, cooler, coal mill). Adds the columns with `ALTER TABLE` first and replaces the `to_sql(if_exists="replace")` fallback that used to drop the whole `process_live` history. |
| `apply_fixes.sh` / `apply_tags.sh` | Run on the box: patch → compile → smoke test → restart the affected PM2 app. |

```
cd ops/2026-09-23-fixes
scp patch_fixes.py antpc@10.53.2.10:~/patch_fixes_20260923.py
scp patch_tags.py  antpc@10.53.2.10:~/patch_tags_20260923.py
scp apply_fixes.sh antpc@10.53.2.10:~/apply_fixes_20260923.sh
scp apply_tags.sh  antpc@10.53.2.10:~/apply_tags_20260923.sh
ssh -t antpc@10.53.2.10 'bash ~/apply_fixes_20260923.sh && bash ~/apply_tags_20260923.sh'
```

Background: `models/exp_recent*_results.json` (25 controlled experiments, Sep 2026) show the
t+5 twin is at its information ceiling (~30% skill vs persistence) on the current volatile
regime; new plant signals, not model changes, are the route to lower MAE.
