"use client";
import React from "react";

export function Panel({ title, right, children, className = "" }: {
  title?: string; right?: React.ReactNode; children: React.ReactNode; className?: string;
}) {
  return (
    <div className={`panel p-4 ${className}`}>
      {(title || right) && (
        <div className="flex items-center justify-between mb-3">
          {title && <h3 className="text-sm font-semibold tracking-wide text-[var(--muted)] uppercase">{title}</h3>}
          {right}
        </div>
      )}
      {children}
    </div>
  );
}

export function Stat({ label, value, unit, tone = "ink", sub }: {
  label: string; value: React.ReactNode; unit?: string; tone?: string; sub?: string;
}) {
  const color = { ink: "var(--ink)", ok: "var(--ok)", warn: "var(--warn)", bad: "var(--bad)", accent: "var(--accent)" }[tone] || "var(--ink)";
  return (
    <div className="panel-2 p-3">
      <div className="text-[11px] text-[var(--muted)] uppercase tracking-wide truncate">{label}</div>
      <div className="mono text-2xl font-semibold mt-1" style={{ color }}>
        {value}{unit && <span className="text-sm text-[var(--muted)] ml-1">{unit}</span>}
      </div>
      {sub && <div className="text-[11px] text-[var(--muted)] mt-0.5">{sub}</div>}
    </div>
  );
}

export function Badge({ children, tone = "muted" }: { children: React.ReactNode; tone?: string }) {
  const map: Record<string, string> = {
    ok: "bg-[#0f2f1c] text-[var(--ok)] border-[#1f6b3f]",
    bad: "bg-[#33161a] text-[var(--bad)] border-[#7a2630]",
    warn: "bg-[#33280f] text-[var(--warn)] border-[#7a5a1f]",
    accent: "bg-[#0d2438] text-[var(--accent)] border-[#1d4e6b]",
    muted: "bg-[var(--panel-2)] text-[var(--muted)] border-[var(--line)]",
  };
  return <span className={`text-[11px] px-2 py-0.5 rounded-full border ${map[tone]}`}>{children}</span>;
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return <div className="text-[var(--muted)] text-sm p-6 animate-pulse">{label}</div>;
}
