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
by a LightGBM model trained in the `../nn_twin/` sandbox
(`nn_twin/plc_pipeline_delta.py`), predicting calciner outlet temperature 60 s
ahead. **Current design: DELTA target + momentum features** — see "Final
result" below; earlier absolute-value versions are kept as history further
down since the debugging trail (leakage → causal fix → regime bias → delta
fix) is itself useful context for anyone extending this.

```powershell
# 1. refresh process_1min from a 1-second-resolution PLC export (finer-grained
#    and more current than the monthly PLC_DIR .xlsx files; set CSV_1S_PATH in .env)
python -m etl.load_csv_1s

# 2. run the app — the trained model (backend/models/gbt_delta_60s.txt) is
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

**Algorithm choice: GBT (LightGBM), not a neural net.** Every experiment run
in this project — this sandbox, and independently in a colleague's
`feature/digital-twin-ml` branch — found CNN-LSTM/GRU to score notably worse
than GBT (in one bake-off, worse than a "predict no change" baseline
entirely), consistent with published results on tabular data (Grinsztajn et
al., NeurIPS 2022). A neural net was tried here too and dropped for the same
reason.

**Final result — delta target closes the regime-drift gap:** an earlier
absolute-value GBT (still in git history as `gbt_forecast_60s.txt` /
`gbt_forecast_meta.json`) reached 5.69 °C on held-out test after fixing a
separate causal-smoothing leak, but showed a *same-direction,
different-magnitude* bias across two independent real backtests (8.29 °C on
a volatile morning vs 3.66 °C on a calm afternoon) — the signature of a model
that implicitly encodes "what operating regime was I trained in" via
absolute temperature level. `feature/digital-twin-ml` hit this exact failure
mode with absolute-target models (scored *worse than persistence*) and fixed
it by predicting the temperature **change**, using **momentum** features
(deviation-from-now) for the target's own history instead of absolute levels.
Applying the same fix here (`nn_twin/plc_pipeline_delta.py` /
`backend/models/gbt_delta_meta.json`):

| | Held-out test (training) | Live backtest, 2026-08-10 06:00–12:00 (volatile) | Live backtest, 2026-08-05 12:00–18:00 (calm) |
|---|---|---|---|
| **Delta-target GBT** | **2.53 °C** (RMSE 3.39, R² 0.95) | **2.61 °C** (bias −0.88) | **1.99 °C** (bias +0.03) |
| Prior absolute-value GBT | 5.69 °C | 8.29 °C (bias −7.61) | 3.66 °C (bias −3.02) |
| Persistence baseline | 3.52 °C | — | — |

Both the training number and both independent live backtests now agree
closely (~2–2.6 °C), **comfortably inside the ±5 °C target**, and — the more
important change — the regime-dependent bias is gone (both windows near
zero, versus −7.6/−3.0 before). This is verified with `python -m
etl.backtest_forecast` against real historical data through this repo's own
serving code path (`backend.forecast.forecast_from_recent`), not just a
training-time number. Full detail, including *why* momentum features fix
this (deviation-from-now is regime-invariant; absolute lags/rolling-means
aren't), in `backend/models/gbt_delta_meta.json`.

**Known gap:** `process_live`'s OPC mapping doesn't carry 2 of the 31 tags
(`423CN150_TT_1`, `K3V10_F1`); `forecast.py` imputes them from their
`process_1min` long-run median and flags this in every response's
`imputed_tags` field.

**Live in the app:** the Twin Simulator page (`/simulator`) shows this
forecast in its own panel, polling `/api/forecast` every 5 s, independent of
the what-if lever sliders (which still use the original kNN surrogate) — see
`frontend/app/simulator/page.tsx`.

<details>
<summary><strong>History: how this model evolved (leakage → causal fix → regime bias → delta fix)</strong></summary>

1. **v1 (absolute value, non-causal smoothing):** training reported 5.73 °C
   MAE, but used a *centered* Savitzky-Golay filter that smoothed "now" using
   both past **and future** samples — impossible for live serving to
   reproduce. An independent backtest found the real live accuracy was
   ~7–9 °C.
2. **v2 (absolute value, causal smoothing):** replaced the centered filter
   with a trailing-only rolling mean in both training and serving, so
   training's evaluation matched what serving could actually compute.
   Retrained test MAE: 5.69 °C. But live backtests revealed a
   same-direction, different-magnitude bias (8.29 °C vs 3.66 °C across two
   windows) — a *regime-drift* problem, not a leakage problem.
3. **v3 (delta target, momentum features) — current:** predicts temperature
   *change*, using deviation-from-now instead of absolute level for the
   target's own history. Test MAE 2.53 °C, live backtests 2.61/1.99 °C, bias
   near zero on both. See "Final result" above.

A fixed bias correction was tried on v2 (applying one window's bias as a
constant offset to the other) and made things *worse*, confirming the bias
was regime-dependent, not a stable constant — this is *why* v3's structural
fix (removing the regime leak from the features) was the right move rather
than trying to patch v2 with a correction term.
</details>
