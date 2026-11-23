"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export default function TopBar() {
  const [clock, setClock] = useState("");
  const [online, setOnline] = useState<boolean | null>(null);

  useEffect(() => {
    const tick = () => setClock(new Date().toLocaleTimeString("en-GB"));
    tick();
    const id = setInterval(tick, 1000);
    api.health().then(() => setOnline(true)).catch(() => setOnline(false));
    return () => clearInterval(id);
  }, []);

  const dot = online === null ? "#8ea3c4" : online ? "#4ade80" : "#ef4444";
  return (
    <div className="mb-5">
      <div className="flex items-center justify-between panel px-4 py-2.5">
        <div className="flex items-center gap-3">
          <span className="text-[var(--accent)] font-bold tracking-widest text-sm">JK CEMENT</span>
          <span className="text-[var(--muted)] text-xs">|</span>
          <span className="text-xs text-[var(--muted)] tracking-wide">KILN-3 · PRECALCINER · CALCINATION COMBUSTION CONTROL</span>
        </div>
        <div className="flex items-center gap-4 text-xs">
          <span className="mono text-[var(--muted)]">{clock}</span>
          <span className="flex items-center gap-2">
            <span className="led live-dot" style={{ color: dot, background: dot }} />
            <span className="text-[var(--muted)]">{online === false ? "BACKEND OFFLINE" : "TWIN ONLINE"}</span>
          </span>
        </div>
      </div>
      <div className="hazard rounded-b" />
    </div>
  );
}
