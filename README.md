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
