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

## `uat` branch: GBT forecast twin, trained on 1-second PLC data

This branch adds a new ML forecast capability — `GET /api/forecast` — powered
by the LightGBM model validated at **MAE 5.73 °C** (held-out test, `n≈58k`)
in the `../nn_twin/` sandbox: lag (1/5/10/60 s) + rolling-window (5-min
mean/std/skew + EMA) features across all 31 PLC tags, predicting absolute
calciner outlet temperature 60 s ahead.

```powershell
# 1. refresh process_1min from a 1-second-resolution PLC export (finer-grained
#    and more current than the monthly PLC_DIR .xlsx files; set CSV_1S_PATH in .env)
python -m etl.load_csv_1s

# 2. run the app — the trained model (backend/models/gbt_forecast_60s.txt) is
#    already in the repo, nothing to train
python -m uvicorn backend.main:app --port 8000
python -m etl.live_feed --interval 2      # or --replay off the plant network
curl http://localhost:8000/api/forecast

# 3. (optional) independently validate the deployed model + feature code
#    against real historical 1-second data, using this repo's own code path:
python -m etl.backtest_forecast --hours 6 --end "2026-08-10 12:00:00"
```

**Why this is a separate capability from `backend/analysis.simulate()`,
not a replacement for it:** `simulate()` answers a hypothetical "what if I set
these levers to X" query, so it only ever has the *current instant* to work
from — there's no real trajectory for a lever setting that hasn't happened.
This model needs the opposite: real recent per-second history (lags, rolling
stats) is exactly what makes it accurate, so it can only run against **real**
trailing data — the live feed, or a backtest — not a what-if. `simulate()`
still uses the original kNN + cardinal-relationship surrogate, unchanged.
See `backend/forecast.py`'s module docstring for the full reasoning (an
earlier attempt to force a point-in-time-only GBT into `simulate()` scored
essentially tied with a "predict no change" baseline — retired in favour of
this design).

**Honest result — training MAE vs. live-serving accuracy:** the 5.73 °C figure
was measured with *centered* (non-causal) Savitzky-Golay smoothing on the
target — training data deep in a multi-week series can smooth "now" using
both past **and future** neighbours. Live inference only ever has the past, so
`python -m etl.backtest_forecast` (this repo's own code, run twice on
independent real historical windows) measured:

| Window | n | MAE | RMSE | Bias |
|---|---|---|---|---|
| 2026-08-10 06:00–12:00 | 72 | 9.19 °C | 12.20 | −8.61 |
| 2026-08-05 12:00–18:00 | 36 | 2.81 °C | 4.25 | −2.04 |
| **combined** | 108 | **~7.06 °C** | — | — |

The bias is real but **not a stable constant** — applying window 1's −8.61 °C
correction to window 2 made it *worse* (2.81 → 7.05 °C), the same
non-stationary-drift failure mode already found with the LSTM soft sensor in
`../nn_twin/`. So **no bias correction is baked into `backend/forecast.py`** —
treat 5.73 °C as a best-case, centered-smoothing number, and ~7–9 °C as the
honest live-serving range. Full detail in `backend/models/gbt_forecast_meta.json`.

**Known gap:** `process_live`'s OPC mapping doesn't carry 2 of the 31 tags
(`423CN150_TT_1`, `K3V10_F1`); `forecast.py` imputes them from their
`process_1min` long-run median and flags this in every response's
`imputed_tags` field.

**Path to closing the training/serving gap:** retrain with one-sided
(causal) smoothing instead of centered, so training evaluation matches what
live inference can actually compute — or add an online bias tracker (rolling
recent-error correction) rather than a fixed constant.
