# plant_settings.json — what each key answers

Fill values from the plant questionnaire (`CCR_Kiln3_Advisor_Plant_Inputs.docx`).
`null` keeps the current default. Numbers only; units as shown.

| Q | Key | Unit / format | Current default |
|---|---|---|---|
| 1.1 | `optimizer.levers.LAFR_FT.min` / `.max` | l/h | observed 11 / 3,795 (1st–99th percentile of history) |
| 1.2 | `optimizer.levers.K3V10_F1.min` / `.max` | l/h | observed 0 / 3,406 |
| 1.3 | `optimizer.levers.261WF080_TPH.min` / `.max` | t/h | observed 0.06 / 26.2 |
| 1.4 | `optimizer.afr.max_tsr_pct` | % of calciner heat from AFR — hard cap | no limit |
| 1.7 | `optimizer.afr.priority` | list of tags, first raised first | none (*not used yet*) |
| 2.1–2.5 | `interlocks.<tag>` | ppm / % (the limit value) | CO 1,500 · O₂ 1.5 · kiln CO 800 · kiln O₂ 0.5 · PH CO 3,000 |
| 3.1 | `optimizer.levers.467RL400_TPH.min` / `.max` | t/h | observed 14.1 / 34.9 |
| 3.3 | `optimizer.heat_balance.max_change_pct` | ± % change of total calciner heat per recommendation — hard rule | no rule |
| 3.5 | `optimizer.objective.mode` | `"least_coal"` (least coal, then most AFR) · `"cost"` · `"tsr"` · `"coal"` | advisor `"least_coal"`; plant loops `OPTIMIZER_MODE` env (`"cost"`) |
| 4.1, 4.7 | `fuels.467RL400_TPH.gcv_kcal_kg` / `.price_per_ton` | kcal/kg · ₹/t | 6,000 · 8,000 |
| 4.2, 4.8 | `fuels.261WF080_TPH.gcv_kcal_kg` / `.price_per_ton` | kcal/kg · ₹/t | 4,000 · 2,500 |
| 4.4, 4.5, 4.9 | `fuels.LAFR_FT` and `fuels.K3V10_F1`: `gcv_kcal_kg`, `mass_per_unit` (density), `price_per_ton` | kcal/kg · kg/l · ₹/t | 5,500 · 0.95 · 2,000 |
| 4.10 | `process.clinker_factor` | clinker t / feed t (= 1 ÷ the plant's feed-to-clinker factor) | 0.62 (plant: 1/1.7 = 0.588) |
| 5.1–5.3 | `temperature_bands."State N"` | `[low, high]` °C | [920,940] · [900,920] · [880,900] |
| 5.5–5.11 | `state_rules.<key>` | three `[low, high]` pairs for State 1, 2, 3 | see `backend/analysis.py` STATE_RULES |
| 5.12 | `process.kiln_running_min_feed_tph` | t/h | 461 |
| 6.1 | `quality.free_lime_max` | % — above it, recommendations may not cut total calciner heat | 1.8 |
| 6.1 | `quality.free_lime_min` | % — below it (over-burnt), recommendations may not raise total heat | off |
| 6.1 | `quality.liter_weight_min` / `.liter_weight_max` | g/l — below min = under-burnt (no heat cut), above max = over-burnt (no heat rise) | off |
| — | `quality.clinker_lab_max_age_h` | hours; an older lab result leaves the clinker guard inactive | 4 |
| 6.2 | `quality.doc_min` | % | 92 (not used to accept/reject: the DOC model is too weak) |
| 7.1–7.4 | `optimizer.levers.<tag>.max_step` | lever unit, ± per recommendation | ±15% of range, capped at ±100% of current value |
| new | `process_response.467RL400_TPH."5"` … `."30"` | °C per t/h of calciner coal at 5/10/15/30 min | 0.3 / 2.2 / 4.0 / 3.3 (3,328 operator coal steps vs matched control) |
| new | `process_response.261WF080_TPH.*` | °C per t/h of SAFR | 0.0 / 0.35 / 0.4 / 0.45 (~1,400 SAFR trips/restarts) |
| new | `process_response.LAFR_FT.*` / `.K3V10_F1.*` | °C per l/h of LAFR | 0.0015 / 0.0025 / 0.0025 / 0.0025 (~820 LAFR PC steps; TAD assumed equal) |
| 8.4 | `advisor.refresh_sec` | seconds | 60 |
| 8.6 | `advisor.setpoint_expiry_min` | minutes | 30 |
| — | `advisor.setpoint_reach_tol_frac` | fraction of the lever's range | 0.005 |
| — | `advisor.trials` | optimizer trials per run | 120 |
| 7.1–7.4 | `advisor.max_move_pct` | largest move per recommendation, % of the lever's live value (coal, SAFR) | 10 |
| — | `advisor.horizon_min` | prediction horizon the advice is for (min) | 10 |
| — | `advisor.min_gap_sec` | fastest re-computation when the plant moves (s) | 15 |
| — | `advisor.decline_check_tol` | admin check tolerance per tag, e.g. `{"261WF080_TPH": 1.5}` | coal 2.5 t/h · SAFR 1.5 t/h · PC temp 5 °C |
| — | `optimizer.objective.move_weight` / `.confidence_weight` | objective weights | 0.15 / 0.20 |

Example: `"temperature_bands": {"State 3": [880, 900]}` and
`"optimizer": {"levers": {"261WF080_TPH": {"min": 4, "max": 12, "max_step": 1}}}`.

## guardrails (hard roof / floor, re-read on every request)

| Tag | Parameter | min | max |
|---|---|---|---|
| 423CI400_TT_4 | PC outlet temperature (degC) | 820 | 951 |
| 467RL400_TPH | Calciner coal firing (t/h) | 15 | 34.90 |
| 261WF080_TPH | SAFR (t/h) | 0 | 26.50 |

Source: CCR operators via project lead (2026-10-21; temp floor 850 -> 820 on 2026-10-23).
Checked on the live value and the recommended value (for temperature: the predicted T+10).
A value exactly at a limit is allowed; `null` = no limit. The optimizer never searches
outside these limits. Reported in `/api/twin/advisor` -> `guardrails`.
