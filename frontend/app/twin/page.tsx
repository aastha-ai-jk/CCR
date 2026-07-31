"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import Mimic from "@/components/Mimic";
import SavingsPanel from "@/components/SavingsPanel";
import { Panel, Badge, Loading } from "@/components/ui";

const fmt = (v: any, d = 1) =>
  v === null || v === undefined || isNaN(Number(v)) ? "—" : Number(v).toFixed(d);

export default function DigitalTwin() {
  const [latest, setLatest] = useState<any>(null);
  const [ov, setOv] = useState<any>(null);
  const [adv, setAdv] = useState<any>(null);

  const load = () => {
    api.latest().then(setLatest).catch(console.error);
    api.overview().then(setOv).catch(console.error);
    api.advisory().then(setAdv).catch(console.error);
  };
  useEffect(() => { load(); const id = setInterval(load, 2500); return () => clearInterval(id); }, []);

  if (!latest || !ov) return <Loading label="Injecting live plant data into the twin…" />;
  const allOk = ov.interlocks.every((i: any) => i.pass !== false);
  const src = latest.source;
  const feedBadge = src === "opc"
    ? { tone: "ok", text: `● LIVE · OPC · ${latest.age_sec ?? "?"}s ago` }
    : src === "replay"
    ? { tone: "warn", text: "REPLAY (recent history)" }
    : { tone: "muted", text: "historical snapshot" };

  // ---- prediction / tracking ----
  const band = ov.target_band;
  const pred = adv?.baseline;
  const predTemp = pred?.predicted_temp;
  const actual = adv?.live_actual_temp ?? latest.values?.["423CI400_TT_4"];
  const inBand = predTemp != null && predTemp >= band.low && predTemp <= band.high;
  const err = predTemp != null && actual != null ? predTemp - actual : null;
  const sv = adv?.saving;

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
          <Badge tone="muted">Target {band.low}–{band.high} °C</Badge>
          <Badge tone={allOk ? "ok" : "bad"}>{allOk ? "Interlocks OK" : "Interlock breach"}</Badge>
        </div>
      </header>

      {/* ---------------- mimic: full width so nothing is clipped ---------------- */}
      <Panel className="!p-2">
        <Mimic values={latest.values} units={latest.units} band={band} mode="live" />
      </Panel>

      {/* ---------------- prediction / tracking / advisory, below the mimic ------- */}
      <div className="grid grid-cols-12 gap-4 items-start">
          <Panel className="col-span-12 md:col-span-6 lg:col-span-3"
                 title={`Predicted calciner temperature${pred?.horizon_min ? ` · +${pred.horizon_min} min` : ""}`}>
            {!adv ? <Loading /> : (
              <>
                <div className="mono text-5xl font-bold glow text-center"
                     style={{ color: inBand ? "var(--ok)" : "var(--warn)" }}>
                  {fmt(predTemp, 0)}<span className="text-lg text-[var(--muted)]">°C</span>
                </div>
                <div className="text-center text-xs text-[var(--muted)] mt-1">
                  P10–P90: {fmt(pred?.p10, 0)}–{fmt(pred?.p90, 0)} °C
                </div>
                <div className="flex items-center justify-center gap-2 mt-3 flex-wrap">
                  <Badge tone="accent">{ov.operating_state}</Badge>
                  <Badge tone="muted">band {band.low}–{band.high}</Badge>
                  <Badge tone={inBand ? "ok" : "warn"}>{inBand ? "IN BAND" : "OUT OF BAND"}</Badge>
                </div>
              </>
            )}
          </Panel>

          <Panel className="col-span-12 md:col-span-6 lg:col-span-4" title="Twin vs live plant">
            <div className="grid grid-cols-2 gap-2 text-center">
              <div className="panel-2 p-2">
                <div className="text-[10px] text-[var(--muted)] uppercase">Live actual</div>
                <div className="mono text-xl">{fmt(actual, 0)}<span className="text-xs text-[var(--muted)]"> °C</span></div>
              </div>
              <div className="panel-2 p-2">
                <div className="text-[10px] text-[var(--muted)] uppercase">Twin predicted</div>
                <div className="mono text-xl text-[var(--accent)]">{fmt(predTemp, 0)}<span className="text-xs text-[var(--muted)]"> °C</span></div>
              </div>
            </div>
            {err != null && (
              <div className="text-center text-[11px] mt-2 text-[var(--muted)]">
                tracking error{" "}
                <span className={`mono ${Math.abs(err) <= 8 ? "text-[var(--ok)]" : "text-[var(--warn)]"}`}>
                  {err > 0 ? "+" : ""}{err.toFixed(1)} °C
                </span>{" "}
                (twin vs plant)
              </div>
            )}
          </Panel>

          {/* ---------------- savings advisory ---------------- */}
          <Panel className="col-span-12 lg:col-span-5" title="Fuel-saving advisory"
                 right={adv?.mode ? <Badge tone={adv.mode === "hold" ? "ok" : adv.mode === "approach" ? "warn" : "bad"}>
                   {adv.mode === "hold" ? "HOLD BAND" : adv.mode === "approach" ? "APPROACH BAND" : "BLOCKED"}
                 </Badge> : undefined}>
            {!adv ? <Loading /> : !adv.feasible ? (
              <div className="text-xs text-[var(--warn)]">{adv.reason}</div>
            ) : (
              <>
                <div className="text-center">
                  <div className="text-[10px] text-[var(--muted)] uppercase tracking-wide">
                    Estimated coal saving
                  </div>
                  <div className="mono text-4xl font-bold glow"
                       style={{ color: sv.coal_tph > 0 ? "var(--ok)" : "var(--warn)" }}>
                    {sv.coal_tph > 0 ? "−" : "+"}{Math.abs(sv.coal_tph).toFixed(2)}
                    <span className="text-sm text-[var(--muted)] ml-1">t/h</span>
                  </div>
                  <div className="text-[11px] text-[var(--muted)]">
                    {sv.coal_pct != null && <>{Math.abs(sv.coal_pct).toFixed(1)}% of current firing</>}
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-2 mt-3">
                  <div className="panel-2 p-2 text-center">
                    <div className="text-[10px] text-[var(--muted)] uppercase">per day</div>
                    <div className="mono text-base">{Math.abs(sv.coal_tpd).toFixed(1)} <span className="text-[10px] text-[var(--muted)]">t</span></div>
                  </div>
                  <div className="panel-2 p-2 text-center">
                    <div className="text-[10px] text-[var(--muted)] uppercase">per year*</div>
                    <div className="mono text-base">{Math.abs(sv.coal_tpy).toLocaleString()} <span className="text-[10px] text-[var(--muted)]">t</span></div>
                  </div>
                </div>

                {sv.cost_per_year != null && (
                  <div className="panel-2 p-2 mt-2 text-center">
                    <div className="text-[10px] text-[var(--muted)] uppercase">est. value / year</div>
                    <div className="mono text-base text-[var(--ok)]">
                      {sv.currency} {Math.abs(sv.cost_per_year).toLocaleString()}
                    </div>
                  </div>
                )}

                <div className="mt-3 space-y-1">
                  <div className="text-[10px] text-[var(--muted)] uppercase tracking-wide">Recommended move</div>
                  {adv.moves.map((mv: any) => (
                    <div key={mv.tag} className="flex items-center justify-between text-[11px] panel-2 px-2 py-1">
                      <span className="text-[var(--ink)] truncate pr-2">{mv.name}</span>
                      <span className="mono shrink-0">
                        {fmt(mv.from, 1)} <span className="text-[var(--muted)]">→</span>{" "}
                        <span className={mv.delta < 0 ? "text-[var(--ok)]" : "text-[var(--accent)]"}>{fmt(mv.to, 1)}</span>
                      </span>
                    </div>
                  ))}
                </div>

                <div className="text-[10px] text-[var(--muted)] mt-2 leading-relaxed">
                  Predicted temp after move: <span className="mono">{fmt(adv.predicted_temp, 0)} °C</span>
                  {adv.mode === "approach" && adv.steps_to_band
                    ? ` · ~${adv.steps_to_band} guarded steps to reach band`
                    : ""}
                  {" "}· all interlocks respected · within ±{adv.assumptions?.guardrail_pct}% ramp limit.
                </div>
                <div className="text-[10px] text-[var(--muted)] mt-1 leading-relaxed">
                  *{adv.assumptions?.operating_days_per_year} operating days per year.
                </div>
              </>
            )}
          </Panel>
      </div>

      {/* ---------------- per-parameter energy & money savings ---------------- */}
      <SavingsPanel breakdown={adv?.breakdown} mode={adv?.mode} />

      <div className="text-[11px] text-[var(--muted)]">
        Every readout is your own historical/live plant data mapped onto the FLSmidth Kiln-3 flow sheet.
        The calciner outlet temperature is the controlled variable; the analyser boxes carry the combustion-safety interlocks.
      </div>
    </div>
  );
}
