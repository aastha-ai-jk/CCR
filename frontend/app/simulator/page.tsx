"use client";
import { useEffect, useRef, useState } from "react";
import { api, OptimizeResult } from "@/lib/api";
import Mimic from "@/components/Mimic";
import { Panel, Stat, Badge, Loading } from "@/components/ui";
import { ComposedChart, Line, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, ReferenceArea } from "recharts";

const CONDITIONS = [
  { key: "free_lime", label: "Free lime (clinker)", min: 0.5, max: 2.5, step: 0.1, def: 1.2, live: false },
  { key: "liter_weight", label: "Liter weight", min: 1080, max: 1230, step: 5, def: 1170, live: false },
  { key: "liquid", label: "Liquid in clinker %", min: 27, max: 31, step: 0.1, def: 29.2, live: false },
  { key: "doc", label: "DOC hotmeal %", min: 90, max: 97, step: 0.5, def: 94, live: false },
  { key: "433MD140_TQ", label: "Kiln torque (Nm)", min: 340, max: 440, step: 5, def: 390, live: true },
  { key: "423AM822_NO", label: "Kiln inlet NOx (ppm)", min: 400, max: 800, step: 10, def: 600, live: true },
  { key: "443KH050_TT1", label: "Secondary air temp (°C)", min: 950, max: 1120, step: 5, def: 1040, live: true },
];
const TNAME: Record<string, string> = {
  "423CI400_TT_4": "Calciner Temp", "423AM823_CO_PPM": "Calciner CO", "423AM823_O2": "Calciner O₂",
  "423AM822_CO_PPM": "Kiln-inlet CO", "423AM822_O2": "Kiln-inlet O₂",
};
const TUNIT: Record<string, string> = {
  "423CI400_TT_4": "°C", "423AM823_CO_PPM": "ppm", "423AM823_O2": "%", "423AM822_CO_PPM": "ppm", "423AM822_O2": "%",
};
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

export default function Simulator() {
  const [defs, setDefs] = useState<any>(null);
  const [levers, setLevers] = useState<Record<string, number>>({});
  const [conds, setConds] = useState<Record<string, number>>(
    Object.fromEntries(CONDITIONS.map((c) => [c.key, c.def])));
  const [res, setRes] = useState<any>(null);
  const [baseline, setBaseline] = useState<any>(null);
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});
  const [frozen, setFrozen] = useState(false);
  const [opt, setOpt] = useState<OptimizeResult | null>(null);
  const [optLoading, setOptLoading] = useState(false);
  const [optErr, setOptErr] = useState<string | null>(null);

  const overridesRef = useRef(overrides); overridesRef.current = overrides;
  const frozenRef = useRef(frozen); frozenRef.current = frozen;
  const timer = useRef<any>(null);

  useEffect(() => {
    api.simDefaults().then((d) => {
      setDefs(d);
      setLevers(Object.fromEntries(Object.entries(d.features).map(([k, v]: any) => [k, v.default])));
    });
  }, []);

  // ---- LIVE-TRACKING: poll the plant; non-overridden sliders follow it ----
  useEffect(() => {
    if (!defs) return;
    const mvKeys = Object.keys(defs.features);
    const tick = async () => {
      if (frozenRef.current) return;
      try {
        const l = await api.latest();
        setBaseline(l);
        setLevers((prev) => {
          const n = { ...prev };
          for (const k of mvKeys) {
            const f = defs.features[k];
            if (!overridesRef.current[k] && l.values?.[k] != null)
              n[k] = clamp(l.values[k], f.min, f.max);
          }
          return n;
        });
        setConds((prev) => {
          const n = { ...prev };
          for (const c of CONDITIONS)
            if (c.live && !overridesRef.current[c.key] && l.values?.[c.key] != null)
              n[c.key] = clamp(l.values[c.key], c.min, c.max);
          return n;
        });
      } catch { /* keep last values */ }
    };
    tick();
    const id = setInterval(tick, 2500);
    return () => clearInterval(id);
  }, [defs]);

  // ---- prediction from current lever/condition state ----
  useEffect(() => {
    if (!Object.keys(levers).length) return;
    clearTimeout(timer.current);
    const cond = { ...conds, current_temp: baseline?.values?.["423CI400_TT_4"] };
    timer.current = setTimeout(() => api.simulate(levers, cond).then(setRes).catch(console.error), 200);
  }, [levers, conds, baseline]);

  if (!defs) return <Loading label="Loading twin surrogate…" />;
  const temp = res?.prediction?.["423CI400_TT_4"];
  const inBand = res?.temp_in_band;
  const actualTemp = baseline?.values?.["423CI400_TT_4"];
  const err = temp?.mean != null && actualTemp != null ? temp.mean - actualTemp : null;
  const anyOverride = Object.values(overrides).some(Boolean);
  const src = baseline?.source;
  const feed = src === "opc" ? { tone: "ok", text: `● LIVE · ${baseline.age_sec ?? "?"}s` }
    : src === "replay" ? { tone: "warn", text: "REPLAY" } : { tone: "muted", text: "historical" };

  const setLever = (k: string, v: number) => {
    setOverrides((o) => ({ ...o, [k]: true }));
    setLevers((s) => ({ ...s, [k]: v }));
  };
  const setCond = (k: string, v: number) => {
    setOverrides((o) => ({ ...o, [k]: true }));
    setConds((s) => ({ ...s, [k]: v }));
  };

  const runOptimize = async () => {
    setOptLoading(true);
    setOptErr(null);
    try {
      const cond = { ...conds, current_temp: baseline?.values?.["423CI400_TT_4"] };
      const r = await api.optimize(cond);
      setOpt(r);
    } catch (e) {
      setOptErr(e instanceof Error ? e.message : "Optimize failed");
    } finally {
      setOptLoading(false);
    }
  };

  // Push recommended setpoints onto the sliders as overrides → the sim effect re-runs.
  const applyRecommended = () => {
    if (!opt) return;
    const rec = opt.recommended;
    setOverrides((o) => {
      const n = { ...o };
      for (const k of Object.keys(rec)) n[k] = true;
      return n;
    });
    setLevers((s) => {
      const n = { ...s };
      for (const [k, v] of Object.entries(rec)) {
        const f = defs.features[k];
        n[k] = f ? clamp(v, f.min, f.max) : v;
      }
      return n;
    });
  };

  const mimicValues: Record<string, number> = { ...(baseline?.values || {}), ...levers };
  if (res) for (const [k, v] of Object.entries(res.prediction)) mimicValues[k] = (v as any).mean;

  return (
    <div className="space-y-5">
      <header className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <h1 className="text-xl font-bold flex items-center gap-2">
            <span className="live-dot text-[var(--ok)]">●</span> Twin Simulator
            <span className="text-[var(--muted)] text-sm font-normal">· live-tracking</span>
          </h1>
          <div className="text-sm text-[var(--muted)]">
            Sliders follow the live plant. Grab any lever to test a change — the rest keep tracking.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Badge tone={feed.tone}>{feed.text}</Badge>
          {anyOverride && <Badge tone="warn">override active</Badge>}
          <button onClick={() => setOverrides({})}
            className="text-xs px-2.5 py-1 rounded-full border border-[var(--line)] text-[var(--muted)] hover:text-[var(--ink)]">
            Resync to live
          </button>
          <button onClick={() => setFrozen((f) => !f)}
            className={`text-xs px-2.5 py-1 rounded-full border ${frozen
              ? "bg-[var(--panel-2)] text-[var(--warn)] border-[var(--warn)]"
              : "border-[var(--line)] text-[var(--muted)] hover:text-[var(--ink)]"}`}>
            {frozen ? "❚❚ Frozen" : "► Freeze"}
          </button>
          <button onClick={runOptimize} disabled={optLoading}
            className="text-xs px-3 py-1 rounded-full border border-[var(--accent)] text-[var(--accent)] hover:bg-[var(--panel-2)] disabled:opacity-50">
            {optLoading ? "Optimizing…" : "⚡ Optimize setpoints"}
          </button>
        </div>
      </header>

      <Panel title="Process mimic — live plant, with your lever changes + predicted response" className="!p-2">
        <Mimic values={mimicValues} units={baseline?.units || {}} band={res?.target_band} mode="sim" />
      </Panel>

      <div className="grid grid-cols-12 gap-4">
        {/* levers */}
        <Panel title="Control levers — 8 manipulated variables" className="col-span-12 lg:col-span-5"
          right={res?.method === "gbt-twin"
            ? <Badge tone="accent">SHAP (°C) · gbt-twin</Badge>
            : <Badge tone="muted">cardinal Δ (°C)</Badge>}>
          <div className="text-[11px] text-[var(--muted)] mb-2">
            {res?.method === "gbt-twin"
              ? "Each °C figure is the lever's SHAP contribution to the predicted PC temperature (GBT twin)."
              : "Each lever moves PC temperature by its documented cardinal relationship (direction & strength)."}
          </div>
          <div className="space-y-3">
            {Object.entries(defs.features).map(([k, f]: any) => {
              const rel = res?.relationships?.[k];
              const c = rel?.contribution ?? 0;
              const relTone = rel?.sign === "+" ? "text-[var(--ok)]" : "text-[var(--warn)]";
              const live = baseline?.values?.[k];
              const isOverride = !!overrides[k];
              return (
                <div key={k}>
                  <div className="flex justify-between text-xs mb-0.5">
                    <span className="text-[var(--ink)] flex items-center gap-1.5">
                      {f.name}
                      <span className={`text-[9px] px-1 rounded ${isOverride ? "text-[var(--warn)] bg-[#33280f]" : "text-[var(--ok)] bg-[#0f2f1c]"}`}>
                        {isOverride ? "override" : "live"}
                      </span>
                    </span>
                    <span className="mono text-[var(--accent)]">{levers[k]?.toFixed(1)} <span className="text-[var(--muted)]">{f.uom}</span></span>
                  </div>
                  <div className="flex justify-between text-[10px] mb-1">
                    <span className={relTone}>{rel ? `${rel.label} (${rel.sign})` : ""}</span>
                    <span className="mono text-[var(--muted)]">
                      {isOverride && live != null ? `live ${Number(live).toFixed(1)} · ` : ""}{c > 0 ? "+" : ""}{c} °C
                    </span>
                  </div>
                  <input type="range" min={f.min} max={f.max} step={(f.max - f.min) / 200} value={levers[k] ?? f.default}
                    onChange={(e) => setLever(k, +e.target.value)}
                    className={`w-full ${isOverride ? "accent-[var(--warn)]" : "accent-[var(--accent)]"}`} />
                </div>
              );
            })}
          </div>
        </Panel>

        {/* conditions */}
        <Panel title="Quality / state conditions" className="col-span-12 lg:col-span-3">
          <div className="space-y-3">
            {CONDITIONS.map((c) => {
              const isOverride = !!overrides[c.key];
              return (
                <div key={c.key}>
                  <div className="flex justify-between text-xs mb-1">
                    <span className="text-[var(--ink)] flex items-center gap-1.5">
                      {c.label}
                      {c.live && <span className={`text-[9px] px-1 rounded ${isOverride ? "text-[var(--warn)] bg-[#33280f]" : "text-[var(--ok)] bg-[#0f2f1c]"}`}>{isOverride ? "override" : "live"}</span>}
                    </span>
                    <span className="mono text-[var(--muted)]">{conds[c.key]}</span>
                  </div>
                  <input type="range" min={c.min} max={c.max} step={c.step} value={conds[c.key]}
                    onChange={(e) => setCond(c.key, +e.target.value)}
                    className={`w-full ${isOverride ? "accent-[var(--warn)]" : "accent-[var(--muted)]"}`} />
                </div>
              );
            })}
            <div className="text-[10px] text-[var(--muted)] pt-1">
              Torque / NOx / secondary-air track live; lab values (free lime, liter wt, liquid, DOC) are manual until the lab feed is wired in.
            </div>
          </div>
        </Panel>

        {/* result */}
        <div className="col-span-12 lg:col-span-4 space-y-4">
          <Panel title="Predicted calciner temperature">
            {!res ? <Loading /> : (
              <>
                <div className="mono text-5xl font-bold glow text-center" style={{ color: inBand ? "var(--ok)" : "var(--warn)" }}>
                  {temp?.mean?.toFixed(0)}<span className="text-lg text-[var(--muted)]">°C</span>
                </div>
                <div className="text-center text-xs text-[var(--muted)] mt-1">
                  P10–P90: {temp?.p10?.toFixed(0)}–{temp?.p90?.toFixed(0)} °C
                </div>
                <div className="flex items-center justify-center gap-2 mt-3">
                  <Badge tone="accent">{res.operating_state}</Badge>
                  <Badge tone="muted">band {res.target_band.low}–{res.target_band.high}</Badge>
                  <Badge tone={inBand ? "ok" : "warn"}>{inBand ? "IN BAND" : `${res.temp_gap > 0 ? "+" : ""}${res.temp_gap}°C`}</Badge>
                </div>
              </>
            )}
          </Panel>

          {/* v3: predicted temperature trajectory (t+5/10/15/30) */}
          {res?.trajectory && res.trajectory.length > 1 && (
            <Panel title="Predicted trajectory — where these setpoints take the calciner">
              <div style={{ height: 180 }}>
                <ResponsiveContainer>
                  <ComposedChart
                    data={[{ min: 0, mean: res.t0, band: [res.t0, res.t0] },
                           ...res.trajectory.map((p: any) => ({ min: p.min, mean: p.mean, band: [p.p10, p.p90] }))]}
                    margin={{ top: 5, right: 10, left: -14, bottom: 0 }}>
                    <CartesianGrid stroke="#223252" strokeDasharray="3 3" />
                    <XAxis dataKey="min" type="number" domain={[0, 30]} ticks={[0, 5, 10, 15, 30]}
                      tick={{ fill: "#8ea3c4", fontSize: 10 }} unit="m" />
                    <YAxis domain={["auto", "auto"]} tick={{ fill: "#8ea3c4", fontSize: 10 }} />
                    <Tooltip contentStyle={{ background: "#16233d", border: "1px solid #223252", borderRadius: 8, color: "#e6edf7" }} />
                    <ReferenceArea y1={res.target_band.low} y2={res.target_band.high} fill="#22c55e" fillOpacity={0.1} />
                    <Area type="monotone" dataKey="band" stroke="none" fill="#38bdf8" fillOpacity={0.15} isAnimationActive={false} />
                    <Line type="monotone" dataKey="mean" stroke="#38bdf8" strokeWidth={1.8} dot={{ r: 2.5 }} isAnimationActive={false} />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
              <div className="text-[11px] text-[var(--muted)] mt-1">
                Mean path with P10–P90 uncertainty; green = the state target band. Horizons: now → +5 → +10 → +15 → +30 min at held setpoints.
              </div>
            </Panel>
          )}

          {/* predicted-vs-live accuracy meter */}
          <Panel title="Twin vs live plant">
            <div className="grid grid-cols-2 gap-2 text-center">
              <div className="panel-2 p-2">
                <div className="text-[10px] text-[var(--muted)] uppercase">Live actual</div>
                <div className="mono text-xl">{actualTemp != null ? Number(actualTemp).toFixed(0) : "—"}<span className="text-xs text-[var(--muted)]"> °C</span></div>
              </div>
              <div className="panel-2 p-2">
                <div className="text-[10px] text-[var(--muted)] uppercase">Twin predicted</div>
                <div className="mono text-xl text-[var(--accent)]">{temp?.mean != null ? temp.mean.toFixed(0) : "—"}<span className="text-xs text-[var(--muted)]"> °C</span></div>
              </div>
            </div>
            <div className="text-center text-[11px] mt-2">
              {anyOverride
                ? <span className="text-[var(--warn)]">what-if active — prediction reflects your override, not the live setpoint</span>
                : err != null
                ? <span className="text-[var(--muted)]">tracking error <span className={`mono ${Math.abs(err) <= 8 ? "text-[var(--ok)]" : "text-[var(--warn)]"}`}>{err > 0 ? "+" : ""}{err.toFixed(1)} °C</span> (twin vs plant)</span>
                : null}
            </div>
          </Panel>

          <Panel title="Combustion-safety prediction">
            {res && (
              <div className="space-y-1.5">
                {res.interlocks.map((i: any) => (
                  <div key={i.tag} className="flex items-center justify-between text-xs panel-2 px-2 py-1.5 gap-2">
                    <span className="text-[var(--ink)] leading-tight">{i.name}</span>
                    <span className="flex items-center gap-2 shrink-0">
                      <span className="mono">{i.predicted?.toFixed(1) ?? "—"}</span>
                      <span className="text-[var(--muted)]">({i.rule})</span>
                      <Badge tone={i.pass === false ? "bad" : i.pass ? "ok" : "muted"}>{i.pass === false ? "✕" : i.pass ? "✓" : "?"}</Badge>
                    </span>
                  </div>
                ))}
              </div>
            )}
          </Panel>
        </div>
      </div>

      {(opt || optLoading || optErr) && (
        <Panel title="Setpoint optimizer — advisory (coal ↓ · AFR ↑, safety-constrained)"
          right={opt && <Badge tone="accent">{opt.n_trials} trials</Badge>}>
          {optErr ? (
            <div className="text-sm text-[var(--bad)]">Optimizer error: {optErr}</div>
          ) : optLoading ? (
            <Loading label="Searching setpoints (Optuna)…" />
          ) : opt ? (
            <div className="grid grid-cols-12 gap-4">
              {/* recommended levers */}
              <div className="col-span-12 lg:col-span-5 space-y-2">
                <div className="text-[11px] text-[var(--muted)] uppercase tracking-wide">Recommended setpoints vs current</div>
                {Object.entries(opt.recommended).map(([k, v]) => {
                  const f = defs.features[k];
                  const base = opt.baseline.levers?.[k];
                  const d = base != null ? v - base : null;
                  return (
                    <div key={k} className="flex items-center justify-between panel-2 px-3 py-2 text-xs gap-2">
                      <span className="text-[var(--ink)]">{f?.name || k}</span>
                      <span className="flex items-center gap-2 shrink-0">
                        {base != null && <span className="mono text-[var(--muted)]">{base.toFixed(1)} →</span>}
                        <span className="mono text-[var(--accent)]">{v.toFixed(1)} <span className="text-[var(--muted)]">{f?.uom}</span></span>
                        {d != null && Math.abs(d) > 0.05 && (
                          <span className={`mono text-[10px] ${d < 0 ? "text-[var(--ok)]" : "text-[var(--warn)]"}`}>
                            {d > 0 ? "+" : ""}{d.toFixed(1)}
                          </span>
                        )}
                      </span>
                    </div>
                  );
                })}
                <button onClick={applyRecommended}
                  className="w-full mt-1 text-xs px-3 py-2 rounded-lg border border-[var(--accent)] text-[var(--accent)] hover:bg-[var(--panel-2)]">
                  Apply to sliders &amp; re-simulate
                </button>
              </div>

              {/* predicted temp + objective */}
              <div className="col-span-12 lg:col-span-4 space-y-3">
                <div className="text-center panel-2 p-3">
                  <div className="text-[10px] text-[var(--muted)] uppercase tracking-wide">Predicted calciner temp</div>
                  <div className="mono text-3xl font-bold glow" style={{ color: opt.in_band ? "var(--ok)" : "var(--warn)" }}>
                    {opt.predicted.temp.mean.toFixed(0)}<span className="text-sm text-[var(--muted)]">°C</span>
                  </div>
                  <div className="text-[11px] text-[var(--muted)]">P10–P90 {opt.predicted.temp.p10.toFixed(0)}–{opt.predicted.temp.p90.toFixed(0)} °C</div>
                  <div className="flex items-center justify-center gap-2 mt-2">
                    <Badge tone="muted">band {opt.target_band.low}–{opt.target_band.high}</Badge>
                    <Badge tone={opt.in_band ? "ok" : "warn"}>{opt.in_band ? "IN BAND" : "OUT OF BAND"}</Badge>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-2 text-center">
                  <div className="panel-2 p-2">
                    <div className="text-[10px] text-[var(--muted)] uppercase tracking-wide">Coal ↓</div>
                    <div className="mono text-lg text-[var(--ok)]">{opt.objective.coal.toFixed(1)}</div>
                    <div className="text-[10px] mono text-[var(--muted)]">Δ {opt.delta.coal > 0 ? "+" : ""}{opt.delta.coal.toFixed(1)} t/h</div>
                  </div>
                  <div className="panel-2 p-2">
                    <div className="text-[10px] text-[var(--muted)] uppercase tracking-wide">AFR ↑</div>
                    <div className="mono text-lg text-[var(--accent)]">{opt.objective.afr_total.toFixed(1)}</div>
                    <div className="text-[10px] mono text-[var(--muted)]">Δ {opt.delta.afr > 0 ? "+" : ""}{opt.delta.afr.toFixed(1)}</div>
                  </div>
                </div>
              </div>

              {/* interlock / safety status */}
              <div className="col-span-12 lg:col-span-3 space-y-2">
                <div className="text-[11px] text-[var(--muted)] uppercase tracking-wide">Safety interlocks (predicted)</div>
                {opt.interlocks.map((i) => (
                  <div key={i.tag} className="flex items-center justify-between text-[11px] panel-2 px-2 py-1.5 gap-2">
                    <span className="text-[var(--ink)] leading-tight">{i.name}</span>
                    <span className="flex items-center gap-1.5 shrink-0">
                      <span className="mono">{i.predicted != null ? i.predicted.toFixed(1) : "—"}</span>
                      <span className="text-[var(--muted)]">({i.rule})</span>
                      <Badge tone={i.pass === false ? "bad" : i.pass ? "ok" : "muted"}>{i.pass === false ? "✕" : i.pass ? "✓" : "?"}</Badge>
                    </span>
                  </div>
                ))}
              </div>

              {opt.explanation?.length > 0 && (
                <div className="col-span-12">
                  <ul className="text-[11px] text-[var(--muted)] space-y-1 list-disc list-inside">
                    {opt.explanation.map((e, idx) => <li key={idx}>{e}</li>)}
                  </ul>
                </div>
              )}
              <div className="col-span-12 text-[10px] text-[var(--warn)]">
                ⚠ Advisory only — recommended setpoints are never written to any control system.
              </div>
            </div>
          ) : null}
        </Panel>
      )}

      {res && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          {Object.entries(res.prediction).map(([k, v]: any) => (
            <Stat key={k} label={TNAME[k] || k} unit={TUNIT[k]} value={v.mean?.toFixed(1)}
              tone={k === "423CI400_TT_4" ? "accent" : "ink"} sub={`P10–P90 ${v.p10?.toFixed(0)}–${v.p90?.toFixed(0)}`} />
          ))}
        </div>
      )}
    </div>
  );
}
