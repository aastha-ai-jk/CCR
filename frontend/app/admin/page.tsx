"use client";
import { Panel } from "@/components/ui";

// Administrative dashboard — placeholder; contents to be defined.
export default function Admin() {
  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-bold">Admin Dashboard</h1>
        <div className="text-sm text-[var(--muted)]">Kiln-3 digital twin · administration</div>
      </header>
      <Panel>
        <div className="text-sm text-[var(--muted)]">Nothing here yet.</div>
      </Panel>
    </div>
  );
}
