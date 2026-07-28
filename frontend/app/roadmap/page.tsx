"use client";
import { useEffect, useState } from "react";
import { api, Milestone } from "@/lib/api";
import { Panel, Badge, Loading } from "@/components/ui";
import { CheckCircle2, Circle, Loader } from "lucide-react";

const STATUS: Record<string, { tone: string; label: string; icon: any }> = {
  done: { tone: "ok", label: "Complete", icon: CheckCircle2 },
  in_progress: { tone: "accent", label: "In progress", icon: Loader },
  planned: { tone: "muted", label: "Planned", icon: Circle },
};

export default function Roadmap() {
  const [ms, setMs] = useState<Milestone[] | null>(null);
  useEffect(() => { api.roadmap().then(setMs).catch(console.error); }, []);
  if (!ms) return <Loading />;

  const done = ms.filter((m) => m.status === "done").length;
  const prog = Math.round((done + ms.filter((m) => m.status === "in_progress").length * 0.5) / ms.length * 100);

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-bold">Project Roadmap</h1>
        <div className="text-sm text-[var(--muted)]">Calcination Combustion Control — from raw data to closed-loop advisory</div>
      </header>

      <Panel>
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm text-[var(--muted)]">Overall progress</span>
          <span className="mono text-[var(--accent)]">{prog}%</span>
        </div>
        <div className="h-3 rounded-full bg-[var(--panel-2)] overflow-hidden">
          <div className="h-full bg-[var(--accent)]" style={{ width: `${prog}%` }} />
        </div>
      </Panel>

      <div className="relative pl-6">
        <div className="absolute left-2 top-2 bottom-2 w-px bg-[var(--line)]" />
        <div className="space-y-3">
          {ms.map((m) => {
            const st = STATUS[m.status] || STATUS.planned;
            const Icon = st.icon;
            return (
              <div key={m.phase} className="relative">
                <div className="absolute -left-[18px] top-4 bg-[var(--bg)]">
                  <Icon size={16} className={m.status === "in_progress" ? "animate-spin-slow" : ""}
                    color={st.tone === "ok" ? "#22c55e" : st.tone === "accent" ? "#38bdf8" : "#8ea3c4"} />
                </div>
                <div className="panel p-4 flex items-start justify-between gap-4">
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="mono text-[var(--accent)] font-bold">{m.phase}</span>
                      <span className="font-semibold">{m.title}</span>
                    </div>
                    <div className="text-sm text-[var(--muted)] mt-1">{m.detail}</div>
                  </div>
                  <Badge tone={st.tone}>{st.label}</Badge>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
