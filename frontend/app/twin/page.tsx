"use client";
import { useEffect, useMemo, useState } from "react";
import { Area, ComposedChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, ReferenceArea } from "recharts";
import { api, TwinMetrics, TwinBacktest, ShadowResult, DecisionsResult, TwinFuture } from "@/lib/api";
import Mimic from "@/components/Mimic";
import { Panel, Stat, Badge, Loading } from "@/components/ui";

export default function DigitalTwin() {
  const [latest, setLatest] = useState<any>(null);
  const [ov, setOv] = useState<any>(null);
  const [metrics, setMetrics] = useState<TwinMetrics | null>(null);
  const [metricsErr, setMetricsErr] = useState(false);
  const [bt, setBt] = useState<TwinBacktest | null>(null);
  const [btErr, setBtErr] = useState(false);
  const [shadow, setShadow] = useState<ShadowResult | null>(null);
  const [decisions, setDecisions] = useState<DecisionsResult | null>(null);
  const [future, setFuture] = useState<TwinFuture | null>(null);
  const [futureErr, setFutureErr] = useState(false);

  const load = () => {
    api.latest().then(setLatest).catch(console.error);
    api.overview().then(setOv).catch(console.error);
    api.shadow().then(setShadow).catch(() => {});
    api.decisions().then(setDecisions).catch(() => {});
    api.twinFuture().then((f) => { setFuture(f); setFutureErr(false); }).catch(() => setFutureErr(true));
  };
  useEffect(() => { load(); const id = setInterval(load, 2500); return () => clearInterval(id); }, []);

  // Model quality + backtest load once (static held-out evaluation).
  useEffect(() => {
    api.twinMetrics().then(setMetrics).catch(() => setMetricsErr(true));
    api.twinBacktest({ max_points: 800 }).then(setBt).catch(() => setBtErr(true));
  }, []);

  const chartData = useMemo(
    () => (bt?.series || []).map((p) => ({
      ts: p.ts, actual: p.actual, pred: p.pred,
      miss: p.actual != null && p.pred != null
        ? [Math.min(p.actual, p.pred), Math.max(p.actual, p.pred)]
        : null,
    })),
    [bt]);
  // OVERLAP = ACCURACY: every matured prediction is paired with the realised
  // temperature at the SAME timestamp (its target time), so the two curves
  // are directly comparable point-for-point — the shaded strip between them
  // is the error. The last ~5 min (not yet matured) show forecast only.
  const shadowChart = useMemo(() => {
    return (shadow?.series || [])
      .filter((p) => p.horizon_ts && p.pred_mean != null)
      .map((p) => {
        const actual = p.matured && p.actual_future != null ? p.actual_future : null;
        return {
          t: p.horizon_ts,
          pred: p.pred_mean,
          actual,
          miss: actual != null && p.pred_mean != null
            ? [Math.min(p.pred_mean, actual), Math.max(p.pred_mean, actual)]
            : null,
        };
      })
      .sort((a, b) => (a.t < b.t ? -1 : 1))
      .map((b) => ({ ...b, ts: String(b.t).slice(11, 16) }));
  }, [shadow]);

  if (!latest || !ov) return <Loading label="Injecting live plant data into the twin…" />;
  const src = latest.source;
  const feedBadge = src === "opc"
    ? { tone: "ok", text: `● LIVE · OPC · ${latest.age_sec ?? "?"}s ago` }
    : src === "replay"
    ? { tone: "warn", text: "REPLAY (recent history)" }
    : { tone: "muted", text: "historical snapshot" };

  // Twin = the plant horizon_min ahead. Falls back to the live mimic if the
  // twin endpoint is unavailable (and says so).
  const ahead = !!future && !futureErr;
  const h = future?.horizon_min ?? 5;
  const mimicValues = ahead
    ? Object.fromEntries(Object.entries(future!.values).map(([t, v]) => [t, v.twin]))
    : latest.values;
  const mimicLive = ahead
    ? Object.fromEntries(Object.entries(future!.values).map(([t, v]) => [t, v.live]))
    : undefined;
  const mimicBasis = ahead
    ? Object.fromEntries(Object.entries(future!.values).map(([t, v]) => [t, v.basis]))
    : undefined;
  const band = ahead ? future!.target_band : ov.target_band;
  const allOk = ahead ? future!.all_interlocks_ok : ov.interlocks.every((i: any) => i.pass !== false);
  const active = (future?.setpoints ?? []).filter((s) => s.status === "active");
  const hhmmss = (ts?: string | null) => (ts ? String(ts).replace(" ", "T").slice(11, 19) : "—");

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold flex items-center gap-2">
            <span className="live-dot text-[var(--ok)]">●</span>
            {ahead ? `Digital Twin — Kiln-3 at T+${h} min` : "Digital Twin — Kiln-3 Process Mimic (live)"}
          </h1>
          <div className="text-sm text-[var(--muted)] mono">
            {ahead
              ? <>twin time {hhmmss(future!.twin_ts)} · live plant {hhmmss(future!.live_ts)} · {future!.method}</>
              : <>snapshot · {latest.ts}{futureErr ? " · twin ahead-view unavailable, showing live" : ""}</>}
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <Badge tone={feedBadge.tone}>{feedBadge.text}</Badge>
          {ahead && <Badge tone={active.length ? "warn" : "muted"}>
            {active.length ? `${active.length} accepted setpoint${active.length > 1 ? "s" : ""} applied` : "no accepted setpoints"}
          </Badge>}
          <Badge tone="accent">{ahead ? future!.operating_state : ov.operating_state}</Badge>
          <Badge tone="muted">Target {band.low}–{band.high} °C</Badge>
          <Badge tone={allOk ? "ok" : "bad"}>
            {allOk ? (ahead ? `Interlocks OK at T+${h}` : "Interlocks OK") : (ahead ? `Interlock risk at T+${h}` : "Interlock breach")}
          </Badge>
        </div>
      </header>

      <Panel className="!p-2">
        <Mimic values={mimicValues} units={latest.units} band={band} mode={ahead ? "sim" : "live"}
          live={mimicLive} basis={mimicBasis} />
      </Panel>

      <div className="text-[11px] text-[var(--muted)]">
        {ahead
          ? <>The twin runs {h} min ahead of the plant. Bold values are the twin: the calciner outlet temperature and the
             safety analysers are its t+{h} prediction, levers carry any accepted advisor setpoint (amber outline) as if applied now.
             Tags the model does not predict (cyclone, cooler, feed-pipe temps…) are shown live. &quot;live X&quot; is the plant right now.</>
          : <>Every readout is your own historical/live plant data mapped onto the FLSmidth Kiln-3 flow sheet.
             The calciner outlet temperature is the controlled variable; the analyser boxes carry the combustion-safety interlocks.</>}
      </div>

      {/* ---- calciner temperature trajectory: t+5 / 10 / 15 / 30 ---- */}
      {ahead && (
        <Panel title="Calciner outlet temperature — twin forecast by horizon"
          right={<Badge tone="muted">band {band.low}–{band.high} °C · {future!.operating_state}</Badge>}>
          {!future!.trajectory?.length ? (
            <div className="text-sm text-[var(--muted)]">
              Multi-horizon forecast unavailable — it needs the trained GBT twin (current method: <span className="mono">{future!.method}</span>).
            </div>
          ) : (
            <table className="w-full text-xs">
              <thead className="text-[var(--muted)] text-left">
                <tr>
                  <th className="py-1 pr-3">Horizon</th><th className="pr-3">For time</th>
                  <th className="pr-3 text-right">{active.length ? "Twin (setpoints applied)" : "Twin"}</th>
                  <th className="pr-3 text-right">P10–P90</th>
                  {active.length > 0 && <><th className="pr-3 text-right">Live levers</th><th className="pr-3 text-right">Setpoint effect</th></>}
                  <th>Band</th>
                </tr>
              </thead>
              <tbody>
                {future!.trajectory!.map((p) => {
                  const b = future!.baseline_trajectory?.find((x) => x.min === p.min);
                  const inBand = p.mean >= band.low && p.mean <= band.high;
                  const at = (() => {
                    const d = new Date(String(future!.live_ts).replace(" ", "T").slice(0, 19));
                    if (isNaN(d.getTime())) return "—";
                    d.setMinutes(d.getMinutes() + p.min);
                    return d.toTimeString().slice(0, 8);
                  })();
                  return (
                    <tr key={p.min} className="border-t border-[var(--line)]">
                      <td className="py-1.5 pr-3 mono">T+{p.min} min</td>
                      <td className="pr-3 mono">{at}</td>
                      <td className="pr-3 text-right mono font-bold text-[var(--accent)]">{p.mean.toFixed(1)} °C</td>
                      <td className="pr-3 text-right mono text-[var(--muted)]">{p.p10.toFixed(0)}–{p.p90.toFixed(0)}</td>
                      {active.length > 0 && <>
                        <td className="pr-3 text-right mono">{b ? `${b.mean.toFixed(1)} °C` : "—"}</td>
                        <td className="pr-3 text-right mono">{b ? `${p.mean - b.mean >= 0 ? "+" : ""}${(p.mean - b.mean).toFixed(1)} °C` : "—"}</td>
                      </>}
                      <td><Badge tone={inBand ? "ok" : "warn"}>{inBand ? "in band" : p.mean < band.low ? "below" : "above"}</Badge></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          <div className="text-[11px] text-[var(--muted)] mt-2">
            One twin forecast per horizon from the live plant state{active.length ? ", with the accepted setpoints applied as a step now" : ""}.
            Uncertainty widens with the horizon (P10–P90).
            {(() => {
              const th = (metrics as any)?.temp_horizons as Record<string, { mae?: number }> | undefined;
              const parts = th ? Object.entries(th).filter(([, v]) => v?.mae != null)
                .map(([k, v]) => `T+${k} ${v.mae!.toFixed(1)}`) : [];
              return parts.length ? ` Held-out test MAE (°C): ${parts.join(" · ")}.` : null;
            })()}
          </div>
        </Panel>
      )}

      {/* ---- accepted setpoints driving the twin ---- */}
      {ahead && (
        <Panel title={`Accepted setpoints driving the twin (T+${h})`}
          right={<Badge tone={active.length ? "warn" : "muted"}>
            calciner t+{h}: {future!.prediction.mean.toFixed(1)} °C
            {active.length ? ` (${future!.setpoint_effect_c >= 0 ? "+" : ""}${future!.setpoint_effect_c.toFixed(1)} °C vs live levers)` : ""}
          </Badge>}>
          {future!.setpoints.length === 0 ? (
            <div className="text-sm text-[var(--muted)]">
              No accepted setpoints in the last 30 min — the twin runs on the live lever values. Accept a recommendation on the advisor and it is applied here on the next refresh.
            </div>
          ) : (
            <table className="w-full text-xs">
              <thead className="text-[var(--muted)] text-left">
                <tr><th className="py-1 pr-3">Lever</th><th className="pr-3 text-right">Accepted</th><th className="pr-3 text-right">Live now</th>
                  <th className="pr-3">Accepted at</th><th>Status</th></tr>
              </thead>
              <tbody>
                {future!.setpoints.map((s) => (
                  <tr key={s.tag} className="border-t border-[var(--line)]">
                    <td className="py-1.5 pr-3">{s.name ?? s.tag}</td>
                    <td className="pr-3 text-right mono">{s.accepted.toFixed(2)} {s.unit ?? ""}</td>
                    <td className="pr-3 text-right mono">{s.live != null ? s.live.toFixed(2) : "—"} {s.unit ?? ""}</td>
                    <td className="pr-3 mono">{hhmmss(s.decided_at)} ({s.age_min.toFixed(0)} min ago)</td>
                    <td>
                      <Badge tone={s.status === "active" ? "warn" : s.status === "reached" ? "ok" : "muted"}>
                        {s.status === "active" ? "applied to twin" : s.status === "reached" ? "plant reached it" : "expired (30 min)"}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div className="text-[11px] text-[var(--muted)] mt-2">
            An accepted setpoint drives the twin until the plant reaches it (within 0.5% of the lever&apos;s range), it is undone or replaced on the advisor, or 30 min pass. Advisory only — nothing is written to the plant.
          </div>
        </Panel>
      )}

      {/* ---- CCR operator decisions on the advisor's recommendations ---- */}
      <Panel title={`CCR operator decisions — from the advisor · ${decisions?.day ?? "today"}`}
        right={decisions && (
          <div className="flex items-center gap-2 flex-wrap">
            <Badge tone="ok">{decisions.counts.accepted} accepted</Badge>
            <Badge tone="bad">{decisions.counts.declined} declined</Badge>
            <Badge tone="muted">{decisions.counts.undone} undone</Badge>
          </div>
        )}>
        {!decisions ? (
          <div className="text-sm text-[var(--muted)]">Decision log unavailable.</div>
        ) : decisions.rows.length === 0 ? (
          <div className="text-sm text-[var(--muted)]">
            No operator decisions yet today. Accept or decline a recommendation on the advisor screen (<span className="mono">/advisor</span>) and it appears here.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-[var(--muted)] text-left">
                <tr>
                  <th className="py-1 pr-3">Time</th><th className="pr-3">Parameter</th><th className="pr-3">Decision</th>
                  <th className="pr-3 text-right">Current → Recommended</th><th className="pr-3">Reason / note</th>
                  <th className="pr-3 text-right">Twin t+{decisions.rows[0].horizon_min ?? 5} (with recs)</th><th>State</th>
                </tr>
              </thead>
              <tbody>
                {decisions.rows.slice(0, 25).map((d) => (
                  <tr key={d.id} className="border-t border-[var(--line)] align-top">
                    <td className="py-1.5 pr-3 mono">{d.decided_at.slice(11, 19)}</td>
                    <td className="pr-3">{d.param_name ?? d.tag}</td>
                    <td className="pr-3">
                      <Badge tone={d.action === "accepted" ? "ok" : d.action === "declined" ? "bad" : "muted"}>{d.action}</Badge>
                    </td>
                    <td className="pr-3 text-right mono whitespace-nowrap">
                      {d.current_value?.toFixed(2) ?? "—"} → {d.recommended_value?.toFixed(2) ?? "—"} {d.unit ?? ""}
                    </td>
                    <td className="pr-3">
                      {d.reason ?? "—"}{d.note ? <span className="text-[var(--muted)]"> · {d.note}</span> : null}
                    </td>
                    <td className="pr-3 text-right mono">{d.predicted_temp != null ? `${d.predicted_temp.toFixed(1)} °C` : "—"}</td>
                    <td className="text-[var(--muted)]">{d.operating_state ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="text-[11px] text-[var(--muted)] mt-2">
          Stored date-wise in the twin database (<span className="mono">operator_decisions</span>) with the twin&apos;s advisory context, as labelled data for future retraining. Nothing here is written to the plant.
        </div>
      </Panel>

      {/* ---- Shadow mode: twin advising beside the live CCR ---- */}
      {shadow?.available && shadow.current && (() => {
        const c = shadow.current!;
        const dcoal = c.delta_coal ?? 0;
        const dafr = c.delta_afr ?? 0;
        return (
          <Panel title="Shadow mode — twin advising beside the live CCR (observe-only)"
            right={
              <div className="flex items-center gap-2 flex-wrap">
                <Badge tone="ok"><span className="live-dot">●</span> LIVE</Badge>
                {(shadow.matured ?? 0) > 0 && (
                  <Badge tone="accent">
                    tracking MAE {shadow.running_mae?.toFixed(1)}°C · {((shadow.pct_within_5C ?? 0) * 100).toFixed(0)}% within 5°C · n={shadow.matured}
                  </Badge>
                )}
              </div>
            }>
            <div className="grid grid-cols-12 gap-4">
              <div className="col-span-12 lg:col-span-5 space-y-3">
                <div className="grid grid-cols-3 gap-2">
                  <Stat label="Live actual" unit="°C" value={c.actual_now?.toFixed(1)} tone="ok" />
                  <Stat label={`Twin t+${shadow.horizon_min ?? 5}min`} unit="°C" value={c.pred_mean?.toFixed(1)} tone="accent"
                    sub={c.pred_p10 != null ? `${c.pred_p10?.toFixed(0)}–${c.pred_p90?.toFixed(0)}` : undefined} />
                  <Stat label="Target band" unit="°C" value={c.band_lo != null ? `${c.band_lo}–${c.band_hi}` : "—"} tone="ink" sub={c.state ?? undefined} />
                </div>
                <div className="text-[11px] uppercase tracking-wide text-[var(--muted)]">Twin advisory right now — setpoints it would recommend</div>
                <div className="grid grid-cols-2 gap-2">
                  <Stat label="Calciner coal" unit="tph" value={c.rec_coal?.toFixed(1)} tone="accent"
                    sub={c.rec_coal != null ? `${dcoal >= 0 ? "+" : ""}${dcoal.toFixed(1)} vs current` : undefined} />
                  <Stat label="AFR total" unit="tph" value={c.rec_afr?.toFixed(0)} tone="accent"
                    sub={c.rec_afr != null ? `${dafr >= 0 ? "+" : ""}${dafr.toFixed(0)} vs current` : undefined} />
                </div>
                <div className="flex items-center gap-2 flex-wrap">
                  <Badge tone={c.in_band_now ? "ok" : "warn"}>{c.in_band_now ? "Now in band" : "Now off band"}</Badge>
                  <Badge tone={c.rec_in_band ? "ok" : "warn"}>{c.rec_in_band ? "Advisory reaches band" : "Advisory moves toward band"}</Badge>
                  <Badge tone={c.interlocks_ok ? "ok" : "bad"}>{c.interlocks_ok ? "Interlocks OK" : "Interlock risk"}</Badge>
                </div>
                <div className="text-[11px] text-[var(--muted)]">
                  Advisory only — every recommendation is logged beside the CCR and <b>never written</b> to the plant.
                </div>
              </div>
              <div className="col-span-12 lg:col-span-7">
                <div className="flex items-center gap-4 text-[11px] text-[var(--muted)] mb-2">
                  <span className="flex items-center gap-1"><span className="inline-block w-3 h-0.5" style={{ background: "#22c55e" }} />Actual (realised)</span>
                  <span className="flex items-center gap-1"><span className="inline-block w-3 h-0.5" style={{ background: "#38bdf8" }} />Twin forecast (made 5 min earlier)</span>
                  <span className="flex items-center gap-1"><span className="inline-block w-3 h-2" style={{ background: "rgba(248,113,113,0.3)" }} />miss (gap = error)</span>
                  <span className="flex items-center gap-1"><span className="inline-block w-3 h-2" style={{ background: "rgba(34,197,94,0.12)" }} />target band</span>
                </div>
                <div style={{ height: 220 }}>
                  <ResponsiveContainer>
                    <ComposedChart data={shadowChart} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
                      <CartesianGrid stroke="#223252" strokeDasharray="3 3" />
                      <XAxis dataKey="ts" tick={{ fill: "#8ea3c4", fontSize: 10 }} minTickGap={40} />
                      <YAxis domain={["auto", "auto"]} tick={{ fill: "#8ea3c4", fontSize: 10 }} />
                      <Tooltip contentStyle={{ background: "#16233d", border: "1px solid #223252", borderRadius: 8, color: "#e6edf7" }} />
                      {c.band_lo != null && c.band_hi != null && (
                        <ReferenceArea y1={c.band_lo} y2={c.band_hi} fill="#22c55e" fillOpacity={0.1} />
                      )}
                      <Area type="monotone" dataKey="miss" stroke="none" fill="#f87171" fillOpacity={0.3}
                        connectNulls name="Miss" isAnimationActive={false} />
                      <Line type="monotone" dataKey="actual" stroke="#22c55e" dot={false} strokeWidth={1.8}
                        connectNulls name="Actual" isAnimationActive={false} />
                      <Line type="monotone" dataKey="pred" stroke="#38bdf8" dot={false} strokeWidth={1.5}
                        strokeDasharray="4 2" connectNulls name="Twin forecast" isAnimationActive={false} />
                    </ComposedChart>
                  </ResponsiveContainer>
                </div>
                <div className="text-[11px] text-[var(--muted)] mt-2">
                  Both curves are drawn at the <b>same</b> moment: the forecast (made {shadow.horizon_min ?? 5} min earlier) against what the plant actually did at that minute. Where the curves overlap the twin was right; the red strip is exactly the miss. The blue tail past the green line is the next {shadow.horizon_min ?? 5} min — forecasts whose answers haven&apos;t happened yet.
                </div>
              </div>
            </div>
          </Panel>
        );
      })()}

      <div className="grid grid-cols-12 gap-4">
        {/* ---- Model quality panel ---- */}
        <Panel title="Model quality — calciner-temp twin (t+5 min)"
          className="col-span-12 lg:col-span-5"
          right={metrics?.trained_at && <Badge tone="muted">trained {String(metrics.trained_at).slice(0, 10)}</Badge>}>
          {metricsErr ? (
            <div className="text-sm text-[var(--muted)]">Metrics endpoint unavailable.</div>
          ) : !metrics ? (
            <Loading label="Loading model metrics…" />
          ) : metrics.available === false || !metrics.temp ? (
            <div className="text-sm text-[var(--muted)]">Twin model not trained yet — metrics appear after <span className="mono">twin.train_gbt</span> runs.</div>
          ) : (
            <>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-4">
                <Stat label="MAE" unit="°C" value={metrics.temp.mae?.toFixed(2)} tone="accent" />
                <Stat label="RMSE" unit="°C" value={metrics.temp.rmse?.toFixed(2)} tone="ink" />
                <Stat label="R²" value={metrics.temp.r2?.toFixed(3)} tone="ok" />
                <Stat label="P10–P90 cov." unit="%"
                  value={metrics.temp.interval_coverage != null ? (metrics.temp.interval_coverage * 100).toFixed(0) : "—"}
                  tone="ink" sub="target ≈ 80%" />
              </div>
              <div className="text-[11px] text-[var(--muted)] uppercase tracking-wide mb-2">GBT vs NN vs persistence</div>
              <table className="w-full text-xs">
                <thead className="text-[var(--muted)] text-left">
                  <tr><th className="py-1">Model</th><th className="text-right">MAE °C</th><th className="text-right">RMSE °C</th><th className="text-right">R²</th></tr>
                </thead>
                <tbody className="mono">
                  <tr className="border-t border-[var(--line)]">
                    <td className="py-1.5 text-[var(--accent)]">GBT (served)</td>
                    <td className="text-right">{metrics.temp.mae?.toFixed(2)}</td>
                    <td className="text-right">{metrics.temp.rmse?.toFixed(2)}</td>
                    <td className="text-right">{metrics.temp.r2?.toFixed(3)}</td>
                  </tr>
                  {metrics.nn?.temp && (
                    <tr className="border-t border-[var(--line)]">
                      <td className="py-1.5 text-[var(--ink)]">NN{metrics.nn.model ? ` (${metrics.nn.model})` : ""}</td>
                      <td className="text-right">{metrics.nn.temp.mae?.toFixed(2)}</td>
                      <td className="text-right">{metrics.nn.temp.rmse?.toFixed(2)}</td>
                      <td className="text-right">{metrics.nn.temp.r2 != null ? metrics.nn.temp.r2.toFixed(3) : "—"}</td>
                    </tr>
                  )}
                  {metrics.baseline_persistence?.temp && (
                    <tr className="border-t border-[var(--line)]">
                      <td className="py-1.5 text-[var(--muted)]">Persistence baseline</td>
                      <td className="text-right">{metrics.baseline_persistence.temp.mae?.toFixed(2)}</td>
                      <td className="text-right">{metrics.baseline_persistence.temp.rmse?.toFixed(2)}</td>
                      <td className="text-right text-[var(--muted)]">—</td>
                    </tr>
                  )}
                </tbody>
              </table>
              <div className="text-[11px] text-[var(--muted)] mt-2">
                {metrics.n_test != null ? `${metrics.n_test.toLocaleString()} held-out samples · ` : ""}lower MAE/RMSE and higher R² is better.
              </div>
            </>
          )}
        </Panel>

        {/* ---- Twin-vs-actual backtest chart ---- */}
        <Panel title="Twin vs actual — held-out backtest"
          className="col-span-12 lg:col-span-7"
          right={bt && <Badge tone="muted">n={bt.n} · MAE {bt.mae?.toFixed(1)}°C · R² {bt.r2?.toFixed(2)}</Badge>}>
          {btErr ? (
            <div className="text-sm text-[var(--muted)]">Backtest endpoint unavailable.</div>
          ) : !bt ? (
            <Loading label="Running backtest…" />
          ) : chartData.length === 0 ? (
            <div className="text-sm text-[var(--muted)]">No backtest data in the selected window.</div>
          ) : (
            <>
              <div className="flex items-center gap-4 text-[11px] text-[var(--muted)] mb-2">
                <span className="flex items-center gap-1"><span className="inline-block w-3 h-0.5" style={{ background: "#22c55e" }} />Actual</span>
                <span className="flex items-center gap-1"><span className="inline-block w-3 h-0.5" style={{ background: "#38bdf8" }} />Twin predicted</span>
                <span className="flex items-center gap-1"><span className="inline-block w-3 h-2" style={{ background: "rgba(248,113,113,0.3)" }} />miss (gap = error)</span>
              </div>
              <div style={{ height: 280 }}>
                <ResponsiveContainer>
                  <ComposedChart data={chartData} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
                    <CartesianGrid stroke="#223252" strokeDasharray="3 3" />
                    <XAxis dataKey="ts" tick={{ fill: "#8ea3c4", fontSize: 10 }} minTickGap={50} />
                    <YAxis domain={["auto", "auto"]} tick={{ fill: "#8ea3c4", fontSize: 10 }} />
                    <Tooltip contentStyle={{ background: "#16233d", border: "1px solid #223252", borderRadius: 8, color: "#e6edf7" }} />
                    <Area type="monotone" dataKey="miss" stroke="none" fill="#f87171" fillOpacity={0.3} connectNulls name="Miss" isAnimationActive={false} />
                    <Line type="monotone" dataKey="actual" stroke="#22c55e" dot={false} strokeWidth={1.6} name="Actual" isAnimationActive={false} />
                    <Line type="monotone" dataKey="pred" stroke="#38bdf8" dot={false} strokeWidth={1.6} strokeDasharray="4 2" name="Twin predicted" isAnimationActive={false} />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
              <div className="text-[11px] text-[var(--muted)] mt-2">
                {bt.tag} · a replay over the held-out historical window the model never trained on: at each minute the twin predicts t+5 from that moment&apos;s data, then the curve is compared with what really happened. Overlap = accuracy; the red strip is the miss.
              </div>
            </>
          )}
        </Panel>
      </div>
    </div>
  );
}
