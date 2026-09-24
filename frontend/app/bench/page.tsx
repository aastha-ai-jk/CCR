"use client";
import { useEffect, useMemo, useState } from "react";
import { ComposedChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { api, BenchResult } from "@/lib/api";
import { Panel, Stat, Badge, Loading } from "@/components/ui";

const COLOR: Record<string, string> = {
  actual: "#22c55e", gbt: "#38bdf8", arx: "#f59e0b", gru: "#a78bfa", persist: "#64748b",
};
const ORDER = ["gbt", "arx", "gru"];

export default function ModelBench() {
  const [r, setR] = useState<BenchResult | null>(null);
  const [err, setErr] = useState(false);
  useEffect(() => { api.bench().then(setR).catch(() => setErr(true)); }, []);

  const chart = useMemo(() => (r?.overlay || []).map((p) => ({ ...p, ts: String(p.ts).slice(5) })), [r]);

  if (err) return <div className="text-sm text-[var(--muted)]">Bench endpoint unavailable.</div>;
  if (!r) return <Loading label="Loading model bench…" />;
  if (!r.available || !r.models) {
    return <Panel title="Model Bench"><div className="text-sm text-[var(--muted)]">
      No results yet — run <span className="mono">python -m twin.bench</span> on the server. {r.note}</div></Panel>;
  }

  const models = r.models, labels = r.labels || {}, base = r.baseline_persistence!;
  const present = ORDER.filter((k) => models[k]);
  const w = r.winner!;

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold">Model Bench — 3-approach training bake-off</h1>
          <div className="text-sm text-[var(--muted)] mono">
            {r.dataset?.csv} · {r.dataset?.rows_after_gate?.toLocaleString()} rows · {r.dataset?.span?.[0]?.slice(0, 10)} → {r.dataset?.span?.[1]?.slice(0, 10)} · target {r.dataset?.target} @ t+{r.dataset?.horizon_min} min
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Badge tone="muted">test n={r.eval?.n_test?.toLocaleString()}</Badge>
          <Badge tone={r.eval?.identical_rows ? "ok" : "warn"}>identical test rows</Badge>
          <Badge tone="muted">generated {String(r.generated_at).slice(0, 16)}</Badge>
        </div>
      </header>

      {/* verdict */}
      <Panel className="!py-4" title="Verdict — best by held-out accuracy">
        <div className="flex items-center gap-4 flex-wrap">
          <span className="text-2xl">🏆</span>
          <div>
            <div className="text-lg font-bold" style={{ color: COLOR[w.key] }}>{w.label}</div>
            <div className="text-sm text-[var(--muted)]">
              MAE <span className="mono text-[var(--ink)]">{w.mae.toFixed(2)} °C</span> on {r.eval?.n_test?.toLocaleString()} unseen samples ·
              <span className="text-[var(--ok)]"> {w.vs_persistence_pct}% better than persistence</span> ·
              all approaches trained &amp; tested on the same rows of the new cleaned historian.
            </div>
          </div>
        </div>
      </Panel>

      {/* metrics table */}
      <Panel title="Side-by-side — accuracy on the identical held-out test set">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-[var(--muted)] text-left text-xs uppercase tracking-wide">
              <tr><th className="py-2">Approach</th><th className="text-right">MAE °C ↓</th>
                <th className="text-right">RMSE °C ↓</th><th className="text-right">R² ↑</th>
                <th className="text-right">Train time</th><th className="text-right">Step +2 tph coal</th></tr>
            </thead>
            <tbody className="mono">
              {present.map((k) => {
                const m = models[k]; const win = k === w.key;
                return (
                  <tr key={k} className={`border-t border-[var(--line)] ${win ? "bg-[rgba(56,189,248,0.06)]" : ""}`}>
                    <td className="py-2.5">
                      <span style={{ color: COLOR[k] }} className="font-semibold">{labels[k]}</span>
                      {win && <Badge tone="accent">winner</Badge>}
                      <div className="text-[10px] text-[var(--muted)] font-sans">{m.params}</div>
                    </td>
                    <td className="text-right font-bold">{m.metrics.mae.toFixed(2)}</td>
                    <td className="text-right">{m.metrics.rmse.toFixed(2)}</td>
                    <td className="text-right">{m.metrics.r2 != null ? m.metrics.r2.toFixed(3) : "—"}</td>
                    <td className="text-right">{m.train_time_s}s</td>
                    <td className="text-right" style={{ color: m.step_response_degC > 0.2 ? "var(--ok)" : "var(--warn)" }}>
                      {m.step_response_degC > 0 ? "+" : ""}{m.step_response_degC.toFixed(2)} °C
                    </td>
                  </tr>
                );
              })}
              <tr className="border-t border-[var(--line)] text-[var(--muted)]">
                <td className="py-2.5">Persistence baseline <span className="text-[10px] font-sans">(temp stays where it is)</span></td>
                <td className="text-right">{base.mae.toFixed(2)}</td>
                <td className="text-right">{base.rmse.toFixed(2)}</td>
                <td className="text-right">—</td><td className="text-right">—</td>
                <td className="text-right">0.00 °C</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div className="text-[11px] text-[var(--muted)] mt-2">
          Chronological 70/15/15 split — the test set is the most recent, fully unseen period. Step response = predicted Δtemp for a just-applied +2 tph calciner-coal move (physical expectation: positive).
        </div>
      </Panel>

      {/* overlay chart */}
      <Panel title="All three vs reality — held-out window"
        right={<div className="flex items-center gap-3 text-[11px] text-[var(--muted)]">
          {["actual", ...present].map((k) => (
            <span key={k} className="flex items-center gap-1">
              <span className="inline-block w-3 h-0.5" style={{ background: COLOR[k] }} />
              {k === "actual" ? "Actual" : labels[k]}
            </span>))}
        </div>}>
        <div style={{ height: 300 }}>
          <ResponsiveContainer>
            <ComposedChart data={chart} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
              <CartesianGrid stroke="#223252" strokeDasharray="3 3" />
              <XAxis dataKey="ts" tick={{ fill: "#8ea3c4", fontSize: 10 }} minTickGap={50} />
              <YAxis domain={["auto", "auto"]} tick={{ fill: "#8ea3c4", fontSize: 10 }} />
              <Tooltip contentStyle={{ background: "#16233d", border: "1px solid #223252", borderRadius: 8, color: "#e6edf7" }} />
              <Line type="monotone" dataKey="actual" stroke={COLOR.actual} dot={false} strokeWidth={1.8} name="Actual" isAnimationActive={false} />
              {present.map((k) => (
                <Line key={k} type="monotone" dataKey={k} stroke={COLOR[k]} dot={false}
                  strokeWidth={1.3} strokeDasharray="4 2" name={labels[k]} isAnimationActive={false} />
              ))}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </Panel>

      {/* accountability */}
      <div className="grid grid-cols-12 gap-4">
        {present.map((k) => {
          const m = models[k];
          return (
            <Panel key={k} className="col-span-12 lg:col-span-4"
              title={`${labels[k]} — accountability`}
              right={<span className="inline-block w-2.5 h-2.5 rounded-full" style={{ background: COLOR[k] }} />}>
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-[var(--muted)]">Attribution</span>
                  <Badge tone={k === "arx" ? "ok" : k === "gbt" ? "accent" : "warn"}>{m.attribution}</Badge>
                </div>
                <div>
                  <div className="text-[11px] uppercase tracking-wide text-[var(--muted)] mb-1.5">Top drivers</div>
                  {m.top_drivers.map((d, i) => (
                    <div key={i} className="flex items-center justify-between text-xs py-1 border-t border-[var(--line)]">
                      <span className="mono text-[var(--ink)]">{d.tag}</span>
                      <span className="mono text-[var(--muted)]">{d.weight}{d.gain != null ? ` · gain ${d.gain}` : ""}</span>
                    </div>
                  ))}
                </div>
                <div className="flex items-center justify-between panel-2 px-3 py-2 rounded">
                  <span className="text-xs text-[var(--muted)]">+2 tph coal ⇒ Δtemp</span>
                  <span className="mono font-bold" style={{ color: m.step_response_degC > 0.2 ? "var(--ok)" : "var(--warn)" }}>
                    {m.step_response_degC > 0 ? "+" : ""}{m.step_response_degC.toFixed(2)} °C
                    {m.step_response_degC > 0.2 ? " ✓" : " ⚠"}
                  </span>
                </div>
              </div>
            </Panel>
          );
        })}
      </div>
    </div>
  );
}
