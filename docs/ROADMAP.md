# Calcination Combustion Control — Digital Twin
### Project Roadmap & Parameter Specification
_JK Cement · Kiln-3 Precalciner · Owner: Prakhar Tripathi_

---

## 0. The one-sentence goal
Hold the **calciner (precalciner) outlet temperature** in a tight, in-spec band at the **lowest coal input and the highest practical AFR (alternate-fuel) share**, without ever violating combustion-safety (CO/O₂) limits — and prove it with a data-driven **Digital Twin** trained on our own plant history.

> Variance reduction is the *product*. Setpoint guidance is the *mechanism*. Thermal-efficiency + TSR (thermal substitution rate) gains are the *payoff*.

---

## 1. Data assets (the four rivers)

| # | Source | Format today | Native rate | Role in the twin |
|---|--------|--------------|-------------|------------------|
| 1 | **PLC Historical** | 12 monthly `.xlsx`, wide, 31 tags | 1 min | Training backbone — the process time-series |
| 2 | **OPC live** | daily `.xlsx` via OPC-UA, same 31 tags | ~10 s | Real-time feed for live twin + dashboard |
| 3 | **Hotmeal (eLIMS)** | Side A / Side B XRF | ~2-4 hr | Quality linkage — **DOC** (degree of calcination), Cl, SO₃ |
| 4 | **QC clinker (CLK-3)** | XRF | ~2 hr | Quality linkage — free lime, liter wt, C3S, liquid |

**Data-cleaning contract (Goal 1):**
1. Strip the 3 metadata rows (Y-max / Y-min / Unit / Description) from PLC files → keep them as a **tag dictionary** (engineering ranges + units + descriptions) in `dim_tag`.
2. Parse timestamps to a single UTC-naive index; enforce `dayfirst` handling (files mix formats).
3. **Range-clip / flag** each tag against its own Y-min/Y-max (physical limits) → outlier mask, don't silently drop.
4. Resample process data to a common grid (1 min canonical; 10 s available from OPC).
5. **Time-align lab data**: hotmeal DOC and clinker lab are sparse and lagged — join with as-of / forward-fill within validity windows (DOC 4 hr, clinker 2 hr per the logic doc).
6. Gate all training/optimization rows on **Kiln feed > 461 TPH** (`353RL130_TPH`) — the logic doc's applicability rule; below this the plant is in transient/low-load and the control actions don't apply.
7. Analyzer-validity gate: only trust CO/O₂/NOx when the analyzer `RUN` tag = 1 (exclude purge cycles).

---

## 2. Parameters — what to target and how many (Goal 2)

We split every signal into four control roles. **This is the contract the twin and optimizer are built around.**

### 2A. TARGETS — what the twin predicts / what we hold stable
| Role | Parameter | Tag | Why |
|---|---|---|---|
| **Primary CV** | Calciner outlet temperature | `423CI400_TT_4` | The whole control objective. Held in a band (see §2E). |
| Safety CV | Calciner outlet CO | `423AM823_CO_PPM` | Interlock < 1500 ppm |
| Safety CV | Calciner outlet O₂ | `423AM823_O2` | Interlock > 1.5% |
| Safety CV | Kiln inlet CO | `423AM822_CO_PPM` | Interlock < 800 ppm |
| Safety CV | Kiln inlet O₂ | `423AM822_O2` | Interlock > 0.5% |
| Safety CV | PH outlet CO (S1/S2) | `423AM820_CO_PPM`, `423AM821_CO_PPM` | Interlock < 3000 ppm |

→ **1 primary target + up to 6 safety targets.**

### 2B. MANIPULATED VARIABLES (MVs) — the levers the optimizer moves
| Lever | Tag | Notes |
|---|---|---|
| Calciner coal firing | `467RL400_TPH` | Primary PC fuel lever (±5% / 15 s per logic doc) |
| LAFR feed rate — PC | `LAFR_FT` | Liquid AFR to precalciner |
| LAFR feed rate — TAD | `K3V10F1` / `K3V10_F1` | Liquid AFR to tertiary air duct |
| SAFR feed rate | `261WF080_TPH` | Solid AFR |
| (Kiln coal firing) | `467RL300_TPH` | Kiln-side fuel; MV for the kiln-inlet safety loop |
| (PH fan speed / draft) | `423FN010_ST` | Draft lever — usually a constraint, optionally an MV |

→ **4 core MVs (+2 optional).** Start with the 4 calciner-side levers.

### 2C. DISTURBANCE / STATE VARIABLES (measured, not controlled — model features)
`353RL130_TPH` kiln feed · `433MD140_TQ` kiln torque · `433MD140_ST1` kiln speed · `423AM822_NO` kiln inlet NOx · `443KH050_TT1` secondary air temp · `423DU415_TT` tertiary air temp · `423CN_TEMP` PH outlet temp · `423CN150_TT_1` / `423CN250_TT_1` cyclone-5 exit temps · `423KR405_TT_3/4` feed-pipe temps · `423FN010_IT` PH fan load.

→ **~11 disturbance/state features.**

### 2D. QUALITY PARAMETERS (lab, slow — set the target band & validate)
Hotmeal: **DOC**, Cl, SO₃. Clinker: **free lime**, **liter weight**, **C3S**, **liquid (LIQ)**.
→ **7 quality parameters.**

### 2E. The dynamic target band (from the logic doc — this is the "brain")
Target PC temperature is **not fixed**; it is chosen from plant state:

| Evaluated | State 1 → **920–940 °C** | State 2 → **900–920 °C** | State 3 → **880–900 °C** |
|---|---|---|---|
| Liquid in clinker | 28.5–29.0% | 29.0–29.5% | > 29.5% |
| Kiln torque | 360–380 Nm | 380–400 Nm | > 400 Nm |
| Kiln inlet NOx | 450–550 ppm | 550–650 ppm | > 650 ppm |
| Secondary air temp | 980–1030 °C | 1030–1080 °C | > 1080 °C |
| Free lime | 1.4–1.8% | 1.0–1.4% | < 1.0% |
| Liter weight | 1100–1150 | 1150–1200 | > 1200 |
| DOC in hotmeal | 92–94% | 94–95% | 95–96% |

**Count summary:** ~**1 primary target + 6 safety + 4 core levers + 11 state features + 7 quality = ~29 active modelling signals** out of the 38 mapped (31 PLC + 7 lab).

---

## 3. The Digital Twin — model design (Goal 3)

Four cooperating model families (matches the project brief):

1. **Temperature-response model** — `f(MVs, DVs, recent history) → 423CI400_TT_4 at t+Δ` (Δ ≈ 2-10 min, tuned to measured dead-time).
2. **Combustion-safety model** — same horizon, predicts calciner & kiln-inlet CO/O₂ so a recommendation can never trade safety for efficiency.
3. **Quality-linkage model** — temperature behaviour → hotmeal DOC & clinker free lime (validates that "in-band temp" actually yields "in-spec clinker").
4. **AFR calorific-value inference** — no online CV sensor, so infer effective AFR CV from the temperature response and surface it live.

**Algorithms (as agreed):**
- **Primary: Gradient-Boosted Trees** — LightGBM / XGBoost. Fast, tabular-native, fully explainable via SHAP. No black box.
- **Backup: Neural network** — for the temperature-response dynamics a **sequence model** (Temporal CNN or LSTM/GRU on windowed lag features) is the natural NN; a regularized MLP is the simpler fallback. Trained on the same features, compared head-to-head.

**Feature engineering:** lag features & rolling stats per tag; rate-of-change of each lever; **dead-time alignment** via cross-correlation (lever move → temp response lag); feed-normalized fuel (kcal/kg heat consumption); regime flags (state 1/2/3); analyzer-valid masks.

**Optimizer:** **Optuna** searches the lever combination that holds temp on the state-selected target with **minimum coal + maximum AFR**, subject to the safety model staying inside all CO/O₂ interlocks. Output = recommended `467RL400_TPH`, `LAFR_FT`, `K3V10F1`, `261WF080_TPH`.

**How we know the twin is right (validation):**
- Time-based train/validation/test split (never random — no leakage across time).
- Metrics: MAE/RMSE on temp (target < ~5 °C), R², and **coverage** of prediction intervals.
- **Twin-vs-actual overlay** replay on held-out weeks (the CEO-facing proof).
- Residual drift monitoring in production → retrain trigger.
- Ablation: GBT vs NN vs naïve persistence baseline.

---

## 4. Phased plan

| Phase | Weeks | Deliverable | Exit criteria |
|---|---|---|---|
| **P0 Data foundation** | 1-2 | PostgreSQL schema, ingestion of all 4 sources, tag dictionary, data-quality report | All history queryable; QA dashboard green |
| **P1 The App (this repo)** | 2-4 | Interactive console: Roadmap tracker, Data Explorer, Correlation Studio, Twin Simulator (surrogate), Live Overview | CEO demo-ready; runs on real data |
| **P2 Twin v1** | 4-7 | GBT temperature + safety models, SHAP explainability, backtest | Temp MAE in target; interlocks respected in replay |
| **P3 Twin v2 + optimizer** | 7-10 | NN backup, Optuna optimizer, quality linkage, AFR-CV inference | Recommendations validated vs history |
| **P4 Live & shadow mode** | 10-14 | OPC live feed → twin runs in shadow next to CCR, operator insights | Shadow tracks actual; operators trust it |
| **P5 Closed-loop advisory** | 14+ | Setpoint guidance surfaced in CCR (advisory, then supervised) | Measurable variance ↓, fuel ↓, TSR ↑ |

---

## 5. The Application (P1) — architecture

**Stack:** FastAPI (Python) · PostgreSQL (PgAdmin4) · Next.js + TypeScript frontend · charting (Recharts/Plotly) · SQLAlchemy.

**Modules (CEO-facing):**
1. **Roadmap Tracker** — visual phase/milestone board, live % complete.
2. **Data Explorer** — tag catalog (with engineering ranges), coverage/quality heatmap, trend viewer.
3. **Correlation Studio** — pick tags, correlation matrix, scatter, **lagged cross-correlation** (reveals dead-time), multi-trend overlay.
4. **Twin Simulator (trial)** — lever sliders → predicted temp + CO/O₂ + state-band + interlock check. Uses a surrogate until the real GBT twin is wired in.
5. **Live Overview** — simplified CCR mimic (like the Kiln-3 screen) fed by OPC.

**DB schema (initial):** `dim_tag`, `fact_process` (long: tag_id, ts, value, quality_flag), `lab_hotmeal`, `lab_clinker`, `roadmap_milestone`, `twin_run`.

The real Digital Twin is trained/served **separately** (P2/P3); the app *consumes* it via an API and can run a surrogate for trials.
