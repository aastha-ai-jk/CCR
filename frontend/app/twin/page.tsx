"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import Mimic from "@/components/Mimic";
import SavingsPanel from "@/components/SavingsPanel";
import Advisor from "@/components/Advisor";
import { Panel, Badge, Loading } from "@/components/ui";

const fmt = (v: any, d = 1) =>
  v === null || v === undefined || isNaN(Number(v)) ? "—" : Number(v).toFixed(d);

const FEED_TAG = "353RL130_TPH";
const FEED_MIN = 461; // Combustion Control logic doc: applicability notice

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

  const band = ov.target_band;
  const feed = latest.values?.[FEED_TAG];
  const applicable = feed != null && feed > FEED_MIN;

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

      {/* ---- applicability gate: doc's own prerequisite for ANY automated advisory action ---- */}
      {!applicable && (
        <Panel className="border border-[var(--warn)]">
          <div className="text-sm text-[var(--warn)] font-semibold">
            Advisory not applicable — kiln feed {fmt(feed, 0)} t/h is at or below the {FEED_MIN} t/h threshold.
          </div>
          <div className="text-[11px] text-[var(--muted)] mt-1">
            Per the Combustion Control logic doc's Applicability Notice: automated control actions
            only apply when kiln feed ({FEED_TAG}) is above {FEED_MIN} t/h. The plant is in a
            transient/low-load state — the recommendation below should be treated as informational
            only, not actioned.
          </div>
        </Panel>
      )}

      {/* ---------------- mimic: full width so nothing is clipped ---------------- */}
      <Panel className="!p-2">
        <Mimic values={latest.values} units={latest.units} band={band} mode="live" />
      </Panel>

      {/* ---------------- fuel-saving advisor: hero, recommendation, reasoning, accept/decline, dashboard ---------------- */}
      <Panel>
        <Advisor latest={latest} adv={adv} />
      </Panel>

      {/* ---------------- per-parameter energy & money savings ---------------- */}
      <SavingsPanel breakdown={adv?.breakdown} mode={adv?.mode} />

      <div className="text-[11px] text-[var(--muted)]">
        Every readout is your own historical/live plant data mapped onto the FLSmidth Kiln-3 flow sheet.
        The calciner outlet temperature is the controlled variable; the analyser boxes carry the combustion-safety interlocks.
      </div>
    </div>
  );
}
