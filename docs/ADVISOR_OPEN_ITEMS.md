# CCR Kiln-3 Advisor — objectives, open items and plant requirements

Status as of 16 Oct 2026. To be closed once the plant team's answers arrive
(questionnaire: *CCR_Kiln3_Advisor_Plant_Inputs.docx*; every answer maps to a key in
`data/plant_settings.json`, see `data/plant_settings.README.md`).

## 1. Objective of an ideal recommendation
1. **Least calciner coal, then as much AFR as the plant can take** (objective `least_coal`, done).
2. **Calciner outlet temperature in the state band** over 5–30 min (done; bands to be confirmed).
3. **Combustion safety:** O₂/CO within limits — needs **hard plant limits**, not the weak O₂/CO models.
4. **Clinker quality in zone:** free lime, liter weight, DOC (+ SO₃/Cl where AFR matters).
5. **Followable advice:** small realistic steps, stable (no minute-to-minute flipping), one clear set.
6. **Only the 4 calciner fuel levers** are recommended; kiln coal, kiln feed, PH fan speed and
   kiln speed stay fixed (plant decision, done).
7. **Advisory only:** nothing is written to the DCS.

## 2. Open items — recommendations / optimizer
| # | Item | Waiting on |
|---|---|---|
| 1 | Real AFR limits per stream + max TSR %; AFR priority; availability signal | Plant §1 |
| 2 | Hard O₂/CO limits (alarm/trip, normal operating band) replacing predicted interlocks | Plant §2 |
| 3 | Coal min/max; total-heat rule (± %) | Plant §3 |
| 4 | Real calorific values (SAFR heat is far below the assumed 4,000 kcal/kg) and prices | Plant §4 |
| 5 | Band per operating state; state-vote thresholds; remove advisor's fixed 870–885 °C display band | Plant §5 |
| 6 | Quality limits: free lime min/max, liter weight min/max, DOC min, SO₃/Cl | Plant §6 |
| 7 | Max step per lever; settling time; minimum change worth recommending (hysteresis) | Plant §7 |
| 8 | One Accept/Decline per set, decline reasons, operator login, refresh frequency | Plant §8 (on hold) |
| 9 | Lab-based DOC guard; live eLIMS lab feed on the GPU so quality guards are active | Dev + IT |
| 10 | Replace the mock "why" text with real lever effects | Dev |

## Pending on Advisory
| # | Item | Plan |
|---|---|---|
| A1 | Advisor screen shows a fixed **870–885 °C** band ("Band", band chart, legend, "Inside band" chip; `LO=870, HI=885` in `ccr-kiln3-advisor.html`). It is a placeholder from the uploaded design mock-up (21 Sep, `1bdcb46`), not from the plant or the optimizer, which uses the State band (State 3 = 880–900 °C). Plant setpoint on 21 Oct = 885 °C. | Show the band for the **current state**: the state is set for each set of kiln conditions (state-vote logic), and the screen band follows it, the same band the optimizer uses. |
| A2 | **Weekly retrain learns from PLC data only.** `twin/retrain.py` ingests live PLC minutes; quality labels (`lab_clinker` free lime / liter weight, `lab_hotmeal` DOC) are loaded manually from Excel and stop around 8 Aug 2026. The eLIMS live free lime (`lab_freelime_live`, on the edge) feeds only the clinker guard, not training. Temperature model unaffected (PLC inputs); free-lime / DOC models never get new labels. | 1) fold eLIMS live free lime into `lab_clinker` for training; 2) add eLIMS feeds for hot-meal DOC and raw-meal chemistry (LSF, SM); 3) retrain then learns from quality data too. |

| — | **Fixed 23 Oct (`3ccae07`): A3 band scale, A4 free-lime vote, and new A9 "no advice while kiln feed < 461 t/h".** Rows kept below for history. | — |
| A3 | **Band scale** (advisor "Position in operating band"): the scale covers only the band ± margin, so a live/predicted value outside it is pinned to the edge (e.g. live 883.3 shown at 885 on 21 Oct). | Stretch the scale to include the band **and** the live and predicted values. |
| A4 | **Free-lime vote bug** (`backend/analysis.py` `_classify_state`): an off-scale value is clamped by position, not direction — free lime 1.99 % (above State 1's 1.4–1.8, under-burnt) votes **State 3**. | Direction-aware clamp: free lime > 1.8 → State 1, < 1.0 → State 3; "up" indicators unchanged. |
| A5 | **Stale lab data votes for the state:** 4 of 7 votes (free lime, liter weight, liquid, DOC) use lab results from 8 Aug 2026 (no live eLIMS feed). State is the **rounded average** of votes (not majority): on 21 Oct 2+3+3+1+3+2+2 → 2.3 → State 2 → band 900–920. | Use lab values only if recent (e.g. < 8 h, plant to confirm); otherwise vote on live signals only and show "lab data stale" on screen; bring the edge's eLIMS live free lime into the feed. |
| A6 | **Band vs plant setpoint:** State 2 band 900–920 while the plant setpoint was 885 °C (21 Oct). | Read the DCS calciner-temperature **setpoint tag** and use setpoint ± X °C as the band (plant: tag name, ± X). Until then plant to confirm vote rule (average vs majority, tie-break) and thresholds (questionnaire §5). |
| A7 | **GenAI reasoning (phase 2):** generate the "Why the model is suggesting this" text with a small LLM from the optimiser's facts. Shortlist (cheapest per provider, 21 Oct 2026 list prices): GPT-5 nano ($0.05/$0.40 per 1M), Gemini 2.5 Flash-Lite ($0.10/$0.40, paid tier only), Claude Haiku 5.5 ($0.10/$0.50) — about $13–19/month at 1 call/min, under $5/month if called only when the recommendation changes. **Model chosen 22 Oct 2026: OpenAI GPT-5 nano (`gpt-5-nano`)** — run with minimal reasoning effort (reasoning tokens bill as output), paid API only. **Built 22 Oct (`6ab72d8`, `twin/reasoning.py`); template text live on localhost. Waiting on: real OpenAI API key in `.env` (`OPENAI_API_KEY`, currently a placeholder — user obtaining it; check first whether JK Cement has an OpenAI API org), then restart backend and review AI vs standard text side by side for a day; then IT approval for the edge.** | New `twin/reasoning.py`: numbers-only facts → LLM → every number checked against the facts → else template text (#4); cached per recommendation; hooked after `_advisor_run`; provider/model/key in `.env` (`REASONING_PROVIDER`, default off); `reasoning_log` table. Needs IT: approved cloud, outbound HTTPS from the edge (or proxy), data-sharing sign-off. Text only — never changes a recommendation value. |
| A8 | **Advice changes every minute:** on 22 Oct all 65 of 65 advisor refreshes logged as new advice (`advisor_log.issued` = 1: some lever moved > 0.5 % of its range vs the previous advice). The optimiser re-searches from scratch each minute (Optuna, 120 trials), so the recommended values jitter and are hard for operators to follow. | Hysteresis: keep the previous advice unless the new one is meaningfully better (predicted temp / coal / TSR gain above a threshold); round each lever to a practical step size; hold an advice for N minutes (plant: step sizes, min. change worth recommending, settling time — questionnaire §7). Track with `advisor_log` (issued per hour). |

## 3. Open items — ML model accuracy
| # | Item | Notes |
|---|---|---|
| 1 | Accuracy ceiling (~30 % skill vs persistence; T+5 MAE ≈ 6–8 °C, live 7–12 °C on volatile days) | Needs new signals, not tuning |
| 2 | New signals: raw-mix chemistry (LSF, SM), fineness, AFR moisture/calorific value, coal quality | eLIMS raw meal; plant §4, §9 |
| 3 | Confirm the measured fuel response (coal ≈ 4 °C per t/h at 15 min; SAFR ≈ 0.4; LAFR ≈ 0.25 per 100 l/h) | Process engineer |
| 4 | O₂/CO models useless (R² ≈ 0); free lime / DOC not predictable (R² < 0) | Use lab + hard limits |
| 5 | Data cleaning: outliers (e.g. 516 °C), analyser calibration periods (RUN flags), frozen sensors | Dev, plant §9.5 |
| 6 | Monitor job on the GPU stopped since 4 Oct (no live recalibration) | antpc access |
| 7 | Plant volatility since 9 Oct (AFR unstable 10–13 Oct, coal 29–30 t/h) — cause unknown | Ask plant |

## 4. Requirements from the plant team
- Questionnaire answers (69 questions, 9 sections).
- **`antpc` access** (SSH key or owner deploys) for the GPU server `10.53.2.10`.
- Repo access for reviewers (private GitHub repo: collaborators via the owner).
- CCR screen / edge device details, network path to the GPU, IT security sign-off, MoC.
- Pilot: shifts, champion operators, KPIs (coal t/h, TSR %, time in band, free lime).
