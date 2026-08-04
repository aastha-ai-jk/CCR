"use client";
import { useEffect, useMemo, useState } from "react";
import { Area, ComposedChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { api, TwinMetrics, TwinBacktest, ShadowResult } from "@/lib/api";
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

  const load = () => {
    api.latest().then(setLatest).catch(console.error);
    api.overview().then(setOv).catch(console.error);
    api.shadow().then(setShadow).catch(() => {});
  };
  useEffect(() => { load(); const id = setInterval(load, 2500); return () => clearInterval(id); }, []);

  // Model quality + backtest load once (static held-out evaluation).
  useEffect(() => {
    api.twinMetrics().then(setMetrics).catch(() => setMetricsErr(true));
    api.twinBacktest({ max_points: 800 }).then(setBt).catch(() => setBtErr(true));
  }, []);

  const chartData = useMemo(
    () => (bt?.series || []).map((p) => ({ ts: p.ts, actual: p.actual, pred: p.pred, band: [p.p10, p.p90] })),
    [bt]);
  const shadowChart = useMemo(
    () => (shadow?.series || []).map((p) => ({
      ts: (p.ts || "").slice(11, 16), actual: p.actual_now, pred: p.pred_mean,
      matured: p.actual_future,
    })),
    [shadow]);

  if (!latest || !ov) return <Loading label="Injecting live plant data into the twin…" />;
  const allOk = ov.interlocks.every((i: any) => i.pass !== false);
  const src = latest.source;
  const feedBadge = src === "opc"
    ? { tone: "ok", text: `● LIVE · OPC · ${latest.age_sec ?? "?"}s ago` }
    : src === "replay"
    ? { tone: "warn", text: "REPLAY (recent history)" }
    : { tone: "muted", text: "historical snapshot" };

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold flex items-center gap-2">
            <span className="live-dot text-[var(--ok)]">●</span> Digital Twin — Kiln-3 Process Mimic
          </h1>
          <div className="text-sm text-[var(--muted)] mono">snapshot · {latest.ts}</div>
        </div>
        <div className="flex items-center gap-2">
          <Badge tone={feedBadge.tone}>{feedBadge.text}</Badge>
          <Badge tone="accent">{ov.operating_state}</Badge>
          <Badge tone="muted">Target {ov.target_band.low}–{ov.target_band.high} °C</Badge>
          <Badge tone={allOk ? "ok" : "bad"}>{allOk ? "Interlocks OK" : "Interlock breach"}</Badge>
        </div>
      </header>

      <Panel className="!p-2">
        <Mimic values={latest.values} units={latest.units} band={ov.target_band} mode="live" />
      </Panel>

      <div className="text-[11px] text-[var(--muted)]">
        Every readout is your own historical/live plant data mapped onto the FLSmidth Kiln-3 flow sheet.
        The calciner outlet temperature is the controlled variable; the analyser boxes carry the combustion-safety interlocks.
      </div>

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
                  <span className="flex items-center gap-1"><span className="inline-block w-3 h-0.5" style={{ background: "#22c55e" }} />Live actual</span>
                  <span className="flex items-center gap-1"><span className="inline-block w-3 h-0.5" style={{ background: "#38bdf8" }} />Twin t+{shadow.horizon_min ?? 5}min prediction</span>
                </div>
                <div style={{ height: 220 }}>
                  <ResponsiveContainer>
                    <ComposedChart data={shadowChart} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
                      <CartesianGrid stroke="#223252" strokeDasharray="3 3" />
                      <XAxis dataKey="ts" tick={{ fill: "#8ea3c4", fontSize: 10 }} minTickGap={40} />
                      <YAxis domain={["auto", "auto"]} tick={{ fill: "#8ea3c4", fontSize: 10 }} />
                      <Tooltip contentStyle={{ background: "#16233d", border: "1px solid #223252", borderRadius: 8, color: "#e6edf7" }} />
                      <Line type="monotone" dataKey="actual" stroke="#22c55e" dot={false} strokeWidth={1.6} name="Live actual" isAnimationActive={false} />
                      <Line type="monotone" dataKey="pred" stroke="#38bdf8" dot={false} strokeWidth={1.6} strokeDasharray="4 2" name="Twin predicted" isAnimationActive={false} />
                    </ComposedChart>
                  </ResponsiveContainer>
                </div>
                <div className="text-[11px] text-[var(--muted)] mt-2">
                  The twin predicts calciner temperature {shadow.horizon_min ?? 5} min ahead on every live tick; predictions mature against the realised value to give the live tracking accuracy above.
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
                <span className="flex items-center gap-1"><span className="inline-block w-3 h-2" style={{ background: "rgba(56,189,248,0.15)" }} />P10–P90</span>
              </div>
              <div style={{ height: 280 }}>
                <ResponsiveContainer>
                  <ComposedChart data={chartData} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
                    <CartesianGrid stroke="#223252" strokeDasharray="3 3" />
                    <XAxis dataKey="ts" tick={{ fill: "#8ea3c4", fontSize: 10 }} minTickGap={50} />
                    <YAxis domain={["auto", "auto"]} tick={{ fill: "#8ea3c4", fontSize: 10 }} />
                    <Tooltip contentStyle={{ background: "#16233d", border: "1px solid #223252", borderRadius: 8, color: "#e6edf7" }} />
                    <Area type="monotone" dataKey="band" stroke="none" fill="#38bdf8" fillOpacity={0.14} name="P10–P90" isAnimationActive={false} />
                    <Line type="monotone" dataKey="actual" stroke="#22c55e" dot={false} strokeWidth={1.6} name="Actual" isAnimationActive={false} />
                    <Line type="monotone" dataKey="pred" stroke="#38bdf8" dot={false} strokeWidth={1.6} strokeDasharray="4 2" name="Twin predicted" isAnimationActive={false} />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
              <div className="text-[11px] text-[var(--muted)] mt-2">
                {bt.tag} · held-out window · twin prediction at t+5 min vs the realised actual.
              </div>
            </>
          )}
        </Panel>
      </div>
    </div>
  );
}
