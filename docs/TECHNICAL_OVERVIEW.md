# Kiln-3 Digital Twin — Complete Technical Overview

> **Purpose of this document:** a single, self-contained technical briefing for the
> JK Cement Kiln-3 Digital Twin project ("Calcination Combustion Control"). Upload it
> to any AI assistant or hand it to a new engineer and they should be able to reason
> about, extend, or review the system without any other context.
> Status date: 2026-08-23. No credentials are contained in this file.

---

## 1. What the project is

An **advisory-first digital twin** of the pre-calciner on **Kiln 3 at JK Cement's
Nimbahera plant** (Rajasthan, India). It runs 24/7 on an on-premise GPU server,
mirrors the live plant, **forecasts calciner outlet temperature** at multiple horizons,
recommends fuel setpoints (coal + alternative fuels), and continuously grades its own
accuracy against reality.

**Hard rules (programme mandate, never violated):**
- **Advisory only.** The system recommends; the operator decides. There is NO
  write-path to plant controls anywhere in the codebase.
- **Explainable, classical ML** (gradient-boosted trees + linear/system-ID methods).
  Never marketed as deep learning / black box; every prediction carries attribution.
- **Honest numbers.** Savings framed as illustrative until verified by M&V;
  accuracy always reported against the raw truth and a persistence baseline.

**Why the calciner:** the pyro-process is the plant's largest thermal-energy consumer.
The quantity that matters (degree of calcination of hot meal) cannot be measured
online (lab lag 2–4 h), so the industry controls its fast proxy: **calciner outlet
temperature (tag `423CI400_TT_4`)**, held in a state-dependent band. Tighter control
⇒ less over-burning margin ⇒ lower specific heat consumption and higher alternative-
fuel (AFR/TSR) substitution.

**The control-logic contract** (from the plant's own specification):
- Target temperature band is **state-dependent**: State 1 = 920–940 °C,
  State 2 = 900–920 °C, State 3 = 880–900 °C. The state is voted by 7 indicators:
  clinker liquid %, free lime, litre weight, hot-meal degree-of-calcination (lab) +
  kiln torque, kiln-inlet NOx, secondary-air temperature (process).
- Safety interlocks (hard, checked on every recommendation): calciner CO < 1500 ppm,
  calciner O₂ > 1.5 %, kiln-inlet CO < 800 ppm, kiln-inlet O₂ > 0.5 %,
  pre-heater CO (both strings) < 3000 ppm.
- Control actions apply only when kiln feed `353RL130_TPH` > 461 t/h (the
  "feed gate" used throughout training and serving).

## 2. Infrastructure & deployment

| Item | Detail |
|---|---|
| Host | On-prem GPU workstation `antpc@10.53.2.10` (Ubuntu 24.04, RTX 5090 32 GB, torch 2.11+cu128) |
| Repo | `github.com/aasthatripathijkcement/CCR-Project` (private). Work on branch **`feature/digital-twin-ml`** — PR #1 open to `main` |
| App | FastAPI backend (`:8010`) + Next.js 16 frontend (`:3010`), both bound 0.0.0.0 for LAN access |
| DB | SQLite `data/digital_twin.sqlite` (WAL mode; ~111 MB tracked via git-LFS on main) |
| Process manager | PM2, 6 apps, `pm2 save` + systemd unit ⇒ reboot-persistent |
| OPC connectivity | KEPServer `opc.tcp://10.4.222.139:49320` (NAT; plant-local twin is `192.168.100.139`), ns=2, tags `L3.L3_NEW.Root.<TAG>.Value`, **read-only** |
| Lab data | eLIMS REST API (bearer-token per request, 10-min TTL; credentials live only in a gitignored config). Clinker QC ~2-hourly; hot-meal ~4-hourly |

**The 6 PM2 services:**
1. `ccr-backend` — uvicorn `backend.main:app` (:8010)
2. `ccr-frontend` — Next.js production (:3010)
3. `ccr-livefeed` — `etl.live_feed --interval 2`: streams ~45 OPC tags every 2 s into
   `live_now` (latest row) + `process_live` (history)
4. `ccr-shadow` — `twin.shadow --interval 30`: the self-grading loop (see §6)
5. `ccr-monitor` — `twin.monitor`, cron `0 */6 * * *`: accuracy trail + recalibration
6. `ccr-retrain` — `twin.retrain`, cron `0 2 * * 0` (Sun): guarded weekly retrain

## 3. Data assets

| Table / source | Contents | Size / cadence |
|---|---|---|
| `process_1min` | The training historian: 31 process tags, 1-min rows, **Aug 2025 → present** (live data folded in weekly by `twin.ingest_live`) | ~521k rows, growing |
| `process_live` | Raw live feed | ~2 s cadence since 2026-07-23 |
| `lab_hotmeal` | eLIMS hot-meal XRF: DOC, Cl, SO₃ (sides A/B) | ~3.4k rows |
| `lab_clinker` | Clinker QC: free lime, litre wt, C3S, liquid | ~240+ rows |
| `dim_tag` | Tag dictionary: name, unit, engineering range, control role, OPC address | 31 rows |
| `shadow_log` | Every shadow tick: prediction (raw + served), actuals, advisory, maturation | ~2.9k/day |
| `afr_quality` | Online fuel-quality inference log | per tick |

**Tag roles** (defined in `backend/analysis.py`):
- Target `CV = 423CI400_TT_4` (calciner outlet temp)
- 8 MVs (levers): calciner coal `467RL400_TPH`, kiln coal `467RL300_TPH`, kiln feed
  `353RL130_TPH`, LAFR-PC `LAFR_FT`, LAFR-TAD `K3V10_F1`, SAFR `261WF080_TPH`,
  PH fan `423FN010_ST`, kiln speed `433MD140_ST1`
- 4 safety CVs: `423AM823_CO_PPM/_O2` (calciner), `423AM822_CO_PPM/_O2` (kiln inlet)
- State/disturbance: kiln torque `433MD140_TQ`, NOx `423AM822_NO`, secondary air
  `443KH050_TT1`, tertiary air `423DU415_TT`, PH outlet `423CN_TEMP`, cyclone temps
- 4 analyser RUN tags gate gas-reading validity

**Known data quirks (all handled in code — do not re-learn these the hard way):**
1. CO tags must use the `_PPM` suffix (plain `423AM823_CO` returns a scaled fraction).
2. Three historian tags are **dead on the live OPC server** (`423CN150_TT_1`,
   `423KR405_TT_3`, `423KR405_TT_4`): present in historical exports,永 absent live.
   No model feature may depend on them (this once silently excluded ALL live rows
   from training).
3. `K3V10F1` vs `K3V10_F1`: the underscore-less name in plant docs is dead; the
   `dim_tag` mapping was fixed 2026-08-21 — live capture of the AFR-TAD lever only
   exists after that date.
4. Gas analysers purge/back-flush with RUN still ON: O₂ reads ~ambient (≥15 %) —
   purge masking is mandatory (`ccr` quality gates and `twin/features.py`).
5. Historian timestamps carry microseconds ("…:00.000000"), live rows use ISO-"T" —
   lexicographic ts comparisons across formats over-select; always parse with
   `format="mixed"` and do boundaries in pandas (fixed in `twin/ingest_live.py`).
6. The OPC server has **no historian** (HistoryRead unsupported) — history must be
   accumulated live or exported from the plant historian.
7. Litre weight contains zero-coded blanks (~1.3 %); treat 0 as missing.

## 4. The models (`twin/` package)

### 4.1 Feature pipeline — `twin/features.py`
- 43 features, ALL regime-safe and ALL live-computable (a hard design rule):
  8 MVs; level-free disturbances (torque, NOx, fan current); masked safety readings;
  **CV momentum** at 1/3/5/10/15/30 min (temperature history expressed as deviation
  from now — absolute temps don't transfer across operating states); 5-min rolling
  dev/σ + 15-min σ; calciner-coal lags/rolls; per-lever 3-min rates; hot-side temps
  as **deviation-from-CV** (`423CN250_TT_1`, `423DU415_TT` only — live-available);
  secondary-air + PH-outlet raw; time-of-day sin/cos.
- **Targets are deltas:** every temperature head predicts `temp(t+h) − temp(t)`
  ("velocity form"); serving adds back current temp. This is the single most
  important design decision — absolute-target models fail across the plant's
  ~880–940 °C regime shifts (validated: absolute-target ARX/GRU scored *worse than
  persistence*).
- Training targets are 3-min-centred-mean smoothed (strips ~sensor noise);
  **accuracy is always reported against the raw target.**
- Analyser-valid masking (RUN==1 & O₂<15%), feed-gate filter, per-horizon forward-gap
  guards, chronological 70/15/15 split (never random — leakage).
- MVs are forward-filled across capture gaps (a lever holds its setpoint until moved).

### 4.2 Trained heads — `twin/train_gbt.py`
LightGBM (L1 objective for temp, 127 leaves, lr 0.03, ≤2000 rounds, early stopping):
- **Temperature trajectory:** mean + P10 + P90 heads at **t+5, +10, +15, +30 min**
  (bundle key `temp_multi`; `temp` aliases the 5-min heads).
- **Safety:** one head per CO/O₂ tag at t+5 (feeds interlock prediction).
- **Quality:** hot-meal DOC (MAE ≈ 0.47 on a ~95 mean); free-lime skipped while lab
  labels don't span the train window.
- Bundle: `models/twin_gbt.pkl` (joblib; `temp_delta=True`, `version`, feature list).
  Metrics: `models/metrics.json`. Full retrain ≈ 40–80 s on the host.

### 4.3 Serving — `twin/model.py`
`predict_core(levers, conditions)` → prediction {mean/P10/P90} for CV + safety tags,
**SHAP per-lever contributions (°C)**, quality, trajectory, and applied corrections:
- **Live conformal intervals:** the t+5 band is replaced by mean ± q80 of the last
  3 days of matured live |residuals| (`models/conformal.json`, written by monitor) —
  the stated 80 % band provably holds ~80 % on the running plant.
- **Offset-free bias correction:** shadow publishes the rolling 2-h signed bias of the
  RAW model (`models/live_bias.json`); serving adds a damped (×0.8), clamped (±10 °C)
  offset to mean/band/trajectory. Raw predictions are logged separately
  (`shadow_log.pred_raw`) so the correction can never cancel its own input signal.
- Graceful fallback to the legacy kNN surrogate if the bundle is missing.

### 4.4 Advisory optimizer — `twin/optimize.py`
Optuna (TPE) over the 4 calciner-side levers (calciner coal, LAFR-PC, LAFR-TAD, SAFR):
- **Objective:** minimise coal_norm − afr_norm (cheapest, greenest firing) + movement
  penalty (calm advice) + reward for band-confidence − prefer earlier band-reach.
- **Hard constraints:** predicted trajectory must REACH the state band and HOLD it at
  t+30; every safety interlock predicted-satisfied. Infeasible ⇒ rank by closest-to-
  band (never falls back to min-coal — that was a real bug, fixed).
- **Trust region:** search bounded to ±15 % of engineering range AND ±100 % of the
  current setpoint (whichever smaller) around live values — the model is trained on
  closed-loop data, so causal validity decays away from observed operation; full-range
  search let it exploit model insensitivity ("coal = 0 looks fine").
- Returns setpoints, predicted trajectory, `reach_min`, `band_confidence`
  (P(in band) from the P10–P90 spread, normal approx), interlock table, explanation.

### 4.5 Shadow mode — `twin/shadow.py` (every 30 s)
Reads the live snapshot → twin predicts t+5 → optimizer recommends → everything
logged to `shadow_log`. When a prediction's horizon elapses it is **matured**: the
realised temperature is looked up and the error recorded → a continuous, live
accuracy record. Also computes the **AFR fuel-quality inference**: rolling 2-h signed
residual of the raw model + current AFR share ⇒ `fuel_hot / fuel_cold / normal`
(the fuel delivering more/less heat than the model learned as normal — the plant's
dominant unmeasured disturbance, surfaced live).

### 4.6 Monitoring & self-maintenance
- `twin/monitor.py` (6-hourly): 24-h + lifetime accuracy (MAE, median, %within 5/10 °C,
  interval coverage), bias {raw, served}, conformal q80/q90 refresh, AFR summary,
  followed-vs-ignored advisory M&V mining (labelled *coincidental* until the advisory
  is surfaced to operators) → appends `models/monitor_log.jsonl`.
- `twin/ingest_live.py` + `twin/retrain.py` (weekly, Sun 02:00): fold live 2-s data
  into the historian as 1-min means → backup → retrain → **promote only if new test
  MAE ≤ 115 % of old** (else auto-restore) → reload serving → `retrain_log.jsonl`.

## 5. Results (all numbers real, as of 2026-08-21 retrain)

**Offline (chronological held-out test, includes the newest regime):**

| Horizon | Twin MAE °C | Persistence | R² | 80% interval coverage |
|--:|--:|--:|--:|--:|
| t+5 | **7.01** | 7.97 | 0.63 | 0.80 |
| t+10 | **7.00** | 8.55 | 0.60 | 0.83 |
| t+15 | **7.04** | 8.57 | 0.58 | 0.84 |
| t+30 | **7.27** | 8.95 | 0.54 | 0.85 |

**Live validation:** 46,000+ matured shadow predictions; live accuracy ≈ offline test
(no sim-to-real gap). Live skill vs persistence holds ~+5–15 % across calm and
turbulent regimes (during high-volatility windows both errors scale together —
always compare against persistence, never read MAE alone across windows).

**Model bake-off** (identical 57k test rows, all delta-target):
GBT 7.46 → best · velocity-ARX 7.98 (fully transparent close second) ·
GRU deep net 10.46 (**worse than persistence** — consistent with tabular-data
literature, e.g. Grinsztajn et al. NeurIPS 2022) · persistence 8.73.

**How to state accuracy defensibly** (the "99 % accurate" framing is banned — the
denominator is unit-arbitrary):
- Hero number: **average error ±7 °C, five minutes ahead, live-verified**.
- Single relative number: **~12–15 % MAE forecast skill over persistence**
  (unit-invariant, baseline-anchored, standard forecast verification).
- Distribution: ~75 % of forecasts within ±10 °C; median error ~5.7 °C.
- Physical context: sensor noise ≈ ±3 °C; median 5-min move ≈ 6 °C ⇒ realistic
  MAE floor ≈ 5–6 °C. External anchor: Deng & Kao, *Processes* 13(12):4068 (MDPI,
  Dec 2025) report 0.91 % MRE (≈8 °C) for the same problem with a TCN-BiLSTM.

**Diagnosed model characteristics:** predictions anchor on current temp (delta
model); genuine anticipation skill = 0.41 correlation between predicted and realized
change (persistence ≡ 0); damping ratio ≈ 0.44 (predicts ~44 % of the magnitude of
real moves — MAE-optimal caution on noisy targets; big excursions are under-called).

## 6. API surface (backend :8010)

`GET /api/health · /api/tags · /api/data-quality · /api/roadmap · /api/latest ·
/api/live · /api/overview · /api/series?tags=&window=day|week|month · /api/correlation ·
/api/crosscorr · /api/lab/hotmeal · /api/lab/clinker · /api/simulate/defaults`
`POST /api/simulate {levers, conditions}` → prediction + SHAP + trajectory + state/
band/interlocks (method `gbt-twin`)
`POST /api/optimize {conditions}` → advisory setpoints (live-snapshot-enriched)
`GET /api/twin/metrics · /api/twin/backtest · /api/shadow · /api/bench · /api/monitor`

**Frontend pages (:3010):** Live Overview (band trend + interlocks + state votes,
day/week/month toggle) · Digital Twin (live FLSmidth-style mimic + shadow panel with
time-aligned forecast-vs-actual and running accuracy + model quality + backtest) ·
Twin Simulator (live-tracking levers, SHAP °C per lever, predicted trajectory fan,
Optimize button) · Model Bench (bake-off) · Data Explorer · Correlation Studio ·
Roadmap.

## 7. Timeline of what was built (compressed changelog)

| Date (2026) | Milestone |
|---|---|
| Jul 20 | Programme context assembled; live OPC + eLIMS connectivity verified; tag errors found & fixed (`_PPM` CO tags, `K3V10_F1`, `423CN150_TT_2`); read-only collector + QC pullers + EDA (separate repo `~/projects/ccr-optimisation`) |
| Jul 29 | Teammate's CCR-Project app deployed on GPU; **trained twin v1** replaces kNN surrogate (GBT, delta target, MAE 7.70); optimizer, backtest, shadow mode, live OPC feed — all PM2 |
| Aug 3–4 | Model Bench 3-way bake-off on the cleaned historian; **v2** (enriched features, L1, smoothed target, MAE 7.46) → promoted (7.37 full-data); **v3** multi-horizon trajectory + chance-constrained optimizer; live learning loop (AFR inference, conformal, monitor) |
| Aug 6 | User-spotted **+1.9 °C bias** diagnosed on 15.7k live preds → offset-free correction (replay-proven +2.4 %); "leads the pattern" explained (plot geometry + anchoring) |
| Aug 20–21 | Optimizer trust-region fix; time-aligned accuracy chart; **weekly guarded retrain on live data** → best model (7.01/7.00/7.04/7.27), drift bias halved; TAD-lever live capture restored; PR #1 opened |

## 8. Current status & open items

**Working now:** everything in §2–§6, self-maintaining (self-grading, self-correcting
bias, self-recalibrating intervals, weekly guarded retrain).

**Open / next:**
1. PR #1 review & merge by the repo owners (Prakhar/Aastha).
2. **1-second granular training data** (roadmap): capture + retrain for faster dynamics.
3. **ROI model**: coal+AFR co-optimization quantified into predicted savings
   (anchor: industry APC benchmarks 1–5 % thermal energy, up to +50 % AFR — always
   framed as benchmarks, never commitments, pending finance-agreed M&V baseline).
4. Surface the advisory in the CCR to operators (turns the M&V mining from
   "coincidental" to real followed-vs-ignored evidence).
5. Big-move detector to counteract the 0.44 damping on large excursions.
6. Plant items: restore dead sensors (`423KR405_TT_3/4`), eLIMS hot-meal
  `material_name` for the DOC feed, historian export at 1 s.
7. Free-lime quality head: needs lab labels spanning the training window.

**Competitive context** (from a verified research pass): commercial cement-AI
(ABB Expert Optimizer, FLSmidth PXP, Rockwell Pavilion = closed-loop MPC+fuzzy;
Carbon Re/"Gigaton", Petuum, Imubit = closed-loop RL on digital twins) all run the
same *model + constrained optimiser* spine. Gigaton is deployed at JK Cement's
Mangrol plant (cloud, black-box). This project deliberately occupies the empty
quadrant: **advisory + explainable + on-premise**, with a clean upgrade path.

## 9. Repository map (branch `feature/digital-twin-ml`)

```
CCR-Project/
├── backend/            main.py (API) · analysis.py (twin routing + kNN fallback,
│                       state votes, interlocks) · db.py
├── frontend/           Next.js app — pages listed in §6
├── etl/                tags.py · clean.py · load.py · live_feed.py (OPC → DB)
├── twin/               features · train_gbt · train_nn · model · optimize · shadow
│                       · monitor · ingest_live · retrain · bench · bench_data
│                       · CONTRACT.md (module interface contract)
├── models/             twin_gbt.pkl (gitignored, regenerable ~60 s) · metrics.json ·
│                       bench_results.json · runtime state (gitignored): conformal /
│                       live_bias / monitor_log / retrain_log
├── data/               digital_twin.sqlite (LFS) · merged_kiln3_cleaned.csv (gitignored)
├── ecosystem.config.js (6 PM2 apps) · requirements.txt · requirements-ml.txt
└── docs/               ROADMAP.md · this file
```

Related but separate: `~/projects/ccr-optimisation` — the original data-foundation
repo (read-only OPC collector, eLIMS QC pullers, quality gates, EDA, competitive
research report, ML approach recommendation). Its tag registry and control-logic
constants were the basis for this project's domain rules.
