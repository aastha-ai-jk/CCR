# 2026-09-25 — CCR advisor + operator decision log → GPU box

Deploys commit `066a5b3` (`feature/ccr-advisor`) onto the deployed tree
`antpc@10.53.2.10:/home/antpc/CCR-Project` (not a git checkout).

| file | change |
|---|---|
| `backend/main.py` | `/advisor` page, `/api/twin/forecast`, `/api/twin/advisor` (8-lever, background, ≤ every 60 s), `/api/decisions`, `/api/decisions/days`; `live_now` read retry |
| `backend/decisions.py` | new — `operator_decisions` table (created on first use), date-wise |
| `backend/static/ccr-kiln3-advisor.html` | new — advisor screen |
| `twin/optimize.py` | optional `levers` arg; default = the 4 fuel levers, so `ccr-shadow` / `ccr-optimizer` behave exactly as before |
| `twin/train_gbt.py` | **fix**: `_THREADS` used before definition (NameError on import since the 2026-09-23 patch) — the Sunday `ccr-retrain` needs this |
| `frontend/app/twin/page.tsx`, `frontend/lib/api.ts` | "CCR operator decisions" panel on the Digital Twin page |

Not deployed: the database, `models/*` (the server keeps its own model and metrics).

## Run (from this folder, on a machine that can reach 10.53.2.10)
```
python build_bundle.py 066a5b3
scp dist/ccr-advisor-20260925.tar.gz antpc@10.53.2.10:~/
ssh -t antpc@10.53.2.10 'tar xzf ccr-advisor-20260925.tar.gz && bash ccr-advisor-20260925/apply_advisor.sh'
```
`apply_advisor.sh`: pre-flight hash check (aborts without changing anything if a server file
was edited there) → backup `*.bak-<ts>` → install → compile + import test → `npm run build` →
restart **only** `ccr-backend` + `ccr-frontend` → health checks. Any failure after install
rolls back automatically. Manual rollback: `bash ccr-advisor-20260925/rollback_advisor.sh`.

After: advisor `http://10.53.2.10:8010/advisor`, twin `http://10.53.2.10:3010/twin`.
The frontend is briefly unavailable during `npm run build` (≈1–2 min).
