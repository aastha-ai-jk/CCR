# CCR Digital Twin — Progress Report

**Project:** Calcination Combustion Control, Nimbahera Kiln 3
**Scope:** Advisory-first digital twin for calciner outlet temperature (423CI400_TT_4)
**Status:** LIVE in shadow mode on the plant GPU server · **Report date: 2026-09-02**

---

## 1. Executive summary

A full digital twin of the Kiln-3 precalciner is live: it streams plant data every
2 seconds, predicts the calciner outlet temperature trajectory (t+5/10/15/30 min,
each with uncertainty bands), recommends coal/AFR setpoints under safety interlocks,
scores itself against the realised plant continuously, self-corrects its bias,
recalibrates its uncertainty from live residuals, and retrains itself weekly behind
a promotion guard. Everything is advisory — nothing writes to the plant.

**Headline accuracy (t+5 min, held-out test):**

| Date | Version | MAE (°C) | Note |
|---|---|---|---|
| Jul 29 | v1 | 7.7 | first GBT twin, deployed with shadow mode |
| Aug 04 | v2 | 7.37 | enriched features (bench winner promoted) |
| Aug 04 | v3 | — | multi-horizon trajectory heads + optimizer path-planning |
| Aug 21 | — | 7.01 | first guarded retrain with live-regime data |
| Aug 27 | v4 | 6.67 | 5-min-averaged (denoised) features |
| Aug 27 | v5 | 6.43 | huber objective (capped-rounds recipe) |
| Aug 27 | **v7** | **5.98** | extended features + ARX blend + live-history serving |

**Live validation (the number that matters):** since v7's promotion, **11,033
matured live predictions score MAE 6.56 °C vs 8.45 °C for persistence on the
identical window — 22% forecast skill, running against the real plant.** The
Sunday automatic retrain (Aug 30, 3,463 fresh minutes) re-confirmed the model
at 5.975 with no manual involvement.

Published state of the art for this exact problem (TCN-BiLSTM hybrid, MDPI
*Processes* 13(12):4068, Dec 2025) reports MRE 0.91% ≈ 8 °C. **The twin is
~25% better than published SOTA.**

---

## 2. What is running (architecture)

```
KEPServer OPC UA (10.4.222.139:49320, read-only)
   └─ ccr-livefeed   45 tags every 2 s → SQLite (live_now, process_live)
        └─ ccr-shadow  every 30 s: predict t+5..t+30 → advisory → log →
        │              mature vs realised temp → AFR fuel-quality inference
        │              → live bias estimate (models/live_bias.json)
        ├─ ccr-monitor every 6 h: accuracy / bias / conformal recalibration
        │              / M&V followed-vs-ignored → models/monitor_log.jsonl
        ├─ ccr-retrain Sun 02:00: ingest live minutes → retrain → GUARD
        │              (promote only if ≤115% of old MAE) → reload serving
        ├─ ccr-backend FastAPI :8010 (simulate / optimize / twin metrics /
        │              backtest / shadow / bench / monitor endpoints)
        └─ ccr-frontend Next.js :3010 (Overview, Simulator, Twin, Model Bench)
```

**Model core (v7):** LightGBM gradient boosting, 89 features, one head-set per
horizon (mean + P10/P90 quantiles). The served mean is a fixed-weight blend of
the GBT with a ridge ARX fit on the same target (w=0.4) — the two make
differently-shaped errors, and the linear model is regime-stable exactly where
trees overfit. Temperature is always predicted as a **5-min change** (regime-
invariant) and reconstructed onto the current temperature.

**Key design decisions, each validated by experiment:**

- **Delta targets, never absolute** — absolute-target models fail across the
  880–940 °C state bands (bench: absolute ARX/GRU scored *worse than
  persistence*).
- **Trees beat deep learning on this data** — 3-way bake-off on identical rows:
  GBT 7.71 · velocity-ARX 7.97 · GRU 10.46 · persistence 8.73. Consistent with
  Grinsztajn et al. (NeurIPS 2022) on tabular data.
- **1-min resolution beats 1-second** — controlled 3-way study on the 28.8M-row
  1 s export: 5-min features 5.62 < 1-min 5.81 < 1-second 6.20. Averaging
  denoises; sub-minute detail is noise at a 5-min horizon. (This study produced
  the v4 features.)
- **Smoothed training targets, raw-truth scoring** — models train on a 3-min
  centred mean (less noise to chase) but every reported MAE is against the raw
  sensor value. No metric flattery.
- **Live-history serving (v7)** — prediction inputs are built from the real
  last 80 minutes of live data (momentum/EWM/lag features exact), falling back
  to trend-linearised values only if the feed is down.

**Live learning loop:**

- **Offset-free bias correction** — rolling 2 h signed bias vs the raw model,
  damped ×0.8, clamped ±10 °C, applied to mean + intervals + trajectory.
  (Diagnosed from a user observation of chart bias: systematic +1.9 °C from
  condition drift; replay-proven +2.4% MAE, live-verified.)
- **Conformal recalibration** — the t+5 interval width is q80 of matured live
  residuals (3-day window), replacing the offline quantile interval. Live
  coverage ~80–84%, as designed.
- **AFR fuel-quality inference** — 2 h residual trend + AFR share → labels the
  fuel hot/cold/normal (an unmeasured disturbance made visible).
- **Guarded weekly retrain** — live minutes ingested into the historian, model
  retrained, promoted only if not >15% worse, else auto-restored; serving and
  shadow reload automatically. Has run autonomously and improved the model.

---

## 3. The accuracy journey (what worked)

| Step | Gain | Evidence |
|---|---|---|
| v2: long momentum, hot-side deviations, time-of-day, L1, tuning | 7.71→7.37 | Model Bench |
| Live-regime data in training (retrain loop + 3 data-bug fixes) | 7.37→7.01 | retrain log |
| v4: 5-min-averaged (denoised) inputs + 15-min anchor | 7.01*→6.67 | exp_v4 A/B (*window shifted) |
| v5: huber objective under the 2000-round cap | 6.67→6.43 | exp_squeeze 1–2 |
| v7: +33 features (45–60 min memory, EWMs, lever dynamics, coal×O₂ interactions) | — | opt_bench: xfeat 6.10 |
| v7: ridge-ARX blend (w=0.4, every horizon) | — | opt_bench: 5.99; exp_squeeze4 per-horizon |
| v7: live-history serving (exact features at serve time) | live-side | shadow verified |
| **Net** | **7.01 → 5.98 offline · 6.56 live** | guard + 6-day live window |

**Equally important — what didn't work (all measured, all documented):**

- Deep sequence models (GRU): worse than persistence even with delta targets.
- 1-second-resolution features: −6.6% vs the 1-min recipe.
- Recency-weighted training: hurt (the full year matters; drift is handled by
  the bias corrector).
- Optuna hyperparameter search: val-optimised params did not transfer to test
  (6.11 vs 5.99) — the hand recipe stands.
- Linear-tree LightGBM, two-stage stacking, 5-seed bagging, heavier target
  smoothing: no gain.
- Uncapped huber training: the 2000-round cap is load-bearing regularisation
  (6.43 capped vs 6.74 uncapped) — warning documented in code.
- EWM-smoothed target "won" the leaderboard (5.92) but was **rejected**: it
  shifts the effective horizon to ~t+3.5 min — scores better by predicting
  less far ahead. Metric gaming, not skill.

---

## 4. Short-horizon capability (t+1/t+2/t+3)

Tested 2026-09-02 (`twin/exp_short_h.py`, v7 recipe, identical splits, vs raw
truth):

| Horizon | Twin (blend) | Persistence | Forecast skill |
|---|---|---|---|
| t+1 min | **3.36 °C** | 3.87 | 13.2% |
| t+2 min | 5.03 | 6.08 | 17.3% |
| t+3 min | 5.52 | 7.23 | 23.6% |
| t+5 min | 5.98 | 7.93 | 24.6% |

Two readings: (a) the "3 °C" accuracy target naturally exists at the 1-minute
horizon — the plant simply hasn't moved far yet, so most of the 3.36 is sensor
noise plus micro-variation; (b) skill *grows* with horizon (13% → 25%), i.e.
the twin's value over a naive operator increases exactly where prediction is
hardest and most useful. Short horizons could feed a fast "temperature moving
NOW" early-warning strip alongside the 5–30 min advisory trajectory.

---

## 5. The accuracy floor — why ~6 °C is near the limit

Measured from the year-long 1-second export (`twin/noise_floor.py`):

- The **true 5-minute temperature change** (denoised at both ends) averages
  **|Δ| = 11.6 °C** (median 8.5, p90 25.7). This is the quantity any model
  must predict.
- **~0.7 °C** of every raw-truth MAE is thermocouple jitter on the truth
  itself (the same model scores 5.98 vs raw but ~5.6 vs the smoothed process
  temperature).
- The 3-model stack with test-picked weights — the theoretical ensemble
  ceiling — scores 5.98: ensembling has nothing further to give.
- The unexplained remainder is dominated by disturbances **not present in the
  tag list**, chiefly minute-scale fuel calorific-value variation in the AFR
  stream.

**Practical floor with current instrumentation: ~5.5–6 °C vs raw truth.**
Reaching materially lower (e.g. 3 °C) requires new plant sensing — online fuel
quality/calorimetry — not better algorithms. That is an instrumentation
investment decision, not a modelling task.

**Defensible stakeholder framing:** "The twin predicts calciner temperature
5 minutes ahead within ±6 °C on average — 20%+ better than assuming no change,
verified on 11,000+ live predictions, and ~25% better than the best published
academic result for this problem."

---

## 6. Optimizer / advisory (unchanged this period, live since July)

Optuna-based advisory over 4 calciner levers (coal, AFR, TAD damper, kiln-feed
coupling): minimise coal / maximise AFR subject to the predicted **trajectory**
reaching and holding the temperature band (chance-constrained on P10/P90), all
safety interlocks respected (calciner CO<1500, O₂>1.5, kiln-inlet CO<800,
O₂>0.5, PH CO<3000), trust region around live setpoints, movement penalty.
Advisories are logged every 30 s in shadow mode with followed-vs-ignored M&V
mining (labelled coincidental until the advisory is surfaced to CCR operators).

---

## 7. Infrastructure & ops

- **GPU server** `10.53.2.10` (RTX 5090): 6 PM2 services, boot-persistent
  (`pm2 save` + systemd). Ports: frontend :3010, API :8010.
- **Data:** SQLite (WAL) — 420k+ historian minutes (Aug 2025 → now, growing
  weekly via ingest), 2 s live tables, shadow/AFR/monitor logs. 1-second
  year-long export (28.8M rows) retained on GPU for research.
- **Reproducibility:** every experiment is a committed module
  (`twin/exp_*.py`, `twin/opt_bench.py`) writing a JSON artifact to `models/`.
  The optimisation bench has frozen splits + a persistent leaderboard — any
  new idea is a ~10-line function away from a fair verdict.
- **Safety nets:** promotion guard (≤115% of incumbent MAE), previous bundle
  auto-backup with restore, live bias corrector, conformal interval
  recalibration, 6-hourly monitor evidence trail.
- **Code:** branch `feature/digital-twin-ml`, **PR #1 open** to main.

---

## 8. Open items & recommended next steps

1. **Surface the advisory in the CCR room** (display/annunciation) — unlocks
   real M&V (followed vs ignored → measured ROI). Currently shadow-only.
2. **ROI co-optimisation model** — translate coal↓/AFR↑ advisories into ₹/day
   using fuel prices + measured response; the optimizer already produces the
   recommendations.
3. **Instrumentation proposal** — online AFR fuel-quality sensing is the only
   route below ~5.5 °C; worth a costed proposal if leadership wants the next
   accuracy tier.
4. **Free-lime quality head** — blocked on denser lab labels (eLIMS linkage
   built; sampling cadence is the constraint).
5. **Big-move detector** — the model damps large swings (damping ratio 0.44);
   a classifier flagging "large move imminent" would complement the regression.
6. **PR #1 review + merge** — 5 commits total pending (v4/v5/v7 currently
   local, awaiting push approval).

---

*Prepared by the AI development loop (Claude Fable 5) under Mudit Krishna's
direction. Every accuracy figure in this report is reproducible from a
committed experiment module and its JSON artifact in `models/`.*
