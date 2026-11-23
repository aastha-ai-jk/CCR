# CCR advisor + twin → GPU box

Deploys branch `advisory-twin-changes` onto the deployed tree
`antpc@10.53.2.10:/home/antpc/CCR-Project` (not a git checkout). The server is
expected to run `6763a01` (`feature/digital-twin-ml`); the pre-flight check enforces it
file by file. `build_bundle.py` takes every app file changed since `6763a01`.

What it brings (see the branch log for detail):
- **Advisor** `/advisor`: live values (2 s), T+5 forecast + 5/10/15/30 min grid, recommendations
  for the 4 calciner fuel levers (kiln coal, kiln feed, PH fan, kiln speed held fixed),
  decisions saved to `operator_decisions`, clinker-quality guard from the latest lab result.
- **Twin** `/twin`: runs 5 min ahead, applies accepted setpoints, forecast-by-horizon and
  operator-decisions panels.
- **Model**: realistic fuel response for what-if predictions (measured from plant history);
  `train_gbt` NameError fix (needed by the Sunday `ccr-retrain`); free-lime label 2 h after PLC.
- **Settings**: `data/plant_settings.json` (all `null` = today's behaviour).
- **ETL**: clinker loader reads Kiln 3 (CLK-3); `--only clinker [--clinker-csv]` reload.

Not deployed: the database, `models/*` (the server keeps its own model and metrics).

## Run (from this folder, on a machine that can reach 10.53.2.10)
Must run **as `antpc`** (the app's owner and PM2 user).
```
python build_bundle.py
scp dist/ccr-advisor-<date>.tar.gz antpc@10.53.2.10:~/
ssh -t antpc@10.53.2.10 'tar xzf ccr-advisor-<date>.tar.gz && bash -n ccr-advisor-<date>/apply_advisor.sh && bash ccr-advisor-<date>/apply_advisor.sh'
```
`apply_advisor.sh`: pre-flight hash check (aborts without changing anything if a server file
was edited there) → backup `*.bak-<ts>` → install → compile + import test → `npm run build` →
restart **only** `ccr-backend` + `ccr-frontend` → health checks. Any failure after install
rolls back automatically. Manual rollback: `bash ccr-advisor-<date>/rollback_advisor.sh`.

`ccr-shadow`, `ccr-optimizer` and the cron jobs pick up the new model code at their next
restart (`pm2 restart ccr-shadow ccr-optimizer` when convenient).

After: advisor `http://10.53.2.10:8010/advisor`, twin `http://10.53.2.10:3010/twin`.
The frontend is briefly unavailable during `npm run build` (≈1–2 min).
