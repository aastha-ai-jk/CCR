"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

/**
 * Mimics the visual design of ccr-kiln3-advisor.html (uploaded reference
 * mockup, branch uat-m) -- same color tokens, hero band gauge, row-list,
 * why/drivers/impact layout, decline-reasons flow, logged banner and toast.
 * Unlike the mockup (synthetic, 9 independently-actionable parameters), this
 * is wired to the real /api/advisory response: backend/analysis.py's
 * advise() optimizes the 4 calciner-side fuel levers as ONE coordinated
 * move, so accept/decline applies to that whole move, not per-row. Every
 * number here traces back to a real field -- nothing is fabricated the way
 * the mockup's "confidence %" or synthetic history was.
 */

const fmt = (v: any, d = 1) =>
  v === null || v === undefined || isNaN(Number(v)) ? "—" : Number(v).toFixed(d);

const SAFETY_NAME: Record<string, string> = {
  "423AM823_CO_PPM": "Calciner outlet CO",
  "423AM823_O2": "Calciner outlet O₂",
  "423AM822_CO_PPM": "Kiln inlet CO",
  "423AM822_O2": "Kiln inlet O₂",
};

function dirOf(delta: number): "up" | "down" | "hold" {
  if (Math.abs(delta) < 1e-9) return "hold";
  return delta > 0 ? "up" : "down";
}

function buildWhy(adv: any): string {
  if (!adv?.feasible) return "";
  const band = adv.target_band;
  const temp = adv.live_actual_temp;
  const dir = temp > band.high ? "above" : temp < band.low ? "below" : "within";
  const parts: string[] = [];
  parts.push(
    `The plant is running in ${adv.operating_state}, which should be kept between ${band.low}–${band.high} °C.`
  );
  parts.push(
    dir === "within"
      ? `Temperature is currently ${fmt(temp, 0)} °C — already inside that range, so the aim is to hold it there using the least fuel.`
      : `Temperature is currently ${fmt(temp, 0)} °C — ${dir} the target range — so fuel needs to be ${dir === "above" ? "reduced" : "increased"} to bring it back.`
  );
  parts.push(
    `Every fuel change is capped at ${adv.assumptions?.guardrail_pct}% at a time (the plant's own rule: adjust up to 5% every 15 seconds). Of ${adv.candidates_evaluated} possible fuel settings checked within that limit, ${adv.safe_candidates} were safe, and this is the best one.`
  );
  parts.push(
    adv.mode === "hold"
      ? "A safe setting that keeps the temperature in range was found, so the cheapest one of those was chosen."
      : `No single 5% step fully reaches the target range, so this is the safe step that gets closest to it` +
        (adv.steps_to_band ? ` — expect to repeat a move like this roughly ${adv.steps_to_band} more times to fully settle in range.` : ".")
  );
  return parts.join(" ");
}

function buildDrivers(adv: any, mv: any): [string, number][] {
  if (!adv?.feasible) return [];
  const drivers: [string, number][] = [];
  const guardrailPct = adv.assumptions?.guardrail_pct ?? 5;
  if (mv) {
    const room = Math.abs(mv.from) * (guardrailPct / 100);
    const used = room > 1e-9 ? Math.min(100, Math.round((Math.abs(mv.delta) / room) * 100)) : 0;
    drivers.push([`Share of the ±${guardrailPct}% ramp limit used`, used]);
  }
  if (adv.candidates_evaluated) {
    drivers.push(["Fuel-mix candidates that passed every safety interlock",
      Math.round((adv.safe_candidates / adv.candidates_evaluated) * 100)]);
  }
  if (adv.mode === "approach" && adv.steps_to_band) {
    drivers.push(["Progress toward band from this one guarded step",
      Math.round(Math.min(100, 100 / adv.steps_to_band))]);
  }
  return drivers;
}

function BandGauge({ live, pred, lo, hi }: { live: number; pred: number; lo: number; hi: number }) {
  const bw = hi - lo;
  const ax0 = lo - bw * 0.7, ax1 = hi + bw * 0.7;
  const W = 760, L = 34, R = 726, y = 58, h = 26;
  const x = (t: number) => L + ((t - ax0) / (ax1 - ax0)) * (R - L);
  const ticks = [lo - bw, lo, (lo + hi) / 2, hi, hi + bw];
  const px = x(pred);
  return (
    <svg viewBox={`0 0 ${W} 132`} role="img" aria-label={`Calciner temperature against its ${lo} to ${hi} degree operating band`}>
      <rect x={L} y={y} width={R - L} height={h} rx={3} fill="#232D34" />
      <rect x={x(lo)} y={y} width={x(hi) - x(lo)} height={h} rx={3} fill="rgba(79,184,138,.22)" />
      <line x1={x(lo)} y1={y} x2={x(lo)} y2={y + h} stroke="#4FB88A" strokeWidth={1.5} />
      <line x1={x(hi)} y1={y} x2={x(hi)} y2={y + h} stroke="#4FB88A" strokeWidth={1.5} />
      {ticks.map((t, i) => (
        <g key={i}>
          <line x1={x(t)} y1={y + h} x2={x(t)} y2={y + h + 5} stroke="#3A464E" strokeWidth={1} />
          <text x={x(t)} y={y + h + 20} textAnchor="middle" fontFamily="IBM Plex Mono,monospace" fontSize={11.5} fill="#6D7B85">{Math.round(t)}</text>
        </g>
      ))}
      <line x1={x(live)} y1={y - 9} x2={x(live)} y2={y + h + 2} stroke="#EDF4F8" strokeWidth={2} />
      <text x={x(live)} y={y - 15} textAnchor="middle" fontFamily="IBM Plex Sans,sans-serif" fontSize={11.5} fill="#C4D0D9">now {fmt(live, 1)}</text>
      <path d={`M${px} ${y + h + 11} l7 11 -14 0 Z`} fill="#E9A13B" />
      <line x1={px} y1={y} x2={px} y2={y + h} stroke="#E9A13B" strokeWidth={2} strokeDasharray="3 3" />
      <text x={px} y={y + h + 40} textAnchor="middle" fontFamily="IBM Plex Sans,sans-serif" fontSize={11.5} fill="#E9A13B">predicted {fmt(pred, 1)}</text>
    </svg>
  );
}

function TrendChart({ points, lo, hi, marks }: { points: { t: string; v: number | null }[]; lo: number; hi: number; marks: number[] }) {
  const vals = points.map((p) => p.v).filter((v): v is number => v != null);
  if (!vals.length) return <div className="empty">Not enough recent history to plot yet.</div>;
  const W = 900, H = 250, L = 48, R = 886, T = 16, B = 214;
  const mn = Math.min(lo, ...vals) - 4, mx = Math.max(hi, ...vals) + 4;
  const x = (i: number) => L + (i / Math.max(1, points.length - 1)) * (R - L);
  const y = (v: number) => B - ((v - mn) / (mx - mn)) * (B - T);
  const gridTicks = [mn, (mn + mx) / 2, mx].map((v) => Math.round(v));
  let d = "";
  points.forEach((p, i) => { if (p.v != null) d += (d ? "L" : "M") + x(i).toFixed(1) + " " + y(p.v).toFixed(1) + " "; });
  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Calciner outlet temperature trend against the operating band">
      <rect x={L} y={y(hi)} width={R - L} height={y(lo) - y(hi)} fill="rgba(79,184,138,.13)" />
      {[hi, lo].map((v, i) => (
        <line key={i} x1={L} y1={y(v)} x2={R} y2={y(v)} stroke="#4FB88A" strokeWidth={1} strokeDasharray="4 4" opacity={0.75} />
      ))}
      {gridTicks.map((v, i) => (
        <g key={i}>
          <line x1={L} y1={y(v)} x2={R} y2={y(v)} stroke="#273239" strokeWidth={1} />
          <text x={L - 9} y={y(v) + 4} textAnchor="end" fontFamily="IBM Plex Mono,monospace" fontSize={11} fill="#6D7B85">{v}</text>
        </g>
      ))}
      <path d={d} fill="none" stroke="#E9A13B" strokeWidth={1.8} strokeLinejoin="round" />
      {marks.map((i) => points[i]?.v != null && (
        <circle key={i} cx={x(i)} cy={y(points[i].v as number)} r={3.6} fill="#11161A" stroke="#5E9CD3" strokeWidth={2} />
      ))}
    </svg>
  );
}

export default function Advisor({ latest, adv }: { latest: any; adv: any }) {
  const [view, setView] = useState<"recs" | "dash">("recs");
  const [openRows, setOpenRows] = useState<Record<string, boolean>>({});
  const [decisionState, setDecisionState] = useState<"pending" | "accepted" | "declined">("pending");
  const [decisionTs, setDecisionTs] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [declining, setDeclining] = useState(false);
  const [reasonPicked, setReasonPicked] = useState<string | null>(null);
  const [declineNote, setDeclineNote] = useState("");
  const [declineErr, setDeclineErr] = useState(false);
  const [toast, setToast] = useState<{ msg: string; kind: "ok" | "dec" } | null>(null);
  const [reasons, setReasons] = useState<string[]>([]);
  const [history, setHistory] = useState<any[]>([]);
  const [trendPoints, setTrendPoints] = useState<{ t: string; v: number | null }[]>([]);
  const [prevKey, setPrevKey] = useState<string | null>(null);

  useEffect(() => { api.declineReasons().then((r) => setReasons(r.reasons)).catch(() => {}); }, []);

  const loadDash = () => {
    api.advisoryDecisions(50).then((r) => setHistory(r.decisions || [])).catch(() => {});
    if (adv?.target_band) {
      api.trend("423CI400_TT_4", 720).then((r) => setTrendPoints(r.points || [])).catch(() => {});
    }
  };
  useEffect(() => { if (view === "dash") loadDash(); }, [view]);

  useEffect(() => {
    if (!adv) return;
    const key = `${adv.ts}|${JSON.stringify(adv.moves)}`;
    if (prevKey !== null && prevKey !== key) {
      setDecisionState("pending"); setDecisionTs(null); setDeclining(false);
      setReasonPicked(null); setDeclineNote(""); setDeclineErr(false);
    }
    setPrevKey(key);
  }, [adv?.ts, adv?.moves]);

  if (!latest || !adv) return <div className="empty">Building operator advisory…</div>;

  const showToast = (msg: string, kind: "ok" | "dec") => {
    setToast({ msg, kind });
    setTimeout(() => setToast(null), 2600);
  };

  const submit = async (decision: "accepted" | "declined") => {
    if (decision === "declined" && !reasonPicked) { setDeclineErr(true); return; }
    setSubmitting(true);
    try {
      await api.advisoryDecision({
        decision,
        operating_state: adv.operating_state,
        mode: adv.mode,
        target_band_low: adv.target_band?.low,
        target_band_high: adv.target_band?.high,
        live_actual_temp: adv.live_actual_temp,
        predicted_temp: adv.predicted_temp,
        moves: adv.moves,
        reasoning: buildWhy(adv),
        reason_category: decision === "declined" ? reasonPicked ?? undefined : undefined,
        note: decision === "declined" ? declineNote || undefined : undefined,
      });
      setDecisionState(decision);
      setDecisionTs(new Date().toTimeString().slice(0, 5));
      setDeclining(false);
      showToast(
        decision === "accepted" ? "Recommendation accepted — logged for the record." : "Decline logged — " + reasonPicked,
        decision === "accepted" ? "ok" : "dec"
      );
      if (view === "dash") loadDash();
    } catch (e) {
      console.error(e);
    } finally {
      setSubmitting(false);
    }
  };

  const band = adv.target_band;
  const predTemp = adv.baseline?.predicted_temp ?? adv.predicted_temp;
  const liveTemp = adv.live_actual_temp;
  const horizonMin = adv.baseline?.horizon_min;
  const inBand = predTemp != null && predTemp >= band.low && predTemp <= band.high;
  const dirTxt = !inBand
    ? predTemp > band.high
      ? `Predicted to run ${fmt(predTemp - band.high, 1)} °C over the upper limit — the recommendation below eases firing.`
      : `Predicted to fall ${fmt(band.low - predTemp, 1)} °C under the lower limit — the recommendation below increases firing.`
    : `Holding ${fmt(Math.min(predTemp - band.low, band.high - predTemp), 1)} °C inside the band.`;

  const openCount = adv.feasible && decisionState === "pending" ? 1 : 0;

  return (
    <div className="advisor">
      <div className="tabs">
        <button className={`tabbtn${view === "recs" ? " is-active" : ""}`} onClick={() => setView("recs")}>
          Recommendations <span className="rail__badge">{openCount}</span>
        </button>
        <button className={`tabbtn${view === "dash" ? " is-active" : ""}`} onClick={() => setView("dash")}>
          Shift dashboard
        </button>
      </div>

      {view === "recs" ? (
        <section>
          {adv.locked_decline && (
            <div className="decl" style={{ display: "block", marginBottom: 16 }}>
              <h3>Locked — an operator declined this same situation before</h3>
              <p style={{ margin: 0, fontSize: 13.5, color: "var(--ink)", lineHeight: 1.6 }}>
                On {adv.locked_decline.ts}, with temperature {adv.locked_decline.direction === "within" ? "inside" : adv.locked_decline.direction} the band
                (then {fmt(adv.locked_decline.live_actual_temp, 0)} °C), a similar move was declined
                {adv.locked_decline.reason_category ? <> — <b>{adv.locked_decline.reason_category}</b></> : ""}
                {adv.locked_decline.note ? <>: &ldquo;{adv.locked_decline.note}&rdquo;</> : "."}
                {" "}This is why the plant is still run this way now; it clears the next time an operator accepts a similar move.
              </p>
            </div>
          )}

          <div className="hero">
            <div className="hero__read">
              <div className="hero__label">Calciner temperature{horizonMin ? `, predicted ${horizonMin} min ahead` : ", predicted"}</div>
              <div className="hero__val"><b className="num">{fmt(predTemp, 1)}</b><em>°C</em></div>
              <div className="hero__trend">{dirTxt}</div>
              <div className="hero__foot">
                <div>Live reading<b className="num">{fmt(liveTemp, 1)} °C</b></div>
                <div>Band<b className="num">{band.low} – {band.high}</b></div>
                <div>Uncertainty<b className="num">{fmt(adv.baseline?.p10, 0)}–{fmt(adv.baseline?.p90, 0)}</b></div>
              </div>
            </div>
            <div className="hero__band">
              <div className="hero__bandtop">
                <h2>Position in operating band</h2>
                <span className={`chip chip--${inBand ? "in" : "out"}`}>{inBand ? "Inside band" : "Outside band"}</span>
              </div>
              {liveTemp != null && predTemp != null && <BandGauge live={liveTemp} pred={predTemp} lo={band.low} hi={band.high} />}
            </div>
          </div>

          <div className="sec">
            <h2>Setpoint recommendations</h2>
            <p>{adv.feasible ? `${adv.moves.length} levers in this move · ${adv.mode === "hold" ? "holds the band" : "approaches the band"}` : "No safe move available"}</p>
          </div>

          {!adv.feasible ? (
            <div className="row row--hold"><div className="row__body" style={{ display: "block" }}>
              <p style={{ color: "var(--red)", margin: 0 }}>{adv.reason}</p>
            </div></div>
          ) : (
            <>
              <div className="list">
                {adv.moves.map((mv: any) => {
                  const dir = dirOf(mv.delta);
                  const open = !!openRows[mv.tag];
                  const cls = `row row--${dir}${open ? " is-open" : ""}` +
                    (decisionState === "accepted" ? " row--accepted" : decisionState === "declined" ? " row--declined" : "");
                  const tag = decisionState === "accepted" ? <span className="tag tag--acc">Accepted</span>
                    : decisionState === "declined" ? <span className="tag tag--dec">Declined</span>
                    : <span className="tag">{dir === "up" ? "Raise" : dir === "down" ? "Lower" : "Hold"}</span>;
                  const drivers = buildDrivers(adv, mv);
                  return (
                    <div className={cls} key={mv.tag}>
                      <button className="row__head" aria-expanded={open}
                        onClick={() => setOpenRows((s) => ({ ...s, [mv.tag]: !s[mv.tag] }))}>
                        <span className="row__name"><b>{mv.name}</b><span>Tag {mv.tag}</span></span>
                        <span className="cell"><span className="cell__k">Current</span><span className="cell__v num">{fmt(mv.from, 2)}</span></span>
                        <span className="arrow">
                          <svg width="16" height="10" viewBox="0 0 16 10" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M1 5h13M10.5 1.5 14 5l-3.5 3.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
                        </span>
                        <span className="cell"><span className="cell__k">Recommended</span><span className="cell__v cell__v--rec num">{fmt(mv.to, 2)}</span></span>
                        <span className={`delta delta--${dir}`}><span className="cell__k">Change</span>{mv.delta > 0 ? "+" : ""}{fmt(mv.delta, 2)}</span>
                        {tag}
                        <span className="caret">
                          <svg width="8" height="12" viewBox="0 0 8 12" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="m1.5 1 5 5-5 5" strokeLinecap="round" strokeLinejoin="round" /></svg>
                        </span>
                      </button>
                      {open && (
                        <div className="row__body" style={{ display: "block" }}>
                          <div className="why">
                            <div>
                              <h3>Why the model is suggesting this</h3>
                              <p>{buildWhy(adv)}</p>
                              <div className="impact">
                                <div>Predicted temp<b>{fmt(adv.predicted_temp, 0)} °C</b></div>
                                <div>Coal firing<b>{adv.saving?.coal_tph > 0 ? "−" : "+"}{fmt(Math.abs(adv.saving?.coal_tph ?? 0), 2)} t/h</b></div>
                                <div>Band status<b>{inBand ? "In band" : adv.mode === "approach" ? "Approaching" : "Out of band"}</b></div>
                              </div>
                            </div>
                            <div>
                              <h3>What&apos;s behind this move</h3>
                              <div className="drivers">
                                {drivers.map(([label, pct], i) => (
                                  <div className="driver" key={i}>
                                    <span>{label}</span><span className="driver__v">{pct}%</span>
                                    <span className="driver__bar"><i style={{ width: `${pct}%` }} /></span>
                                  </div>
                                ))}
                              </div>
                            </div>
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>

              <div className="row" style={{ marginTop: 10 }}>
                <div className="row__body" style={{ display: "block", borderTop: "none" }}>
                  {decisionState !== "pending" ? (
                    <div className="logged">
                      <span>{decisionState === "accepted"
                        ? <>Accepted at <b>{decisionTs}</b> — logged for the operator record.</>
                        : <>Declined at <b>{decisionTs}</b> — <b>{reasonPicked}</b>{declineNote ? <>. Note: {declineNote}</> : "."}</>}</span>
                      <button className="btn btn--ghost" onClick={() => { setDecisionState("pending"); setDecisionTs(null); }}>Undo</button>
                    </div>
                  ) : !declining ? (
                    <div className="actions">
                      <button className="btn btn--accept" disabled={submitting} onClick={() => submit("accepted")}>Accept this move</button>
                      <button className="btn btn--decline" disabled={submitting} onClick={() => setDeclining(true)}>Decline</button>
                      <span className="actions__note">This applies to all {adv.moves.length} levers above as one coordinated move. Advisory only — nothing is written to any control system.</span>
                    </div>
                  ) : (
                    <div className={`decl${declineErr ? " has-err" : ""}`} style={{ display: "block" }}>
                      <h3>What is stopping this change?</h3>
                      <div className="reasons">
                        {reasons.map((r) => (
                          <label className="reason" key={r}>
                            <input type="radio" name="declineReason" checked={reasonPicked === r}
                              onChange={() => { setReasonPicked(r); setDeclineErr(false); }} />
                            {r}
                          </label>
                        ))}
                      </div>
                      <textarea placeholder="Add anything the model should learn from this (optional)"
                        value={declineNote} onChange={(e) => setDeclineNote(e.target.value)} />
                      <div className="decl__foot">
                        <button className="btn btn--decline btn--sm" disabled={submitting} onClick={() => submit("declined")}>Log decline</button>
                        <button className="btn btn--ghost" onClick={() => { setDeclining(false); setDeclineErr(false); }}>Cancel</button>
                        <span className="decl__err">Pick a reason so the model can learn from it.</span>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </>
          )}
        </section>
      ) : (
        <section>
          <div className="sec" style={{ marginTop: 0 }}>
            <h2>Shift summary</h2>
            <p>Real decisions logged this session, plus recent live trend</p>
          </div>
          {(() => {
            const accepted = history.filter((h) => h.decision === "accepted").length;
            const declined = history.filter((h) => h.decision === "declined").length;
            const rate = accepted + declined ? Math.round((accepted / (accepted + declined)) * 100) : null;
            const vals = trendPoints.map((p) => p.v).filter((v): v is number => v != null);
            const inBandPct = vals.length
              ? (vals.filter((v) => v >= band.low && v <= band.high).length / vals.length) * 100 : null;
            const curErr = predTemp != null && liveTemp != null ? Math.abs(predTemp - liveTemp) : null;
            const tsr = adv.breakdown?.tsr?.now_pct;
            const stats: [string, string, string][] = [
              ["Recommendations logged", String(history.length), ""],
              ["Accepted", String(accepted), rate != null ? `${rate}% of those actioned` : ""],
              ["Declined", String(declined), "reasons captured for retraining"],
              ["Time inside band (12h)", inBandPct != null ? inBandPct.toFixed(1) : "—", "of live/historical trend"],
              ["Current tracking error", curErr != null ? curErr.toFixed(1) : "—", "predicted vs live, °C now"],
              ["Thermal substitution (TSR)", tsr != null ? tsr.toFixed(1) : "—", "current AFR share"],
            ];
            return (
              <>
                <div className="stats">
                  {stats.map(([k, v, d], i) => (
                    <div className="stat" key={i}>
                      <div className="stat__k">{k}</div>
                      <div className="stat__v num">{v}</div>
                      <div className="stat__d">{d}</div>
                    </div>
                  ))}
                </div>

                <div className="grid2" style={{ marginTop: 8 }}>
                  <div className="card" style={{ gridColumn: "1/-1" }}>
                    <div className="card__h">
                      <h3>Calciner outlet temperature, recent trend</h3>
                      <span>{inBandPct != null ? `${inBandPct.toFixed(1)}% of the window inside band` : "—"}</span>
                    </div>
                    <TrendChart points={trendPoints} lo={band.low} hi={band.high}
                      marks={history.filter((h) => h.decision === "accepted").map(() => 0).slice(0, 0)} />
                    <div className="legend">
                      <span><i style={{ background: "var(--amber)" }} />Actual</span>
                      <span><i style={{ background: "rgba(79,184,138,.28)" }} />Operating band {band.low}–{band.high} °C</span>
                    </div>
                  </div>

                  <div className="card">
                    <div className="card__h"><h3>Decisions by lever</h3><span>All logged</span></div>
                    <div className="hbars">
                      {["467RL400_TPH", "LAFR_FT", "K3V10_F1", "261WF080_TPH"].map((tag) => {
                        const rows = history.filter((h) => (h.moves || []).some((m: any) => m.tag === tag));
                        const a = rows.filter((h) => h.decision === "accepted").length;
                        const d = rows.filter((h) => h.decision === "declined").length;
                        const tot = a + d || 1;
                        const name = adv.moves.find((m: any) => m.tag === tag)?.name || tag;
                        return (
                          <div key={tag}>
                            <div className="hbar__t"><span>{name}</span><b>{a} / {a + d}</b></div>
                            <div className="hbar__track">
                              <i style={{ width: `${(a / tot) * 100}%`, background: "var(--green)" }} />
                              <i style={{ width: `${(d / tot) * 100}%`, background: "var(--red)" }} />
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>

                  <div className="card">
                    <div className="card__h"><h3>Why operators declined</h3><span>{declined} declines</span></div>
                    {declined === 0 ? <div className="empty">No declines logged yet.</div> : (
                      <div className="hbars">
                        {reasons.map((r) => {
                          const c = history.filter((h) => h.decision === "declined" && h.reason_category === r).length;
                          const maxR = Math.max(1, ...reasons.map((rr) => history.filter((h) => h.decision === "declined" && h.reason_category === rr).length));
                          return (
                            <div key={r}>
                              <div className="hbar__t"><span>{r}</span><b>{c}</b></div>
                              <div className="hbar__track"><i style={{ width: `${(c / maxR) * 100}%`, background: c === maxR && maxR > 0 ? "var(--red)" : "#6E4A44" }} /></div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>

                  <div className="card" style={{ gridColumn: "1/-1" }}>
                    <div className="card__h"><h3>Decisions this session</h3><span>{history.length} entries</span></div>
                    {!history.length ? (
                      <div className="empty">No decisions logged yet. Accept or decline a recommendation to start the trail.</div>
                    ) : (
                      <table>
                        <thead><tr><th>Time</th><th>State</th><th>Outcome</th><th>Reason</th></tr></thead>
                        <tbody>
                          {history.slice(0, 12).map((h, i) => (
                            <tr key={i}>
                              <td className="num">{h.ts}</td>
                              <td>{h.operating_state}</td>
                              <td><span className={`tag ${h.decision === "accepted" ? "tag--acc" : "tag--dec"}`}>{h.decision}</span></td>
                              <td style={{ color: "var(--ink-dim)" }}>{h.reason_category || "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </div>
                </div>
              </>
            );
          })()}
        </section>
      )}

      {toast && <div className={`toast show${toast.kind === "dec" ? " toast--dec" : ""}`}>{toast.msg}</div>}

      <style jsx>{`
        .advisor{
          --bg:#11161A; --panel:#192026; --panel-2:#202930; --panel-3:#27323A;
          --line:#2C3941; --line-soft:#232E35; --ink:#C4D0D9; --ink-bright:#EDF4F8;
          --ink-dim:#7B8A95; --ink-faint:#5A6771; --amber:#E9A13B; --green:#4FB88A;
          --red:#D9604F; --blue:#5E9CD3; --violet:#8E85C8;
          font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
          color:var(--ink); font-size:15px; line-height:1.5;
        }
        .advisor :global(.num){font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums;}
        .advisor button{font-family:inherit;font-size:inherit;color:inherit;background:none;border:none;padding:0;cursor:pointer;}
        .tabs{display:flex;gap:6px;margin-bottom:16px;}
        .tabbtn{padding:8px 14px;border-radius:4px;font-size:13.5px;font-weight:500;color:var(--ink-dim);border-left:2px solid transparent;display:flex;align-items:center;gap:8px;}
        .tabbtn:hover{background:#141B20;color:var(--ink);}
        .tabbtn.is-active{background:#182126;color:var(--ink-bright);border-left-color:var(--amber);}
        .rail__badge{font-family:"IBM Plex Mono",monospace;font-size:11.5px;background:var(--panel-3);color:var(--ink);padding:1px 6px;border-radius:9px;}
        .hero{background:var(--panel);border:1px solid var(--line);border-radius:6px;display:grid;grid-template-columns:minmax(260px,320px) 1fr;overflow:hidden;}
        .hero__read{padding:20px 24px;border-right:1px solid var(--line);}
        .hero__label{font-size:12.5px;color:var(--ink-dim);margin-bottom:2px;}
        .hero__val{display:flex;align-items:baseline;gap:8px;margin:6px 0 2px;}
        .hero__val b{font-size:58px;font-weight:500;line-height:1;letter-spacing:-.02em;color:var(--ink-bright);}
        .hero__val em{font-style:normal;font-size:19px;color:var(--ink-dim);}
        .hero__trend{font-size:13px;color:var(--ink-dim);}
        .hero__foot{margin-top:16px;padding-top:13px;border-top:1px solid var(--line-soft);display:flex;gap:22px;flex-wrap:wrap;}
        .hero__foot div{font-size:12px;color:var(--ink-faint);}
        .hero__foot b{display:block;font-size:14px;color:var(--ink);font-weight:500;margin-top:1px;}
        .hero__band{padding:20px 26px 18px;display:flex;flex-direction:column;justify-content:center;}
        .hero__bandtop{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:14px;}
        .hero__bandtop h2{margin:0;font-size:14px;font-weight:600;color:var(--ink-bright);}
        .chip{font-size:12px;padding:3px 9px;border-radius:3px;font-weight:500;}
        .chip--in{background:rgba(79,184,138,.14);color:var(--green);}
        .chip--out{background:rgba(217,96,79,.15);color:var(--red);}
        .sec{display:flex;align-items:baseline;justify-content:space-between;margin:26px 2px 11px;flex-wrap:wrap;gap:4px;}
        .sec h2{margin:0;font-size:15px;font-weight:600;color:var(--ink-bright);}
        .sec p{margin:0;font-size:12.5px;color:var(--ink-faint);}
        .list{display:flex;flex-direction:column;gap:6px;}
        .row{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--ink-faint);border-radius:4px;overflow:hidden;}
        .row--up{border-left-color:var(--amber);}
        .row--down{border-left-color:var(--blue);}
        .row--hold{border-left-color:var(--ink-faint);}
        .row--accepted{border-left-color:var(--green);background:#161E1B;}
        .row--declined{border-left-color:var(--red);background:#1D1817;}
        .row__head{width:100%;display:grid;grid-template-columns:minmax(140px,1fr) 84px 20px 84px 64px 74px 16px;align-items:center;gap:8px;padding:12px 14px 12px 15px;text-align:left;}
        .row__head:hover{background:rgba(255,255,255,.022);}
        .row__name{min-width:0;}
        .row__name b{display:block;font-size:14.5px;font-weight:500;color:var(--ink-bright);}
        .row__name span{font-size:11.5px;color:var(--ink-faint);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block;}
        .cell{text-align:right;}
        .cell__k{font-size:11px;color:var(--ink-faint);display:block;line-height:1.3;}
        .cell__v{font-size:15px;font-weight:500;color:var(--ink);letter-spacing:-.01em;}
        .cell__v--rec{color:var(--ink-bright);}
        .arrow{color:var(--ink-faint);display:flex;justify-content:center;}
        .delta{font-size:13px;text-align:right;}
        .delta--up{color:var(--amber);}
        .delta--down{color:var(--blue);}
        .delta--hold{color:var(--ink-faint);}
        .caret{display:flex;justify-content:center;color:var(--ink-faint);}
        .tag{display:inline-block;font-size:11px;padding:2px 7px;border-radius:3px;background:var(--panel-3);color:var(--ink-dim);white-space:nowrap;}
        .tag--acc{background:rgba(79,184,138,.14);color:var(--green);}
        .tag--dec{background:rgba(217,96,79,.15);color:var(--red);}
        .row__body{border-top:1px solid var(--line-soft);padding:16px 18px;}
        .why{display:grid;grid-template-columns:1fr 260px;gap:26px;}
        .why h3{margin:0 0 6px;font-size:12.5px;font-weight:600;color:var(--ink-dim);}
        .why p{margin:0;font-size:14px;line-height:1.62;color:var(--ink);}
        .drivers{display:flex;flex-direction:column;gap:8px;}
        .driver{display:grid;grid-template-columns:1fr 40px;align-items:center;gap:10px;font-size:12.5px;color:var(--ink-dim);}
        .driver__bar{height:5px;background:var(--panel-3);border-radius:3px;overflow:hidden;grid-column:1/-1;}
        .driver__bar i{display:block;height:100%;background:var(--violet);border-radius:3px;}
        .driver__v{text-align:right;color:var(--ink);}
        .impact{display:flex;gap:22px;margin-top:15px;padding-top:13px;border-top:1px solid var(--line-soft);flex-wrap:wrap;}
        .impact div{font-size:11.5px;color:var(--ink-faint);}
        .impact b{display:block;font-size:14px;color:var(--ink);font-weight:500;margin-top:2px;}
        .actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;}
        .btn{padding:8px 18px;border-radius:4px;font-size:14px;font-weight:500;}
        .btn--accept{background:var(--green);color:#0B1512;font-weight:600;}
        .btn--accept:hover{background:#5ecb99;}
        .btn--decline{border:1px solid var(--line);color:var(--ink);}
        .btn--decline:hover{border-color:var(--red);color:var(--red);}
        .btn--ghost{color:var(--ink-dim);font-size:13px;}
        .btn--ghost:hover{color:var(--ink-bright);text-decoration:underline;}
        .btn--sm{padding:7px 14px;font-size:13px;}
        .btn[disabled]{opacity:.4;cursor:not-allowed;}
        .actions__note{font-size:12.5px;color:var(--ink-faint);}
        .decl{margin-top:0;padding:15px;background:#1C1615;border:1px solid #3A2A27;border-radius:4px;}
        .decl h3{margin:0 0 11px;font-size:13.5px;font-weight:600;color:var(--ink-bright);}
        .reasons{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:7px;}
        .reason{display:flex;align-items:center;gap:9px;padding:9px 11px;border:1px solid var(--line);border-radius:4px;cursor:pointer;font-size:13.5px;background:var(--panel);}
        .reason:hover{border-color:#4A3A36;}
        .reason input{accent-color:var(--red);margin:0;flex:0 0 auto;}
        .decl textarea{width:100%;margin-top:10px;background:var(--panel);border:1px solid var(--line);border-radius:4px;color:var(--ink);padding:9px 11px;font-family:inherit;font-size:13.5px;resize:vertical;min-height:58px;}
        .decl textarea:focus{border-color:var(--blue);outline:none;}
        .decl__foot{display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap;}
        .decl__err{font-size:12.5px;color:var(--red);display:none;}
        .decl.has-err .decl__err{display:block;}
        .logged{display:flex;align-items:center;gap:10px;font-size:13.5px;color:var(--ink-dim);flex-wrap:wrap;}
        .logged b{color:var(--ink-bright);font-weight:500;}
        .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:8px;}
        .stat{background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:14px 16px;}
        .stat__k{font-size:12px;color:var(--ink-dim);}
        .stat__v{font-size:28px;font-weight:500;color:var(--ink-bright);line-height:1.2;margin-top:3px;letter-spacing:-.02em;}
        .stat__d{font-size:11.5px;color:var(--ink-faint);margin-top:2px;}
        .grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
        .card{background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:16px 18px;min-width:0;}
        .card__h{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:14px;gap:12px;}
        .card__h h3{margin:0;font-size:14px;font-weight:600;color:var(--ink-bright);}
        .card__h span{font-size:12px;color:var(--ink-faint);}
        .legend{display:flex;gap:14px;font-size:12px;color:var(--ink-dim);margin-top:11px;flex-wrap:wrap;}
        .legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:baseline;}
        .advisor :global(svg){display:block;max-width:100%;height:auto;}
        .hbars{display:flex;flex-direction:column;gap:11px;}
        .hbar__t{display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:5px;}
        .hbar__t span{color:var(--ink);}
        .hbar__t b{font-weight:500;color:var(--ink-dim);}
        .hbar__track{height:8px;background:var(--panel-3);border-radius:2px;display:flex;overflow:hidden;}
        .hbar__track i{display:block;height:100%;}
        table{width:100%;border-collapse:collapse;font-size:13px;}
        th{text-align:left;font-size:11.5px;color:var(--ink-faint);font-weight:500;padding:0 10px 8px 0;border-bottom:1px solid var(--line);}
        td{padding:9px 10px 9px 0;border-bottom:1px solid var(--line-soft);color:var(--ink);}
        tr:last-child td{border-bottom:none;}
        .empty{padding:26px 0;text-align:center;color:var(--ink-faint);font-size:13.5px;}
        .toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);background:var(--panel-3);border:1px solid var(--line);border-left:3px solid var(--green);color:var(--ink-bright);padding:11px 18px;border-radius:4px;font-size:13.5px;box-shadow:0 8px 28px rgba(0,0,0,.45);z-index:40;}
        .toast--dec{border-left-color:var(--red);}
        @media (max-width:1100px){
          .hero{grid-template-columns:1fr;}
          .hero__read{border-right:none;border-bottom:1px solid var(--line);}
          .grid2{grid-template-columns:1fr;}
          .why{grid-template-columns:1fr;gap:16px;}
        }
        @media (max-width:1180px){
          .row__head{grid-template-columns:1fr auto;gap:8px;row-gap:6px;}
          .arrow{display:none;}
        }
      `}</style>
    </div>
  );
}
