# Calciner Temperature State Bands — Data-Driven Validation

Answers one question: **applying the Combustion Control logic doc's own
State 1/2/3 classification rules to real plant history (unchanged — no
threshold edits), what calciner outlet temperature does the plant actually
run at in each state?**

Source: `etl/analyze_state_bands.py`. Run `python -m etl.analyze_state_bands
[--source 1s|1min]`. Cross-validated at both native 1-second resolution
(24.9M readings, `config.CSV_1S_PATH`) and 1-minute resolution
(`process_1min`) — the two sources agree within rounding, so `1min` is the
fast default; `1s` reproduces the same numbers in ~30s for anyone who wants
the literal native-resolution figure.

## Method

1. Load process history + gate on kiln feed > 461 TPH (`config.KILN_FEED_MIN_TPH`,
   matches the doc's "Applicability Notice").
2. Join in eLIMS quality data (hot-meal DOC, clinker free lime/liter weight/
   liquid) by nearest timestamp — same tolerance convention as
   `twin/features.py` (DOC 4h, clinker QC 2h).
3. Classify each of the 7 documented parameters into State 1/2/3 using
   `backend.analysis.STATE_RULES` / `_classify_state` **directly, unmodified**
   — this script never redefines the logic, so it can't silently drift from
   what the app actually serves.
4. Average the available per-parameter votes, round to the nearest state
   (same rule `simulate()` itself uses), then group by state and report the
   calciner outlet temperature (`423CI400_TT_4`) distribution.

## Results (native 1-second resolution, 24.9M readings)

| State | Doc's target band | % of history | n (seconds) | Mean ± std | Actual band (p5–p95) |
|---|---|---|---|---|---|
| **State 1** | 920–940 °C | 0.3% | 64,440 | 889.7 ± 44.2 | 857 – 934 °C |
| **State 2** | 900–920 °C | 46.4% | 11,532,293 | 905.8 ± 36.3 | 866 – 940 °C |
| **State 3** | 880–900 °C | 53.4% | 13,279,977 | 900.3 ± 56.0 | 869 – 936 °C |

(`p5`–`p95` = the central 90% of readings, trimming the noisiest 5% at each
tail so a handful of spikes/sensor glitches don't distort the range.)

## The key finding

Three of the seven voting parameters are running **above the doc's entire
documented range** in current plant operation, not just in the "State 3"
bucket:

| Parameter | Doc's full range (S1→S3) | Actual historical median |
|---|---|---|
| Kiln torque | 360–400+ Nm | **466 Nm** |
| Secondary air temp | 980–1080+ °C | **1105 °C** |
| Hot-meal DOC | 92–96% | **95.3%** (mean 98.2%) |

Because of this, almost every minute's vote saturates at State 3, and
**State 1 triggers in only 0.3% of in-load history** — too sparse a sample
to trust as a validated band. States 2 and 3 are each backed by 11–13
million one-second readings and are statistically solid, but they overlap
far more than the doc's tight 20 °C windows imply — real plant operation
spans a much wider range than the logic's band widths suggest.

**This isn't a case for widening the band ranges alone** — the *voting
thresholds* for torque/secondary-air/DOC appear calibrated to an earlier
operating regime the plant has since moved past. Recalibrating those
thresholds (not just State 1/2/3's target temperatures) would be needed to
make State 1 reachable again under current conditions.

## Practical takeaway

Until the parameter thresholds are refreshed, treat this as an effectively
**2-state system**: State 2/3 combined span roughly **866–940 °C**, centered
around **900–915 °C** — rather than trusting the documented 3-way 920/900/880
split, which per this data essentially never resolves to State 1 in practice.

Full per-state stats: `models/state_band_analysis.json`.
