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
