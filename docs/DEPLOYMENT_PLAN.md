# Plant Deployment Plan — Advisory Live by Dec 1, Optimising for Energy

**Objective:** the Kiln-3 calciner twin operating as a used, trusted advisory in the CCR by
**1 December 2026**, with **measured energy savings** (specific heat consumption ↓, AFR
substitution ↑) evidenced by a defensible M&V report — not a launch on Dec 1, but a
*results presentation* on Dec 1.

**North-star metrics (agreed in Week 1, frozen before pilot):**
- **SHC** — specific heat consumption, kcal/kg clinker (the energy bill)
- **TSR** — thermal substitution rate, % heat from AFR (cheaper + greener heat)
- **Time-in-band** — % minutes calciner temp in the state band (stability = quality + energy)
- Guard metrics (must not degrade): free lime, DOC, CO excursions, kiln torque stability

**Posture:** advisory-only throughout. Operators decide; the twin recommends and logs.
Closed-loop is explicitly out of scope for this phase.

---

## Phase map (Sep 7 → Dec 1)

| Phase | Weeks | Outcome |
|---|---|---|
| 0 · Align & define | W1 (Sep 7–12) | Charter signed, KPIs + success criteria frozen, data gaps identified |
| 1 · Measure money | W2 (Sep 14–19) | M&V engine computing SHC/TSR daily; ROI calculator with finance-agreed prices |
| 2 · Build the face | W3 (Sep 21–26) | CCR advisory screen (kiosk) + operator accept/dismiss capture |
| 3 · Harden | W4 (Sep 28–Oct 3) | Watchdogs, alerting, backups, security sign-off, UAT |
| 4 · People | W5 (Oct 5–10) | SOP, training, champion operators, MoC approval |
| 5 · PILOT | W6–8 (Oct 12–31) | Advisory live: 1 shift → all shifts; daily M&V |
| 6 · Operate & measure | W9–10 (Nov 2–14) | Full operation; mid-trial review; savings accumulating |
| 7 · Prove & hand over | W11–12 (Nov 16–28) | Final M&V report, runbook, steering sign-off |
| **GO-LIVE DECLARED** | **Dec 1 (Tue)** | Formal deployment + results readout to leadership |

Buffers respected: Dussehra week (~Oct 20) runs pilot-steady-state only, no changes;
Diwali (~Nov 8) week is observe-only. Sundays free (weekly retrain cron runs Sun 02:00).

---

## Week 1 · Sep 7–12 — Align & define (nothing moves without this)

- **Mon 8** — Kickoff with plant head + process head + CCR in-charge + instrumentation +
  IT. Present the dashboard (accuracy evidence, 68k live predictions). Secure the charter:
  advisory-only, one CCR screen, operator discretion absolute. Name 1 process engineer +
  3 champion operators (one per shift) as counterparts.
- **Tue 9** — KPI definition session with process engineering: agree the exact SHC formula
  (fuel flows × NCVs ÷ clinker production) and TSR formula; identify every input tag/lab
  value; agree the guard metrics.
- **Wed 10** — **Data-gap audit** (the critical path): do we have clinker production rate
  (kiln feed × factor or weigher tag?), coal NCV (lab, per lot), AFR NCV (lab cadence?),
  fuel prices (finance)? Log every gap with an owner and date. *This is the day that
  decides whether SHC is computable or must be proxied by coal-tph-per-feed-tph.*
- **Thu 11** — Success criteria pre-registration (so nobody moves goalposts later):
  e.g. pilot success = SHC ↓ ≥ 1% **or** TSR ↑ ≥ 1.5 pp at equal-or-better time-in-band
  and no guard-metric degradation, volatility-normalised vs baseline. Write it down,
  circulate for sign-off.
- **Fri 12** — Baseline definition: freeze the baseline window (e.g. Aug 1–Oct 11 operating
  data, regime-matched), document exclusions (shutdowns, upsets). Start eLIMS NCV pull
  automation if lab data is the SHC input.
- **Sat 13** — Week wrap: charter + KPI doc + gap register circulated for signatures.

## Week 2 · Sep 14–19 — Measure money (M&V engine + ROI)

- **Mon 14** — Build `twin/energy.py`: daily SHC/TSR/time-in-band computation from
  historian + lab NCVs; backfill over the full year (this yields the baseline
  distribution AND a beautiful seasonality view).
- **Tue 15** — Volatility-normalisation layer: every daily KPI reported beside plant
  volatility (persistence MAE that day) and regime mix — the same discipline that saved
  our accuracy story in the volatile week now protects the savings story.
- **Wed 16** — ROI calculator: (coal saved × price) + (TSR gain × cost spread coal-vs-AFR)
  − penalty terms; prices from finance in writing. Output in ₹/day and ₹/annum-run-rate.
- **Thu 17** — Followed-vs-ignored M&V upgrade: shadow already logs advisories + lever
  moves; add operator-attribution (accept/dismiss from the new screen, W3) and the
  regime-matched comparison query (followed hours vs ignored hours, same state band).
- **Fri 18** — `/api/energy` + Energy page on the dashboard app: daily SHC/TSR trend,
  baseline band, ₹ counter. (This page becomes the Dec-1 slide.)
- **Sat 19** — Review with process engineer: do the computed SHC numbers match their
  monthly reports? Calibrate until they nod. *Their number, our cadence.*

## Week 3 · Sep 21–26 — Build the face (what operators see)

- **Mon 21** — CCR advisory screen design review WITH the champion operators (not for
  them): one glance = current temp + band, twin forecast 30 min, recommended coal & AFR
  with direction arrows, confidence, and the one-line "why" (top SHAP driver in plain
  words). Big fonts, dark, kiosk.
- **Tue 22** — Build `/ccr` kiosk page (auto-refresh, no interaction needed) + advisory
  hygiene: rate-limit (new advisory only when recommendation moves materially or every
  10 min), hysteresis (no flip-flopping), stale-data banner if feed drops.
- **Wed 23** — Accept / dismiss / acted buttons (one tap, per advisory, logged with shift
  ID) — this is the M&V labelling instrument. Also a "twin unsure" state (wide P10–P90 or
  fallback mode → advisory suppressed, honesty on screen).
- **Thu 24** — Alarm-fatigue review with CCR in-charge: the screen must never beep, never
  demand action, never conflict with the DCS alarm philosophy. Position it as a co-pilot
  glance, physically beside (not on) the DCS console. Agree the physical screen + mini-PC
  with IT (order hardware TODAY if not in stock — lead time risk).
- **Fri 25** — Optimizer objective tuning for energy: reweight advisory to explicitly
  minimise (coal × price − AFR credit) subject to trajectory-in-band + interlocks +
  movement limits; re-run the guard suite; shadow-verify for 24 h before it faces
  operators.
- **Sat 26** — Internal demo end-to-end on the GPU + kiosk hardware; punch list.

## Week 4 · Sep 28–Oct 3 — Harden (it must not embarrass us in week 6)

- **Mon 28** — Failure-mode walk: OPC drop, DB lock, GPU reboot, model reload failure,
  stale bias file, network partition CCR↔GPU. For each: detection, on-screen state,
  auto-recovery, alert.
- **Tue 29** — Watchdog + alerting: PM2 max-restart policies, a `ccr-watchdog` heartbeat
  (feed freshness, shadow tick age, prediction sanity range) → email/SMS to us + process
  engineer. Nightly SQLite backup + weekly model-bundle archive to a second disk.
- **Wed 30** — Security/IT review: read-only OPC account confirmed at server level,
  kiosk on view-only VLAN, no plant-write path exists anywhere (show the code), access
  list for the GPU box. Get IT's written OK (this unblocks the CCR screen install).
- **Thu 1** — Model-freeze policy decision (recommend: weekly guarded retrain CONTINUES —
  it's part of the product — but feature/recipe changes freeze from Oct 6 to Dec 1;
  emergencies via the guard + rollback path only). Document rollback drill; perform one.
- **Fri 2** — UAT with process engineer against a scripted checklist (20 scenarios incl.
  feed loss, absurd-advisory suppression, interlock breach display).
- **Sat 3** — Fix UAT punch list. Tag the release (`v7-pilot`).

## Week 5 · Oct 5–10 — People (adoption is the actual product)

- **Mon 5** — SOP drafting with process head: when to consult the screen, how to act
  (through normal DCS moves), how to tap accept/dismiss, who to call. One page.
- **Tue 6** — Training material: 10-minute deck + 1-page Hindi/English quick card
  ("what the twin is, what ±6° means, it's a co-pilot not a boss").
- **Wed 7** — Train shift A (champion-led, we support); collect objections verbatim.
- **Thu 8** — Train shifts B & C; management-of-change (MoC) paperwork submitted with
  charter + IT sign-off + SOP attached.
- **Fri 9** — Dry run: screen live in CCR in "watch-only" banner mode (advisories visible,
  labelled TRIAL); shadow M&V of operator glances; fix legibility complaints same-day.
- **Sat 10** — Go/no-go gate #1 with plant head: MoC approved, training done, hardware
  stable → authorise pilot start Monday.

## Weeks 6–8 · Oct 12–31 — PILOT (advisory live, savings clock starts)

- **W6 Oct 12–17** — Live on **shift A only** (champion shift). Daily 15-min standup with
  the shift + process engineer: what did you follow, what did you ignore, why. Fix UX
  same-day. M&V daily email starts (SHC/TSR/in-band/₹, volatility-normalised, followed
  vs ignored). *Dussehra ~Oct 20: no changes beyond this week's state.*
- **W7 Oct 19–24** — Extend to **all three shifts** (steady state through the festival
  week — deliberately boring). Watch for shift-behaviour differences; that contrast is
  M&V gold.
- **W8 Oct 26–31** — First formal interim review (Fri Oct 30): 3 weeks of advisory data,
  followed-vs-ignored split, first ₹ number with confidence interval. Decide any
  optimizer-weight adjustment (through the guard, once, this week only).

## Weeks 9–10 · Nov 2–14 — Operate & measure (don't touch, just run)

- **W9 Nov 2–7** — Full operation. Weekly auto-report to plant head. Begin drafting the
  final M&V methodology section while data accrues. *Diwali ~Nov 8: observe-only, remote
  watchdog on call.*
- **W10 Nov 9–14** — Mid-trial leadership review (Fri Nov 13): interim savings, adoption
  rate (advisories followed %), operator quotes. Pre-align the Dec-1 decision makers so
  nothing on Dec 1 is a surprise.

## Weeks 11–12 · Nov 16–28 — Prove & hand over

- **W11 Nov 16–21** — Cut the data (Nov 20), run the final M&V: pilot vs baseline,
  volatility- and regime-normalised, followed-vs-ignored causal check, guard metrics.
  Draft report + the Dec-1 deck (the Energy page is slide 1).
- **W12 Nov 23–28** — Operations handover: runbook (restart, rollback, retrain, alarm
  responses), on-call rota, monthly review cadence. Steering pre-read circulated Nov 26.
  Dress rehearsal Nov 27.
- **Mon Nov 30** — Final go/no-go gate #2; sign-offs collected.
- **Tue Dec 1 — GO-LIVE DECLARED**: advisory formally part of CCR operations; results
  readout: measured SHC/TSR delta, ₹ run-rate, adoption stats; Phase-2 proposal on the
  table (fuel-quality sensor → next accuracy tier + higher TSR confidence; big-move
  detector; free-lime head; closed-loop study).

---

## Critical path & risks (watch these, not the code)

1. **SHC computability (W1)** — needs clinker rate + fuel NCVs. If lab NCV cadence is too
   sparse: fall back to coal-tph-per-tonne-feed as primary with SHC monthly. Decide Sep 10.
2. **Kiosk hardware + IT approval (W3–4)** — physical screen in CCR has procurement and
   permission lead times. Order Sep 24, escalate if not installed by Oct 6.
3. **MoC approval (W5)** — plant management-of-change can take weeks if started late;
   paperwork goes in Oct 8 with everything pre-attached.
4. **Adoption, not accuracy** — the model is done (5.98°, 22–25% skill, 68k live proofs).
   If operators ignore the screen, savings are zero. Champions, standups, and same-day UX
   fixes are the real deployment work.
5. **Attribution honesty** — savings claims MUST be volatility-normalised and
   followed-vs-ignored-backed, or one hard week discredits the programme. The M&V engine
   (W2) is the insurance.
6. **Festival windows** — Dussehra/Diwali reduce attention; the plan deliberately parks
   steady-state operation there and changes nothing.

**Owners shorthand:** Mudit/AI-team = build items · Process engineer = KPI/UAT/SOP ·
CCR in-charge = operators + screen placement · IT = hardware/network/security · Plant
head = gates 1 & 2.
