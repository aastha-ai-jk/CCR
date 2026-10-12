# plant_settings.json — what each key answers

Fill values from the plant questionnaire (`CCR_Kiln3_Advisor_Plant_Inputs.docx`).
`null` keeps the current default. Numbers only; units as shown.

| Q | Key | Unit / format | Current default |
|---|---|---|---|
| 1.1 | `optimizer.levers.LAFR_FT.min` / `.max` | l/h | 0 / 9,000 (PLC range) |
| 1.2 | `optimizer.levers.K3V10_F1.min` / `.max` | l/h | 0 / 4,000 |
| 1.3 | `optimizer.levers.261WF080_TPH.min` / `.max` | t/h | 0 / 30 |
| 1.4 | `optimizer.afr.max_tsr_pct` | % of calciner heat from AFR | no limit (*used from step 3*) |
| 1.7 | `optimizer.afr.priority` | list of tags, first raised first | none (*step 3*) |
| 2.1–2.5 | `interlocks.<tag>` | ppm / % (the limit value) | CO 1,500 · O₂ 1.5 · kiln CO 800 · kiln O₂ 0.5 · PH CO 3,000 |
| 3.1 | `optimizer.levers.467RL400_TPH.min` / `.max` | t/h | 0 / 35 |
| 3.3 | `optimizer.heat_balance.max_change_pct` | ± % of total calciner heat | no rule (*step 3*) |
| 3.5 | `optimizer.objective.mode` | `"cost"` · `"tsr"` · `"coal"` | `"cost"` |
| 4.1, 4.7 | `fuels.467RL400_TPH.gcv_kcal_kg` / `.price_per_ton` | kcal/kg · ₹/t | 6,000 · 8,000 |
| 4.2, 4.8 | `fuels.261WF080_TPH.gcv_kcal_kg` / `.price_per_ton` | kcal/kg · ₹/t | 4,000 · 2,500 |
| 4.4, 4.5, 4.9 | `fuels.LAFR_FT` and `fuels.K3V10_F1`: `gcv_kcal_kg`, `mass_per_unit` (density), `price_per_ton` | kcal/kg · kg/l · ₹/t | 5,500 · 0.95 · 2,000 |
| 4.10 | `process.clinker_factor` | clinker t / feed t | 0.62 |
| 5.1–5.3 | `temperature_bands."State N"` | `[low, high]` °C | [920,940] · [900,920] · [880,900] |
| 5.5–5.11 | `state_rules.<key>` | three `[low, high]` pairs for State 1, 2, 3 | see `backend/analysis.py` STATE_RULES |
| 5.12 | `process.kiln_running_min_feed_tph` | t/h | 461 |
| 6.1 | `quality.free_lime_max` | % — above it, recommendations may not cut total calciner heat | 1.8 |
| 6.1 | `quality.free_lime_min` | % — below it (over-burnt), recommendations may not raise total heat | off |
| 6.1 | `quality.liter_weight_min` / `.liter_weight_max` | g/l — below min = under-burnt (no heat cut), above max = over-burnt (no heat rise) | off |
| — | `quality.clinker_lab_max_age_h` | hours; an older lab result leaves the clinker guard inactive | 4 |
| 6.2 | `quality.doc_min` | % | 92 (not used to accept/reject: the DOC model is too weak) |
| 7.1–7.4 | `optimizer.levers.<tag>.max_step` | lever unit, ± per recommendation | ±15% of range, capped at ±100% of current value |
| 8.4 | `advisor.refresh_sec` | seconds | 60 |
| 8.6 | `advisor.setpoint_expiry_min` | minutes | 30 |
| — | `advisor.setpoint_reach_tol_frac` | fraction of the lever's range | 0.005 |
| — | `advisor.trials` | optimizer trials per run | 120 |
| — | `optimizer.objective.move_weight` / `.confidence_weight` | objective weights | 0.15 / 0.20 |

Example: `"temperature_bands": {"State 3": [880, 900]}` and
`"optimizer": {"levers": {"261WF080_TPH": {"min": 4, "max": 12, "max_step": 1}}}`.
