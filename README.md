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
by a LightGBM model trained in the `../nn_twin/` sandbox: lag (1/5/10/60 s) +
rolling-window (5-min mean/std/skew + EMA) features across all 31 PLC tags,
predicting absolute calciner outlet temperature 60 s ahead. **CAUSAL smoothing**
(trailing-only rolling mean, `CFG.smooth_window`), not the centered filter an
earlier version used — see "Honest result" below for why that distinction
matters and what it changed.

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

**Honest result — the causal retrain, and what it fixed:** an earlier version
of this model was trained with *centered* (non-causal) Savitzky-Golay
smoothing on every tag, including the target — training data deep in a
multi-week series can smooth "now" using both past **and future** neighbours.
Live inference only ever has the past, so that model's reported 5.73 °C MAE
never held up live (an independent backtest found ~7–9 °C — see git history
of this section for the full before/after). Both `nn_twin/plc_pipeline.py`
(training) and `backend/forecast.py` (serving) now use the **same causal,
trailing-only rolling mean** instead, so training's evaluation is no longer
measuring something serving structurally can't reproduce.

**Result: retrained held-out test MAE = 5.69 °C** (RMSE 8.04, R² 0.70, `n≈58k`,
purged 5-fold walk-forward CV — see `nn_twin/out/Model_Evaluation_Report.md`).
Independently verified with `python -m etl.backtest_forecast` (this repo's own
code, run on two real historical windows, scored against a causally-denoised
actual):

| Window | n | MAE | RMSE | Bias |
|---|---|---|---|---|
| 2026-08-10 06:00–12:00 (volatile) | 72 | 8.29 °C | 11.42 | −7.61 |
| 2026-08-05 12:00–18:00 (calm) | 72 | 3.66 °C | 4.81 | −3.02 |
| **combined average** | 144 | **~5.98 °C** | — | — |

The causal fix closed most of the training/serving gap — the two numbers are
now measuring the same thing, and the combined empirical accuracy (~6 °C) is
close to the training-reported 5.69 °C, versus the old ~7–9 °C live range.
**But accuracy is genuinely regime-dependent**: ~3.7 °C on a calm afternoon,
~8.3 °C during a more volatile morning. Both windows show a negative bias
(predictions run low) in the same *direction* but different *magnitude* — the
same non-stationary-drift pattern found elsewhere in this repo (the LSTM soft
sensor in `../nn_twin/`, the retired point-in-time GBT twin in
`backend/analysis.py`). A fixed bias correction was tried previously and made
an out-of-sample window worse, so **no bias correction is baked into
`backend/forecast.py`**. Treat **~5–6 °C on typical periods, up to ~8 °C
during faster-moving regimes** as the honest range — not a single guaranteed
number. Full detail in `backend/models/gbt_forecast_meta.json`.

**Known gap:** `process_live`'s OPC mapping doesn't carry 2 of the 31 tags
(`423CN150_TT_1`, `K3V10_F1`); `forecast.py` imputes them from their
`process_1min` long-run median and flags this in every response's
`imputed_tags` field.

**Live in the app:** the Twin Simulator page (`/simulator`) shows this
forecast in its own panel, polling `/api/forecast` every 5 s, independent of
the what-if lever sliders (which still use the original kNN surrogate) — see
`frontend/app/simulator/page.tsx`.
