# Calcination Combustion Control — Digital Twin

Digital twin + optimization console for JK Cement Kiln-3 precalciner.
Full plan and parameter spec: [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Layout
```
config.py            # single source of truth for DB URL + data paths + domain constants
etl/                 # data foundation (Phase 0)
  tags.py            #   build tag dictionary (31 tags, roles, engineering ranges)
  clean.py           #   robust cleaning (timestamp fix, range flagging, lab parsing)
  load.py            #   orchestrate: clean 4 sources -> DB tables
backend/             # FastAPI API (Phase 1)   [next]
frontend/            # Next.js app (Phase 1)   [next]
docs/ROADMAP.md      # roadmap + parameter targeting + twin design
```

## Setup
```powershell
# 1. deps (data layer already installed globally: pandas, sqlalchemy, psycopg, dotenv)
python -m pip install -r requirements.txt

# 2. configure DB — edit .env:
#    set DATABASE_URL password and USE_SQLITE=0 for Postgres,
#    or leave USE_SQLITE=1 to run on the local SQLite bootstrap file.

# 3. ingest all four data sources
python -m etl.load
```

## Database tables (after `etl.load`)
| table | rows | purpose |
|-------|------|---------|
| `dim_tag` | 31 | tag dictionary: name, unit, eng. range, control_role, subsystem, OPC address |
| `process_1min` | ~493k | wide 1-min process history (Aug 2025 – Jul 2026) + `feed_gate` |
| `data_quality` | 31 | per-tag validity %, out-of-range counts, min/max/mean |
| `lab_hotmeal` | ~3.4k | eLIMS hotmeal XRF (DOC, Cl, SO₃, …), sides A/B |
| `lab_clinker` | 239 | clinker QC (free lime, liter wt, C3S, liquid, …) |
| `roadmap_milestone` | 6 | P0–P5 phase tracker (drives the app's Roadmap page) |

## Run the app
```powershell
# 1. backend API (from repo root) — serves the DB on :8000
python -m uvicorn backend.main:app --port 8000

# 2. LIVE feed — streams the plant OPC server into the DB every 2 s
#    (dashboards then update in real time; needs plant-network access)
python -m etl.live_feed --interval 2
#    off the plant network? stream recent history forward instead:
python -m etl.live_feed --replay --interval 2

# 3. frontend (in a third terminal)
cd frontend
npm run dev            # http://localhost:3000
```
Live Overview and Digital Twin poll every 2.5 s and show a **LIVE · OPC** badge
with the feed age. If the feed isn't running (or is stale >120 s), they fall back
to the newest historical snapshot automatically.
Open http://localhost:3000 — five modules: Live Overview, Roadmap, Data Explorer,
Correlation Studio, Twin Simulator. Frontend talks to the API via
`frontend/.env.local` (`NEXT_PUBLIC_API_BASE`).

## Moving to PostgreSQL
Create the DB in PgAdmin4 (`digital_twin`), put the password in `.env`, set `USE_SQLITE=0`,
re-run `python -m etl.load`. No code changes — everything is driven by `DATABASE_URL`.

## `uat` branch: ML twin trained on 1-second PLC data

This branch adds a real trained model to `backend/analysis.simulate()`, alongside
the original kNN + cardinal-relationship surrogate.

```powershell
# 1. refresh process_1min from a 1-second-resolution PLC export (finer-grained
#    and more current than the monthly PLC_DIR .xlsx files; set CSV_1S_PATH in .env)
python -m etl.load_csv_1s

# 2. train the ML twin: LightGBM model of calciner-temp CHANGE over DELTA
#    minutes, on EXACTLY the features simulate() has at inference time
#    (current temp, trend, 3 PLC state vars, 8 levers) — see etl/train_gbt_twin.py
python -m etl.train_gbt_twin
# -> backend/models/gbt_twin_dtemp.txt, backend/models/gbt_twin_meta.json

# 3. run the app as usual — simulate() picks up the trained model automatically
python -m uvicorn backend.main:app --port 8000
```

**Honest result from the first UAT training run:** the twin's held-out test MAE
(7.04 °C) came in essentially tied with a trivial "predict no change" baseline
(7.036 °C, −0.1%). An unconstrained variant reached +1.0%. Feature-importance
showed `temp_now` + `temp_trend` carry 92% of the signal; the 8 real control
levers combined contribute under 8%. This isn't a tuning artifact — it's a
real limit of training on point-in-time features only: `simulate()`'s what-if
API only ever has the *current instant* for a hypothetical lever setting (no
recent trajectory), so the model can't use the lag/rolling-window context that
made the earlier experiments in `../nn_twin/` (a standalone sandbox, not part
of this app) accurate. The kNN surrogate sidesteps this by borrowing real
historical trajectories from matched neighbours instead.

**`simulate()` is gated on demonstrated skill, not on the model merely
existing** (`backend/analysis.GBT_MIN_IMPROVEMENT_PCT`, default 2%): if the
trained twin doesn't beat the persistence baseline by that margin, `simulate()`
automatically falls back to the kNN surrogate — same response shape either way,
with `"method"` telling you which one served the prediction and
`"surrogate_backup"` always carrying the kNN estimate for comparison. The
integration code path itself is verified working (correct feature order,
monotonic in the levers, no exceptions) — it's simply inactive until a trained
model clears the bar.

**Path to a twin that actually clears the bar:** extend `simulate()`'s request
payload to accept a short recent-trajectory window (not just point-in-time
values) so the model can use lag/rolling features the way the sandbox
experiments did — or blend the GBT prediction with the kNN neighbourhood
estimate rather than treating them as a strict either/or.
