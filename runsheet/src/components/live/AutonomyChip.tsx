"use client";

/**
 * Agent autonomy as a chip in Live's title row (UI revamp R9.2): the current
 * level with an icon and its name, linking to Settings → Agents where an
 * admin changes it. Replaces the full-width autonomy banner. Hidden when the
 * level can't be read.
 */
import { Shield, ShieldAlert, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { type AutonomyLevel, getAutonomyLevel } from "../../services/agentApi";

const META: Record<
  AutonomyLevel,
  { label: string; blurb: string; cls: string; Icon: typeof Shield }
> = {
  "suggest-only": {
    label: "Suggest-only",
    blurb: "All agent actions wait for human approval.",
    cls: "border-slate-300 bg-slate-50 text-slate-800",
    Icon: ShieldCheck,
  },
  "auto-low": {
    label: "Auto (low risk)",
    blurb: "Low-risk actions auto-execute; medium and high wait for approval.",
    cls: "border-blue-300 bg-blue-50 text-blue-800",
    Icon: Shield,
  },
  "auto-medium": {
    label: "Auto (medium risk)",
    blurb: "Low and medium actions auto-execute; high-risk waits for approval.",
    cls: "border-amber-300 bg-amber-50 text-amber-800",
    Icon: ShieldAlert,
  },
  "full-auto": {
    label: "Full autonomy",
    blurb: "All actions auto-execute, including high-risk ones.",
    cls: "border-red-300 bg-red-50 text-red-800",
    Icon: ShieldAlert,
  },
};

export function AutonomyChip() {
  const [level, setLevel] = useState<AutonomyLevel | null>(null);
  useEffect(() => {
    let cancelled = false;
    getAutonomyLevel()
      .then((r) => {
        if (!cancelled) setLevel(r.level);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);
  if (!level) return null;
  const meta = META[level] ?? META["suggest-only"];
  const { Icon } = meta;
  return (
    <Link
      href="/dashboard/settings?tab=agents"
      title={`${meta.blurb} Change it in Settings → Agents.`}
      className={`inline-flex h-7 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 text-xs font-semibold hover:brightness-95 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus ${meta.cls}`}
    >
      <Icon aria-hidden="true" className="h-3.5 w-3.5" />
      <span>
        <span className="sr-only">Agent autonomy: </span>
        {meta.label}
      </span>
    </Link>
  );
}

export default AutonomyChip;
