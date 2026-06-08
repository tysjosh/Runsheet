"use client";

/**
 * Compact autonomy-level banner for the Operations Control supervision view.
 *
 * Read-only indicator of the tenant's current agent autonomy level. Under
 * `full-auto` / `auto-medium` the agents execute actions without per-action
 * approval, so the supervisor needs this state visible at a glance. Changing
 * the level stays in Settings → Agents (admin-gated radio control); this
 * banner only surfaces the current level and links there.
 *
 * Backed by GET /api/agent/config/autonomy (services/agentApi.getAutonomyLevel).
 */

import { Shield, ShieldAlert, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import type { AutonomyLevel } from "../../services/agentApi";
import { getAutonomyLevel } from "../../services/agentApi";

const LEVEL_META: Record<
  AutonomyLevel,
  {
    label: string;
    blurb: string;
    bg: string;
    text: string;
    Icon: typeof Shield;
  }
> = {
  "suggest-only": {
    label: "Suggest-only",
    blurb: "All agent actions wait for human approval.",
    bg: "bg-gray-100 border-gray-200",
    text: "text-gray-700",
    Icon: ShieldCheck,
  },
  "auto-low": {
    label: "Auto (low risk)",
    blurb: "Low-risk actions auto-execute; medium/high wait for approval.",
    bg: "bg-blue-50 border-blue-200",
    text: "text-blue-700",
    Icon: Shield,
  },
  "auto-medium": {
    label: "Auto (medium risk)",
    blurb: "Low/medium actions auto-execute; high-risk waits for approval.",
    bg: "bg-amber-50 border-amber-200",
    text: "text-amber-700",
    Icon: ShieldAlert,
  },
  "full-auto": {
    label: "Full autonomy",
    blurb:
      "All actions auto-execute, including high-risk ones. Monitor the activity feed.",
    bg: "bg-error-light border-error-light",
    text: "text-error-dark",
    Icon: ShieldAlert,
  },
};

export default function AgentAutonomyBanner() {
  const [level, setLevel] = useState<AutonomyLevel | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const result = await getAutonomyLevel();
        if (!cancelled) setLevel(result.level);
      } catch {
        if (!cancelled) setFailed(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (failed || !level) return null;

  const meta = LEVEL_META[level] ?? LEVEL_META["suggest-only"];
  const { Icon } = meta;

  return (
    <div
      className={`flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border px-4 py-2.5 ${meta.bg}`}
      role="status"
    >
      <span
        className={`inline-flex items-center gap-2 font-semibold ${meta.text}`}
      >
        <Icon className="h-4 w-4 flex-shrink-0" />
        Autonomy: {meta.label}
      </span>
      <span className={`text-sm ${meta.text} opacity-80`}>{meta.blurb}</span>
      <a
        href="/dashboard/settings"
        className={`ml-auto text-xs font-medium underline ${meta.text} hover:opacity-80`}
      >
        Change in Settings
      </a>
    </div>
  );
}
