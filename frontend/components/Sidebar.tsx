"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity, Map, Database, GitCompare, SlidersHorizontal, Factory, Lightbulb } from "lucide-react";

const NAV = [
  { href: "/", label: "Live Overview", icon: Activity },
  { href: "/advisory", label: "Advisory", icon: Lightbulb },
  { href: "/twin", label: "Digital Twin", icon: Factory },
  { href: "/roadmap", label: "Roadmap", icon: Map },
  { href: "/explorer", label: "Data Explorer", icon: Database },
  { href: "/correlation", label: "Correlation Studio", icon: GitCompare },
  { href: "/simulator", label: "Twin Simulator", icon: SlidersHorizontal },
];

export default function Sidebar() {
  const path = usePathname();
  return (
    <aside className="w-60 shrink-0 h-screen sticky top-0 border-r border-[var(--line)] bg-[var(--panel)] flex flex-col">
      <div className="p-5 border-b border-[var(--line)]">
        <div className="text-[var(--accent)] font-bold text-lg leading-tight glow">KILN-3 · TWIN</div>
        <div className="text-[11px] text-[var(--muted)] mt-1">Calcination Combustion Control</div>
      </div>
      <nav className="p-3 flex flex-col gap-1">
        {NAV.map(({ href, label, icon: Icon }) => {
          const active = href === "/" ? path === "/" : path.startsWith(href);
          return (
            <Link key={href} href={href}
              className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-colors ${
                active ? "bg-[var(--panel-2)] text-[var(--accent)] border border-[var(--line)]"
                       : "text-[var(--muted)] hover:text-[var(--ink)] hover:bg-[var(--panel-2)]"}`}>
              <Icon size={17} />{label}
            </Link>
          );
        })}
      </nav>
      <div className="mt-auto p-4 text-[10px] text-[var(--muted)] border-t border-[var(--line)]">
        JK Cement · Digital Twin v0.1<br />Data: Aug 2025 – Jul 2026
      </div>
    </aside>
  );
}
