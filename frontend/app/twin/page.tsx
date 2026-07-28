"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import Mimic from "@/components/Mimic";
import { Panel, Badge, Loading } from "@/components/ui";

export default function DigitalTwin() {
  const [latest, setLatest] = useState<any>(null);
  const [ov, setOv] = useState<any>(null);

  const load = () => {
    api.latest().then(setLatest).catch(console.error);
    api.overview().then(setOv).catch(console.error);
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
    </div>
  );
}
