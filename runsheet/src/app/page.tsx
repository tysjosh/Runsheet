"use client";

/**
 * Runsheet — editorial landing page.
 *
 * A bold, high-contrast "ops terminal" landing page for the AI dispatch
 * copilot. Each product pillar (01–05) ships a bespoke animated SVG that
 * *illustrates the feature itself*: a runout forecast with P50/P90 bands, a
 * route-replan network with a stalled truck, a grade-segregated tanker, a
 * three-layer agent stack, and the compliance document stack. Pure client
 * component — all motion is CSS keyframes, no deps beyond lucide-react +
 * next/link.
 */

import {
  ArrowRight,
  ArrowUpRight,
  BadgeCheck,
  Droplet,
  Eye,
  FileText,
  Gauge,
  Landmark,
  Plug,
  ScrollText,
  ShieldCheck,
  SlidersHorizontal,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

// ─── Theme ─────────────────────────────────────────────────────────────────────

const ACCENT = "#16b88c";
// Canonical US product codes from the backend fuel product catalog
// (fuel.services.fuel_product_catalog). Kept short so the tanker graphic's
// per-compartment labels stay inside their cells.
const GRADE = {
  DIESEL_2: "#16b88c",
  KEROSENE: "#f59e0b",
  PROPANE: "#3b82f6",
  DEF: "#a78bfa",
} as const;

interface GfxTheme {
  fg: string;
  grid: string;
  panel: string;
  panelEdge: string;
  muted: string;
}

const DARK_GFX: GfxTheme = {
  fg: "#f5f4ef",
  grid: "rgba(245,244,239,0.12)",
  panel: "#101012",
  panelEdge: "rgba(245,244,239,0.14)",
  muted: "rgba(245,244,239,0.45)",
};

const CREAM_GFX: GfxTheme = {
  fg: "#0a0a0b",
  grid: "rgba(10,10,11,0.1)",
  panel: "#ffffff",
  panelEdge: "rgba(10,10,11,0.12)",
  muted: "rgba(10,10,11,0.45)",
};

// ─── Reduced-motion preference ─────────────────────────────────────────────────

function usePrefersReducedMotion() {
  const [reduce, setReduce] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduce(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  return reduce;
}

// ─── Reveal-on-scroll ─────────────────────────────────────────────────────────

function Reveal({
  children,
  className = "",
  delay = 0,
}: {
  children: React.ReactNode;
  className?: string;
  delay?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [shown, setShown] = useState(false);
  const reduce = usePrefersReducedMotion();

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setShown(true);
          obs.disconnect();
        }
      },
      { threshold: 0.18 },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  return (
    <div
      ref={ref}
      className={className}
      style={{
        opacity: shown || reduce ? 1 : 0,
        transform: shown || reduce ? "translateY(0)" : "translateY(40px)",
        transition: reduce
          ? "none"
          : `opacity .9s cubic-bezier(.16,1,.3,1) ${delay}ms, transform .9s cubic-bezier(.16,1,.3,1) ${delay}ms`,
      }}
    >
      {children}
    </div>
  );
}

// ─── Console frame around each feature graphic ──────────────────────────────────

function Console({
  file,
  theme,
  children,
}: {
  file: string;
  theme: GfxTheme;
  children: React.ReactNode;
}) {
  return (
    <div
      className="overflow-hidden rounded-2xl border"
      style={{ background: theme.panel, borderColor: theme.panelEdge }}
    >
      <div
        className="flex items-center gap-2 border-b px-4 py-2.5"
        style={{ borderColor: theme.panelEdge }}
      >
        <span className="flex gap-1.5">
          <span
            className="h-2 w-2 rounded-full"
            style={{ background: "#ef4444" }}
          />
          <span
            className="h-2 w-2 rounded-full"
            style={{ background: "#f59e0b" }}
          />
          <span
            className="h-2 w-2 rounded-full"
            style={{ background: ACCENT }}
          />
        </span>
        <span
          className="ml-2 font-mono text-[10px] uppercase tracking-[0.2em]"
          style={{ color: theme.muted }}
        >
          {file}
        </span>
      </div>
      <div className="p-4">{children}</div>
    </div>
  );
}

// ─── 01 · Forecast graphic ──────────────────────────────────────────────────────

function ForecastGfx({ theme }: { theme: GfxTheme }) {
  return (
    <svg
      viewBox="0 0 400 220"
      className="w-full"
      role="img"
      aria-label="Runout forecast with P50 and P90 confidence bands crossing a runout threshold"
    >
      <defs>
        <linearGradient id="rs-band" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={ACCENT} stopOpacity="0.32" />
          <stop offset="100%" stopColor={ACCENT} stopOpacity="0.04" />
        </linearGradient>
      </defs>

      {/* gridlines */}
      {[44, 88, 132, 176].map((y) => (
        <line
          key={y}
          x1="40"
          y1={y}
          x2="384"
          y2={y}
          stroke={theme.grid}
          strokeWidth="1"
        />
      ))}
      {[40, 126, 212, 298, 384].map((x) => (
        <line
          key={x}
          x1={x}
          y1="24"
          x2={x}
          y2="180"
          stroke={theme.grid}
          strokeWidth="1"
        />
      ))}

      {/* confidence band (P90 envelope) */}
      <path
        d="M40 70 L126 86 L212 112 L298 150 L384 188 L384 150 L298 116 L212 90 L126 72 L40 62 Z"
        fill="url(#rs-band)"
      />

      {/* runout threshold */}
      <line
        x1="40"
        y1="170"
        x2="384"
        y2="170"
        stroke="#ef4444"
        strokeWidth="1.5"
        strokeDasharray="5 4"
      />
      <text
        x="44"
        y="164"
        fontSize="9"
        fontFamily="monospace"
        fill="#ef4444"
        letterSpacing="1"
      >
        RUNOUT
      </text>

      {/* P50 declining line (draws in) */}
      <path
        d="M40 66 L126 79 L212 101 L298 133 L384 169"
        fill="none"
        stroke={ACCENT}
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeDasharray="460"
        strokeDashoffset="460"
        style={{ animation: "rs-draw 2.4s cubic-bezier(.16,1,.3,1) forwards" }}
      />

      {/* crossing marker — where P50 meets the runout line */}
      <g style={{ transformOrigin: "372px 164px" }}>
        <circle
          cx="372"
          cy="164"
          r="9"
          fill="none"
          stroke={ACCENT}
          strokeWidth="1.5"
          style={{ animation: "rs-blip 2s ease-in-out infinite" }}
        />
      </g>
      <circle cx="372" cy="164" r="3.5" fill={ACCENT} />

      {/* labels */}
      <text
        x="40"
        y="200"
        fontSize="9"
        fontFamily="monospace"
        fill={theme.muted}
      >
        0h
      </text>
      <text
        x="196"
        y="200"
        fontSize="9"
        fontFamily="monospace"
        fill={theme.muted}
      >
        36h
      </text>
      <text
        x="364"
        y="200"
        fontSize="9"
        fontFamily="monospace"
        fill={theme.muted}
      >
        72h
      </text>
      {/* legend (kept clear of the plot, above the gridlines) */}
      <line x1="44" y1="14" x2="62" y2="14" stroke={ACCENT} strokeWidth="2.5" />
      <text x="68" y="17" fontSize="9" fontFamily="monospace" fill={ACCENT}>
        P50
      </text>
      <rect x="104" y="9" width="16" height="10" rx="2" fill="url(#rs-band)" />
      <text
        x="126"
        y="17"
        fontSize="9"
        fontFamily="monospace"
        fill={theme.muted}
      >
        P90
      </text>
      <text
        x="12"
        y="100"
        fontSize="9"
        fontFamily="monospace"
        fill={theme.muted}
        transform="rotate(-90 12 100)"
      >
        TANK %
      </text>
    </svg>
  );
}

// ─── 02 · Replan graphic ────────────────────────────────────────────────────────

function ReplanGfx({ theme }: { theme: GfxTheme }) {
  const node = (x: number, y: number, label: string, broken = false) => (
    <g key={label}>
      <circle cx={x} cy={y} r="6" fill={broken ? "#ef4444" : ACCENT} />
      <circle
        cx={x}
        cy={y}
        r="11"
        fill="none"
        stroke={broken ? "#ef4444" : ACCENT}
        strokeOpacity="0.4"
        strokeWidth="1"
      />
      <text
        x={x}
        y={y + 24}
        fontSize="9"
        fontFamily="monospace"
        fill={theme.muted}
        textAnchor="middle"
      >
        {label}
      </text>
    </g>
  );
  return (
    <svg
      viewBox="0 0 400 220"
      className="w-full"
      role="img"
      aria-label="Delivery route rerouting around a stalled truck to an alternate vehicle"
    >
      {/* depot */}
      <rect x="34" y="96" width="20" height="20" rx="3" fill={theme.fg} />
      <text
        x="44"
        y="136"
        fontSize="9"
        fontFamily="monospace"
        fill={theme.muted}
        textAnchor="middle"
      >
        DEPOT
      </text>

      {/* original route (faded, with break) */}
      <path
        d="M54 106 L150 60 L246 60"
        fill="none"
        stroke={theme.muted}
        strokeWidth="1.5"
        strokeDasharray="4 4"
      />
      {/* break marker */}
      <g style={{ transformOrigin: "200px 60px" }}>
        <line
          x1="193"
          y1="53"
          x2="207"
          y2="67"
          stroke="#ef4444"
          strokeWidth="2.5"
        />
        <line
          x1="207"
          y1="53"
          x2="193"
          y2="67"
          stroke="#ef4444"
          strokeWidth="2.5"
        />
      </g>
      {/* stalled truck glyph */}
      <g transform="translate(150 44)">
        <rect
          x="-12"
          y="0"
          width="24"
          height="12"
          rx="2"
          fill="#ef4444"
          fillOpacity="0.85"
        />
        <circle cx="-6" cy="14" r="2.5" fill="#ef4444" />
        <circle cx="6" cy="14" r="2.5" fill="#ef4444" />
      </g>

      {/* reroute path (marching dashes) */}
      <path
        d="M54 106 L150 150 L246 150 L320 96"
        fill="none"
        stroke={ACCENT}
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeDasharray="8 6"
        style={{ animation: "rs-march 1.1s linear infinite" }}
      />
      {/* alternate truck */}
      <g transform="translate(96 132)">
        <rect x="-13" y="0" width="26" height="13" rx="2" fill={ACCENT} />
        <rect x="9" y="2" width="7" height="6" rx="1" fill={ACCENT} />
        <circle cx="-6" cy="15" r="2.5" fill={theme.fg} />
        <circle cx="7" cy="15" r="2.5" fill={theme.fg} />
      </g>

      {/* delivery nodes */}
      {node(246, 60, "S-02 ✕", true)}
      {node(246, 150, "S-03")}
      {node(320, 96, "S-04")}

      {/* status chip */}
      <rect
        x="276"
        y="20"
        width="108"
        height="22"
        rx="11"
        fill={ACCENT}
        fillOpacity="0.14"
      />
      <circle
        cx="290"
        cy="31"
        r="3.5"
        fill={ACCENT}
        style={{ animation: "rs-blip 1.6s ease-in-out infinite" }}
      />
      <text
        x="300"
        y="35"
        fontSize="10"
        fontFamily="monospace"
        fill={ACCENT}
        letterSpacing="1"
      >
        REPLAN · 1.4s
      </text>
    </svg>
  );
}

// ─── 03 · Load / tanker graphic ─────────────────────────────────────────────────

function LoadGfx({ theme }: { theme: GfxTheme }) {
  const comps = [
    { grade: "DIESEL_2", color: GRADE.DIESEL_2, fill: 0.9 },
    { grade: "KEROSENE", color: GRADE.KEROSENE, fill: 0.82 },
    { grade: "PROPANE", color: GRADE.PROPANE, fill: 0.95 },
    { grade: "DEF", color: GRADE.DEF, fill: 0.7 },
  ];
  const x0 = 70;
  const w = 70;
  const top = 40;
  const h = 96;
  return (
    <svg
      viewBox="0 0 400 220"
      className="w-full"
      role="img"
      aria-label="Multi-compartment tanker with grade-segregated fills and utilization gauge"
    >
      {/* cab */}
      <path
        d="M40 96 L40 60 L62 60 L70 88 L70 136 L40 136 Z"
        fill={theme.fg}
        fillOpacity="0.85"
      />
      <rect x="44" y="66" width="14" height="12" rx="2" fill={theme.panel} />
      {/* tank shell */}
      <rect
        x={x0}
        y={top - 6}
        width={w * 4 + 6}
        height={h + 12}
        rx="12"
        fill="none"
        stroke={theme.fg}
        strokeOpacity="0.5"
        strokeWidth="2"
      />

      {comps.map((c, i) => {
        const x = x0 + 3 + i * w;
        const fillH = h * c.fill;
        return (
          <g key={c.grade}>
            {/* compartment outline */}
            <rect
              x={x}
              y={top}
              width={w - 4}
              height={h}
              rx="4"
              fill={theme.fg}
              fillOpacity="0.05"
            />
            {/* liquid fill (rises) */}
            <g
              style={{
                transformOrigin: `${x}px ${top + h}px`,
                animation: `rs-fill 1.6s cubic-bezier(.16,1,.3,1) ${i * 140}ms both`,
              }}
            >
              <rect
                x={x}
                y={top + (h - fillH)}
                width={w - 4}
                height={fillH}
                rx="4"
                fill={c.color}
                fillOpacity="0.85"
              />
              <rect
                x={x}
                y={top + (h - fillH)}
                width={w - 4}
                height="3"
                fill={c.color}
              />
            </g>
            {/* bulkhead */}
            {i < 3 && (
              <line
                x1={x + w - 4}
                y1={top - 4}
                x2={x + w - 4}
                y2={top + h + 4}
                stroke={theme.fg}
                strokeOpacity="0.5"
                strokeWidth="2"
              />
            )}
            {/* grade label — sits below the wheels so the two never overlap */}
            <text
              x={x + (w - 4) / 2}
              y={top + h + 38}
              fontSize="10"
              fontWeight="700"
              fontFamily="monospace"
              fill={theme.fg}
              textAnchor="middle"
            >
              {c.grade}
            </text>
            <text
              x={x + (w - 4) / 2}
              y={top - 14}
              fontSize="9"
              fontFamily="monospace"
              fill={c.color}
              textAnchor="middle"
            >
              {Math.round(c.fill * 100)}%
            </text>
          </g>
        );
      })}

      {/* wheels */}
      {[110, 250, 290].map((cx) => (
        <circle
          key={cx}
          cx={cx}
          cy="150"
          r="9"
          fill={theme.fg}
          fillOpacity="0.85"
        />
      ))}

      {/* utilization gauge */}
      <text
        x="356"
        y="44"
        fontSize="9"
        fontFamily="monospace"
        fill={theme.muted}
        textAnchor="end"
      >
        UTIL
      </text>
      <text
        x="356"
        y="64"
        fontSize="20"
        fontWeight="800"
        fill={ACCENT}
        textAnchor="end"
      >
        92%
      </text>
    </svg>
  );
}

// ─── 04 · Multi-agent graphic ────────────────────────────────────────────────────

function AgentGfx({
  theme,
  reduce = false,
}: {
  theme: GfxTheme;
  reduce?: boolean;
}) {
  const layers = [
    { y: 36, label: "META-LEARNING", dashed: true, nodes: 1 },
    { y: 100, label: "OVERLAY AGENTS", dashed: true, nodes: 3 },
    { y: 164, label: "DOMAIN WATCHDOGS", dashed: false, nodes: 5 },
  ];
  return (
    <svg
      viewBox="0 0 400 220"
      className="w-full"
      role="img"
      aria-label="Three-layer agent stack from domain watchdogs to overlay to meta-learning, with a pulse rising through the layers"
    >
      {/* vertical connectors with rising pulse */}
      {[120, 200, 280].map((x) => (
        <g key={x}>
          <line
            x1={x}
            y1="164"
            x2={x}
            y2="36"
            stroke={theme.grid}
            strokeWidth="1.5"
          />
          {reduce ? (
            <circle cx={x} cy="100" r="3" fill={ACCENT} opacity="0.5" />
          ) : (
            <circle cx={x} cy="164" r="3" fill={ACCENT}>
              <animate
                attributeName="cy"
                values="164;36"
                dur="2.6s"
                repeatCount="indefinite"
                begin={`${(x - 120) / 120}s`}
              />
              <animate
                attributeName="opacity"
                values="0;1;1;0"
                dur="2.6s"
                repeatCount="indefinite"
                begin={`${(x - 120) / 120}s`}
              />
            </circle>
          )}
        </g>
      ))}

      {layers.map((layer) => (
        <g key={layer.label}>
          {Array.from({ length: layer.nodes }).map((_, i) => {
            const cx =
              layer.nodes === 1 ? 200 : 70 + (i * 260) / (layer.nodes - 1);
            return (
              <g key={`${layer.label}-${cx}`}>
                <circle
                  cx={cx}
                  cy={layer.y}
                  r="9"
                  fill={layer.dashed ? "none" : ACCENT}
                  stroke={ACCENT}
                  strokeWidth="1.5"
                  strokeDasharray={layer.dashed ? "3 3" : "0"}
                />
                {!layer.dashed && (
                  <circle cx={cx} cy={layer.y} r="3.5" fill={theme.panel} />
                )}
              </g>
            );
          })}
          <text
            x="20"
            y={layer.y - 16}
            fontSize="8.5"
            fontFamily="monospace"
            fill={theme.muted}
            textAnchor="start"
          >
            {layer.label}
          </text>
        </g>
      ))}

      {/* shadow-mode / autonomy track */}
      <text
        x="16"
        y="202"
        fontSize="8.5"
        fontFamily="monospace"
        fill={theme.muted}
      >
        SHADOW
      </text>
      <line
        x1="70"
        y1="199"
        x2="330"
        y2="199"
        stroke={theme.grid}
        strokeWidth="2"
      />
      {[70, 200, 330].map((x) => (
        <circle key={x} cx={x} cy="199" r="3" fill={theme.muted} />
      ))}
      <circle
        cx="130"
        cy="199"
        r="5"
        fill={ACCENT}
        style={{ animation: "rs-slide 4s ease-in-out infinite" }}
      />
      <text x="344" y="202" fontSize="8.5" fontFamily="monospace" fill={ACCENT}>
        FULL-AUTO
      </text>
    </svg>
  );
}

// ─── Marquee ─────────────────────────────────────────────────────────────────────

function Marquee({
  items,
  direction = "left",
  className = "",
}: {
  items: string[];
  direction?: "left" | "right";
  className?: string;
}) {
  const run = [...items, ...items];
  return (
    <div className={`relative flex overflow-hidden ${className}`}>
      <div
        className="flex shrink-0 items-center whitespace-nowrap"
        style={{ animation: `rs-marquee-${direction} 38s linear infinite` }}
      >
        {run.map((item, i) => (
          <span
            key={`${item}-${i}`}
            className="mx-6 inline-flex items-center text-sm font-semibold uppercase tracking-[0.2em]"
          >
            {item}
            <span className="ml-12 text-[#16b88c]">/</span>
          </span>
        ))}
      </div>
    </div>
  );
}

// ─── Pillar (text + feature graphic) ───────────────────────────────────────────

interface PillarProps {
  id: string;
  index: string;
  kicker: string;
  title: React.ReactNode;
  body: string;
  tags: string[];
  graphic: React.ReactNode;
  accent?: string;
  invert?: boolean;
  flip?: boolean;
  children?: React.ReactNode;
}

function Pillar({
  id,
  index,
  kicker,
  title,
  body,
  tags,
  graphic,
  accent = ACCENT,
  invert = false,
  flip = false,
  children,
}: PillarProps) {
  const text = invert ? "#0a0a0b" : "#f5f4ef";
  return (
    <section
      id={id}
      className={`relative border-t ${invert ? "bg-[#f5f4ef]" : ""}`}
      style={{
        color: text,
        borderColor: invert ? "rgba(10,10,11,0.12)" : "rgba(245,244,239,0.12)",
      }}
    >
      <div className="mx-auto grid max-w-7xl items-center gap-12 px-6 py-20 lg:grid-cols-2 lg:gap-16 lg:px-10 lg:py-28">
        {/* TEXT */}
        <div className={flip ? "lg:order-2" : ""}>
          <Reveal>
            <div
              className="mb-8 flex items-center gap-4 font-mono text-xs uppercase tracking-[0.3em]"
              style={{
                color: invert
                  ? "rgba(10,10,11,0.62)"
                  : "rgba(245,244,239,0.62)",
              }}
            >
              <span style={{ color: accent }}>{index}</span>
              <span
                className="h-px w-[90px]"
                style={{ background: accent, opacity: 0.5 }}
              />
              <span>{kicker}</span>
            </div>
          </Reveal>
          <Reveal delay={60}>
            <h2 className="text-[clamp(2.5rem,7vw,5.5rem)] font-black uppercase leading-[0.9] tracking-[-0.03em]">
              {title}
            </h2>
          </Reveal>
          <Reveal delay={140}>
            <p
              className="mt-7 max-w-lg text-base leading-relaxed lg:text-lg"
              style={{
                color: invert ? "rgba(10,10,11,0.8)" : "rgba(245,244,239,0.82)",
              }}
            >
              {body}
            </p>
          </Reveal>
          {children && (
            <Reveal delay={200}>
              <div className="mt-6">{children}</div>
            </Reveal>
          )}
          <Reveal delay={240}>
            <div className="mt-8 flex flex-wrap gap-x-3 gap-y-2">
              {tags.map((t) => (
                <span
                  key={t}
                  className="rounded-full border px-3 py-1 font-mono text-[10px] uppercase tracking-[0.2em]"
                  style={{
                    borderColor: `color-mix(in srgb, ${accent} 45%, transparent)`,
                    color: invert
                      ? "rgba(10,10,11,0.78)"
                      : "rgba(245,244,239,0.78)",
                  }}
                >
                  {t}
                </span>
              ))}
            </div>
          </Reveal>
        </div>

        {/* GRAPHIC */}
        <Reveal delay={120} className={flip ? "lg:order-1" : ""}>
          {graphic}
        </Reveal>
      </div>
    </section>
  );
}

// ─── Hero console (product preview) ────────────────────────────────────────────

const HERO_QUEUE = [
  { id: "STN-014", dry: "9h to dry", status: "URGENT", color: "#f59e0b" },
  { id: "STN-007", dry: "18h to dry", status: "ASSIGNED", color: ACCENT },
  { id: "STN-022", dry: "31h to dry", status: "SCHEDULED", color: "#3b82f6" },
];

function HeroConsole() {
  return (
    <Console file="dispatch.console" theme={DARK_GFX}>
      <div className="mb-3 flex items-center justify-between">
        <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-[#f5f4ef]/65">
          Runout Forecast · 72h
        </span>
        <span className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.2em] text-[#16b88c]">
          <span className="h-1.5 w-1.5 rounded-full bg-[#16b88c]" />
          Live
        </span>
      </div>
      <ForecastGfx theme={DARK_GFX} />
      <div className="mt-4 space-y-1.5">
        {HERO_QUEUE.map((row) => (
          <div
            key={row.id}
            className="flex items-center justify-between rounded-lg border border-[#f5f4ef]/10 bg-[#0a0a0b] px-3 py-2"
          >
            <span className="font-mono text-[11px] tracking-wide text-[#f5f4ef]/85">
              {row.id}
            </span>
            <span className="font-mono text-[11px] text-[#f5f4ef]/55">
              {row.dry}
            </span>
            <span
              className="rounded-full px-2 py-0.5 font-mono text-[9px] uppercase tracking-[0.15em]"
              style={{
                color: row.color,
                background: `color-mix(in srgb, ${row.color} 16%, transparent)`,
              }}
            >
              {row.status}
            </span>
          </div>
        ))}
      </div>
    </Console>
  );
}

// ─── Keyframes ───────────────────────────────────────────────────────────────────

function LandingStyles() {
  return (
    <style>{`
      @keyframes rs-marquee-left { 0% { transform: translateX(0); } 100% { transform: translateX(-50%); } }
      @keyframes rs-marquee-right { 0% { transform: translateX(-50%); } 100% { transform: translateX(0); } }
      @keyframes rs-grid-drift { 0% { background-position: 0 0; } 100% { background-position: 60px 60px; } }
      @keyframes rs-pulse-glow { 0%,100% { opacity: .35; } 50% { opacity: .75; } }
      @keyframes rs-rise { 0% { transform: translateY(14px); opacity: 0; } 100% { transform: translateY(0); opacity: 1; } }
      @keyframes rs-draw { to { stroke-dashoffset: 0; } }
      @keyframes rs-march { to { stroke-dashoffset: -28; } }
      @keyframes rs-blip { 0%,100% { transform: scale(0.6); opacity: .4; } 50% { transform: scale(1.15); opacity: 1; } }
      @keyframes rs-fill { from { transform: scaleY(0); } to { transform: scaleY(1); } }
      @keyframes rs-slide { 0%,100% { transform: translateX(0); } 50% { transform: translateX(200px); } }
      .rs-hero-word { animation: rs-rise .9s cubic-bezier(.16,1,.3,1) both; }
      .rs-root a:focus-visible, .rs-root button:focus-visible {
        outline: 2px solid #16b88c;
        outline-offset: 3px;
        border-radius: 8px;
      }
      @media (prefers-reduced-motion: reduce) {
        .rs-root *, .rs-root *::before, .rs-root *::after {
          animation: none !important;
          transition: none !important;
        }
      }
    `}</style>
  );
}

const HERO_STATS = [
  { value: "24–72h", label: "Runout forecast horizon" },
  { value: "< 2 min", label: "Disruption replan time" },
  { value: "85–95%", label: "Truck utilization" },
  { value: "< 500ms", label: "Load constraint solve" },
];

const NAV_LINKS = [
  ["Forecasting", "#forecasting"],
  ["Replanning", "#replanning"],
  ["Loading", "#loading"],
  ["AI Agents", "#agents"],
  ["Compliance", "#compliance"],
  ["How", "#how"],
] as const;

const COMPLIANCE = [
  {
    icon: Landmark,
    title: "Tax Engine",
    body: "Federal · State · County · City (FIPS)",
  },
  {
    icon: Gauge,
    title: "Volume Correction",
    body: "API 2540 VCF · Gross → Net at 60°F",
  },
  {
    icon: BadgeCheck,
    title: "Driver Compliance",
    body: "CDL · HAZMAT · Medical card expiry",
  },
  {
    icon: Droplet,
    title: "Enforcement",
    body: "Dyed diesel · IRS 637M · Exemption certs",
  },
  {
    icon: FileText,
    title: "Reporting",
    body: "IFTA quarterly · Form 720 · Audit-ready",
  },
];

// Per-pillar accent hues — a controlled palette break so the page doesn't read
// as one flat green. The emerald action color (CTAs) stays constant; only the
// decorative kicker/index/tags shift.
const PILLAR_ACCENT = {
  forecasting: ACCENT,
  replanning: "#3b82f6",
  loading: ACCENT,
  agents: "#a78bfa",
} as const;

// Real regulatory standards Runsheet implements — used in place of fabricated
// customer logos (there are none to show yet).
const STANDARDS = [
  "API 2540 VCF",
  "DOT / FMCSA",
  "IFTA",
  "IRS 637M",
  "Form 720",
];

// Architecture-true trust claims (not marketing fluff, not fake certifications).
const TRUST_PILLARS = [
  {
    icon: Eye,
    title: "Shadow-mode rollout",
    body: "Agents observe and earn autonomy. Nothing goes live until it's validated against your data.",
  },
  {
    icon: SlidersHorizontal,
    title: "Human-in-the-loop",
    body: "Configurable autonomy per operation — suggest-only, auto-low, or full-auto, with approval gates.",
  },
  {
    icon: ShieldCheck,
    title: "Tenant-isolated",
    body: "Every distributor's data is scoped and isolated end to end. References never cross tenants.",
  },
  {
    icon: ScrollText,
    title: "Full audit trail",
    body: "Every agent decision is logged and explainable — across all three agent layers.",
  },
];

// How it works — the real rollout path, mapped to the shadow-mode model.
const STEPS = [
  {
    icon: Plug,
    title: "Connect",
    body: "Stream tank telemetry, delivery history, and your fleet roster. Runsheet runs alongside your existing stack — no rip-and-replace.",
  },
  {
    icon: Eye,
    title: "Observe",
    body: "Agents forecast runouts, optimize loads, and draft replans in shadow mode. You watch every call before it can act.",
  },
  {
    icon: SlidersHorizontal,
    title: "Approve",
    body: "Promote agents from suggest-only to full-auto, one operation at a time, with a full audit trail on every decision.",
  },
];

// ─── Main page ───────────────────────────────────────────────────────────────────

export default function LandingPage() {
  const [scrolled, setScrolled] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const reduce = usePrefersReducedMotion();

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 16);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <div className="rs-root min-h-screen bg-[#0a0a0b] text-[#f5f4ef] antialiased">
      <LandingStyles />

      {/* NAV */}
      <header
        className={`fixed inset-x-0 top-0 z-50 transition-all duration-300 ${
          scrolled
            ? "border-b border-[#f5f4ef]/10 bg-[#0a0a0b]/85 backdrop-blur-xl"
            : "border-b border-transparent"
        }`}
      >
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-3 lg:px-10">
          <Link href="/" className="flex items-baseline gap-px">
            <span className="text-lg font-black uppercase tracking-tight">
              RUN<span className="text-[#16b88c]">/</span>SHEET
            </span>
            <span className="ml-1.5 font-mono text-[9px] uppercase tracking-[0.3em] text-[#f5f4ef]/40">
              beta
            </span>
          </Link>

          <nav className="hidden items-center gap-8 lg:flex">
            {NAV_LINKS.map(([label, href]) => (
              <a
                key={label}
                href={href}
                className="font-mono text-[11px] uppercase tracking-[0.2em] text-[#f5f4ef]/70 transition-colors hover:text-[#f5f4ef]"
              >
                {label}
              </a>
            ))}
          </nav>

          <div className="flex items-center gap-3">
            <Link
              href="/signin"
              className="hidden font-mono text-[11px] uppercase tracking-[0.2em] text-[#f5f4ef]/60 transition-colors hover:text-[#f5f4ef] sm:inline"
            >
              Sign In
            </Link>
            <Link
              href="/request-pilot"
              className="group inline-flex items-center gap-1.5 rounded-full bg-[#16b88c] px-4 py-2 text-[11px] font-bold uppercase tracking-[0.15em] text-[#06231b] transition-all hover:bg-[#1ed3a0]"
            >
              Request Pilot
              <ArrowRight className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" />
            </Link>
            <button
              type="button"
              onClick={() => setMenuOpen((v) => !v)}
              className="font-mono text-[11px] uppercase tracking-[0.2em] text-[#f5f4ef]/70 lg:hidden"
              aria-label="Toggle menu"
            >
              {menuOpen ? "Close" : "Menu"}
            </button>
          </div>
        </div>

        {menuOpen && (
          <div className="border-t border-[#f5f4ef]/10 bg-[#0a0a0b] px-6 py-4 lg:hidden">
            {NAV_LINKS.map(([label, href]) => (
              <a
                key={label}
                href={href}
                onClick={() => setMenuOpen(false)}
                className="block py-2.5 font-mono text-xs uppercase tracking-[0.2em] text-[#f5f4ef]/70"
              >
                {label}
              </a>
            ))}
          </div>
        )}
      </header>

      {/* HERO */}
      <section className="relative overflow-hidden pt-28 pb-20 lg:pt-32 lg:pb-28">
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 opacity-[0.14]"
          style={{
            backgroundImage:
              "linear-gradient(#f5f4ef 1px, transparent 1px), linear-gradient(90deg, #f5f4ef 1px, transparent 1px)",
            backgroundSize: "60px 60px",
            animation: "rs-grid-drift 8s linear infinite",
            maskImage:
              "radial-gradient(ellipse 80% 60% at 50% 25%, #000 30%, transparent 75%)",
            WebkitMaskImage:
              "radial-gradient(ellipse 80% 60% at 50% 25%, #000 30%, transparent 75%)",
          }}
        />
        <div
          aria-hidden
          className="pointer-events-none absolute left-1/2 top-20 h-[420px] w-[820px] -translate-x-1/2 rounded-full blur-[140px]"
          style={{
            background:
              "radial-gradient(circle, rgba(22,184,140,0.26), transparent 70%)",
            animation: "rs-pulse-glow 6s ease-in-out infinite",
          }}
        />

        <div className="relative mx-auto max-w-7xl px-6 lg:px-10">
          <div className="mb-8 flex items-center gap-3 font-mono text-[11px] uppercase tracking-[0.3em] text-[#16b88c]">
            <span className="h-px w-8 bg-[#16b88c]" />
            AI Dispatch Copilot · For Regional Fuel Distributors
          </div>

          <h1 className="text-[clamp(3.25rem,13vw,10.5rem)] font-black uppercase leading-[0.84] tracking-[-0.04em]">
            <span
              className="rs-hero-word block"
              style={{ animationDelay: "0ms" }}
            >
              Stop
            </span>
            <span
              className="rs-hero-word block text-[#f5f4ef]/40"
              style={{ animationDelay: "120ms" }}
            >
              The
            </span>
            <span
              className="rs-hero-word block text-[#16b88c]"
              style={{ animationDelay: "240ms" }}
            >
              Runout.
            </span>
          </h1>

          <div className="mt-10 grid gap-10 lg:grid-cols-[1fr_1.15fr] lg:items-end">
            <div>
              <Reveal delay={360}>
                <p className="max-w-xl text-lg leading-relaxed text-[#f5f4ef]/70">
                  Autonomous AI operations for fuel distributors. Predict
                  runouts, optimize truck loads, and replan disruptions in
                  seconds — no rip-and-replace.
                </p>
              </Reveal>
              <Reveal delay={440}>
                <div className="mt-8 flex flex-wrap items-center gap-3">
                  <Link
                    href="/request-pilot"
                    className="group inline-flex items-center gap-2 rounded-full bg-[#16b88c] px-7 py-3.5 text-sm font-bold uppercase tracking-[0.12em] text-[#06231b] transition-all hover:bg-[#1ed3a0]"
                  >
                    Request a Pilot
                    <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
                  </Link>
                  <a
                    href="#how"
                    className="inline-flex items-center gap-2 rounded-full border border-[#f5f4ef]/25 px-7 py-3.5 text-sm font-bold uppercase tracking-[0.12em] text-[#f5f4ef] transition-all hover:border-[#f5f4ef]/60"
                  >
                    See How It Works ↓
                  </a>
                </div>
              </Reveal>
            </div>

            <Reveal delay={480}>
              <HeroConsole />
            </Reveal>
          </div>

          <Reveal delay={560}>
            <div className="mt-16 grid grid-cols-2 gap-px overflow-hidden rounded-2xl border border-[#f5f4ef]/10 bg-[#f5f4ef]/10 lg:grid-cols-4">
              {HERO_STATS.map((s) => (
                <div key={s.label} className="bg-[#0a0a0b] p-6">
                  <div className="text-3xl font-black tracking-tight text-[#16b88c] lg:text-4xl">
                    {s.value}
                  </div>
                  <div className="mt-2 font-mono text-[10px] uppercase tracking-[0.2em] text-[#f5f4ef]/65">
                    {s.label}
                  </div>
                </div>
              ))}
            </div>
            <p className="mt-3 font-mono text-[10px] uppercase tracking-[0.15em] text-[#f5f4ef]/45">
              * Design targets from in-development pilots — not guaranteed
              results.
            </p>
          </Reveal>
        </div>
      </section>

      {/* MARQUEE 1 */}
      <div className="border-y border-[#f5f4ef]/10 bg-[#16b88c] py-3 text-[#06231b]">
        <Marquee
          items={[
            "Runout Prevention",
            "P50/P90 Forecasts",
            "Exception Replanning",
            "Multi-Compartment Loading",
            "Shadow-Mode Agents",
            "API 2540 VCF",
            "IFTA Reporting",
            "DOT / FMCSA Compliance",
          ]}
        />
      </div>

      {/* TRUST / STANDARDS */}
      <section className="relative border-t border-[#f5f4ef]/10 bg-[#0d0d0f]">
        <div className="mx-auto max-w-7xl px-6 py-16 lg:px-10 lg:py-20">
          <Reveal>
            <p className="text-center font-mono text-[11px] uppercase tracking-[0.3em] text-[#f5f4ef]/60">
              Built on the standards your auditors already use
            </p>
            <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
              {STANDARDS.map((s) => (
                <span
                  key={s}
                  className="rounded-full border border-[#f5f4ef]/15 px-4 py-1.5 font-mono text-[11px] uppercase tracking-[0.18em] text-[#f5f4ef]/75"
                >
                  {s}
                </span>
              ))}
            </div>
          </Reveal>

          <div className="mt-14 grid gap-px overflow-hidden rounded-2xl border border-[#f5f4ef]/10 bg-[#f5f4ef]/10 sm:grid-cols-2 lg:grid-cols-4">
            {TRUST_PILLARS.map((t, i) => {
              const Icon = t.icon;
              return (
                <Reveal key={t.title} delay={i * 70}>
                  <div className="h-full bg-[#0d0d0f] p-6">
                    <div className="mb-5 flex h-10 w-10 items-center justify-center rounded-lg bg-[#16b88c]/12">
                      <Icon className="h-5 w-5 text-[#16b88c]" />
                    </div>
                    <h3 className="text-sm font-bold uppercase tracking-[0.06em]">
                      {t.title}
                    </h3>
                    <p className="mt-3 text-sm leading-relaxed text-[#f5f4ef]/70">
                      {t.body}
                    </p>
                  </div>
                </Reveal>
              );
            })}
          </div>
        </div>
      </section>

      {/* HOW IT WORKS */}
      <section id="how" className="relative border-t border-[#f5f4ef]/10">
        <div className="mx-auto max-w-7xl px-6 py-20 lg:px-10 lg:py-28">
          <Reveal>
            <div className="mb-8 flex items-center gap-4 font-mono text-xs uppercase tracking-[0.3em] text-[#f5f4ef]/62">
              <span className="text-[#16b88c]">→</span>
              <span className="h-px w-[90px] bg-[#16b88c] opacity-50" />
              How it works
            </div>
          </Reveal>
          <Reveal delay={60}>
            <h2 className="max-w-3xl text-[clamp(2.5rem,7vw,5.5rem)] font-black uppercase leading-[0.9] tracking-[-0.03em]">
              Live in days.
              <br />
              <span className="text-[#f5f4ef]/40">
                Autonomous on your terms.
              </span>
            </h2>
          </Reveal>
          <div className="mt-14 grid gap-px overflow-hidden rounded-2xl border border-[#f5f4ef]/10 bg-[#f5f4ef]/10 md:grid-cols-3">
            {STEPS.map((step, i) => {
              const Icon = step.icon;
              return (
                <Reveal key={step.title} delay={i * 90}>
                  <div className="h-full bg-[#0a0a0b] p-7">
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-[#16b88c]">
                        0{i + 1}
                      </span>
                      <Icon className="h-5 w-5 text-[#f5f4ef]/55" />
                    </div>
                    <h3 className="mt-6 text-xl font-black uppercase tracking-tight">
                      {step.title}
                    </h3>
                    <p className="mt-3 text-sm leading-relaxed text-[#f5f4ef]/70">
                      {step.body}
                    </p>
                  </div>
                </Reveal>
              );
            })}
          </div>
        </div>
      </section>

      {/* 01 FORECASTING */}
      <Pillar
        id="forecasting"
        index="01"
        kicker="Predictive Forecasting"
        title={
          <>
            Predict
            <br />
            before dry.
          </>
        }
        body="Runsheet generates 24–72 hour runout forecasts with P50/P90 confidence for every station — pulling from tank telemetry, delivery history, and demand patterns. Anomaly detection flags sensor drift before it corrupts your plan."
        tags={["P50/P90 Forecasts", "Anomaly Detection", "SLA Prioritization"]}
        graphic={
          <Console file="forecast.engine" theme={DARK_GFX}>
            <ForecastGfx theme={DARK_GFX} />
          </Console>
        }
      />

      {/* 02 REPLANNING */}
      <Pillar
        id="replanning"
        index="02"
        kicker="Exception Replanning"
        flip
        title={
          <>
            Fixed in
            <br />
            <span className="text-[#16b88c]">seconds.</span>
          </>
        }
        body="Truck breakdown. Station outage. Demand spike. The replanning agent patches your plan in real time — finding replacement trucks with compatible compartments, reoptimizing stops, and adjusting quantities. Disruption response drops from 45+ minutes to under 2."
        tags={[
          "Disruption Response",
          "Route Reoptimization",
          "Smart Escalation",
        ]}
        accent={PILLAR_ACCENT.replanning}
        graphic={
          <Console file="replan.agent" theme={DARK_GFX}>
            <ReplanGfx theme={DARK_GFX} />
          </Console>
        }
      />

      {/* 03 LOADING */}
      <Pillar
        id="loading"
        index="03"
        kicker="Load Optimization"
        invert
        title={
          <>
            Loaded
            <br />
            right.
          </>
        }
        body="Auto-generate optimal loading plans for multi-compartment tankers — enforcing absolute AGO · PMS · ATK · LPG grade segregation, pushing utilization to 85–95%, and enabling multi-drop routes in a single trip. Constraint solving completes in under 500ms."
        tags={["Grade Segregation", "Multi-Drop Routing", "85–95% Utilization"]}
        graphic={
          <Console file="load.solver" theme={CREAM_GFX}>
            <LoadGfx theme={CREAM_GFX} />
          </Console>
        }
      />

      {/* 04 AGENTS */}
      <Pillar
        id="agents"
        index="04"
        kicker="Multi-Agent AI"
        flip
        title={
          <>
            <span className="block">Human</span>
            <span className="block text-[#16b88c]">first.</span>
          </>
        }
        body="Three agent layers run continuously: domain watchdogs monitor operations, overlay agents optimize across the fleet, and a meta-learning agent improves decisions over time. Every agent starts in shadow mode and earns autonomy through validated performance."
        tags={[
          "Shadow Mode",
          "Configurable Autonomy",
          "Continuous Learning",
          "Audit Trail",
        ]}
        accent={PILLAR_ACCENT.agents}
        graphic={
          <Console file="agent.stack" theme={DARK_GFX}>
            <AgentGfx theme={DARK_GFX} reduce={reduce} />
          </Console>
        }
      >
        <ul className="space-y-2.5">
          {[
            "Shadow mode first — agents go active only after validation.",
            "Configurable autonomy: suggest-only → auto-low → full-auto.",
            "Full audit trail on every agent decision, across every layer.",
          ].map((line) => (
            <li key={line} className="flex items-start gap-2.5">
              <ArrowUpRight className="mt-0.5 h-4 w-4 shrink-0 text-[#16b88c]" />
              <span className="text-sm text-[#f5f4ef]/70">{line}</span>
            </li>
          ))}
        </ul>
      </Pillar>

      {/* MARQUEE 2 */}
      <div className="border-y border-[#f5f4ef]/10 bg-[#0a0a0b] py-3 text-[#f5f4ef]">
        <Marquee
          direction="right"
          items={[
            "Autonomous Fuel Ops",
            "85–95% Truck Utilization",
            "2-Min Disruption Response",
            "Human-in-Loop",
            "Dyed-Diesel Enforcement",
            "Form 720 Ready",
            "Explainable AI",
            "Regional Distributors",
          ]}
        />
      </div>

      {/* 05 COMPLIANCE */}
      <section
        id="compliance"
        className="relative border-t border-[#f5f4ef]/10"
      >
        <div className="mx-auto max-w-7xl px-6 py-20 lg:px-10 lg:py-28">
          <Reveal>
            <div className="mb-8 flex items-center gap-4 font-mono text-xs uppercase tracking-[0.3em] text-[#f5f4ef]/62">
              <span className="text-[#16b88c]">05</span>
              <span className="h-px w-[90px] bg-[#16b88c] opacity-50" />
              Compliance Backbone
            </div>
          </Reveal>
          <Reveal delay={60}>
            <h2 className="max-w-4xl text-[clamp(2.5rem,7vw,5.5rem)] font-black uppercase leading-[0.9] tracking-[-0.03em]">
              Built in.
              <br />
              <span className="text-[#f5f4ef]/40">Not bolted on.</span>
            </h2>
          </Reveal>
          <Reveal delay={140}>
            <p className="mt-7 max-w-2xl text-base leading-relaxed text-[#f5f4ef]/70 lg:text-lg">
              Runsheet handles the entire regulatory stack for U.S. fuel
              distribution — multi-jurisdiction excise tax, API 2540 volume
              correction, DOT/FMCSA driver qualification, dyed-diesel
              enforcement, and IFTA reporting.
            </p>
          </Reveal>

          <div className="mt-14 grid gap-px overflow-hidden rounded-2xl border border-[#f5f4ef]/10 bg-[#f5f4ef]/10 sm:grid-cols-2 lg:grid-cols-5">
            {COMPLIANCE.map((c, i) => {
              const Icon = c.icon;
              return (
                <Reveal key={c.title} delay={i * 70}>
                  <div className="h-full bg-[#0a0a0b] p-6">
                    <div className="mb-5 flex h-10 w-10 items-center justify-center rounded-lg bg-[#16b88c]/12">
                      <Icon className="h-5 w-5 text-[#16b88c]" />
                    </div>
                    <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.2em] text-[#16b88c]">
                      0{i + 1}
                    </div>
                    <h3 className="text-sm font-bold uppercase tracking-[0.08em]">
                      {c.title}
                    </h3>
                    <p className="mt-3 text-xs leading-relaxed text-[#f5f4ef]/70">
                      {c.body}
                    </p>
                  </div>
                </Reveal>
              );
            })}
          </div>
        </div>
      </section>

      {/* FINAL CTA */}
      <section className="relative overflow-hidden border-t border-[#f5f4ef]/10">
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 opacity-[0.1]"
          style={{
            backgroundImage:
              "linear-gradient(#f5f4ef 1px, transparent 1px), linear-gradient(90deg, #f5f4ef 1px, transparent 1px)",
            backgroundSize: "48px 48px",
          }}
        />
        <div className="relative mx-auto max-w-7xl px-6 py-28 text-center lg:px-10 lg:py-40">
          <Reveal>
            <h2 className="text-[clamp(3rem,11vw,9rem)] font-black uppercase leading-[0.86] tracking-[-0.04em]">
              Run the
              <br />
              <span className="text-[#16b88c]">whole sheet.</span>
            </h2>
          </Reveal>
          <Reveal delay={140}>
            <p className="mx-auto mt-8 max-w-xl text-lg text-[#f5f4ef]/65">
              From forecast to delivery to Form 720 — one autonomous operations
              layer for regional fuel distributors.
            </p>
          </Reveal>
          <Reveal delay={220}>
            <div className="mt-10 flex flex-wrap items-center justify-center gap-3">
              <Link
                href="/request-pilot"
                className="group inline-flex items-center gap-2 rounded-full bg-[#16b88c] px-8 py-4 text-sm font-bold uppercase tracking-[0.12em] text-[#06231b] transition-all hover:bg-[#1ed3a0]"
              >
                Request a Pilot
                <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
              </Link>
              <Link
                href="/signin"
                className="inline-flex items-center gap-2 rounded-full border border-[#f5f4ef]/20 px-8 py-4 text-sm font-bold uppercase tracking-[0.12em] text-[#f5f4ef] transition-all hover:border-[#f5f4ef]/50"
              >
                Sign In
              </Link>
            </div>
          </Reveal>
        </div>
      </section>

      {/* FOOTER */}
      <footer className="border-t border-[#f5f4ef]/10 bg-[#0a0a0b]">
        <div className="mx-auto max-w-7xl px-6 py-14 lg:px-10">
          <div className="grid gap-10 md:grid-cols-[1.4fr_1fr_1fr_1fr]">
            <div>
              <Link href="/" className="flex items-baseline gap-px">
                <span className="text-2xl font-black uppercase tracking-tight">
                  RUN<span className="text-[#16b88c]">/</span>SHEET
                </span>
              </Link>
              <p className="mt-4 max-w-xs text-sm leading-relaxed text-[#f5f4ef]/55">
                Autonomous AI operations for regional fuel distributors —
                forecast, load, replan, and stay compliant.
              </p>
            </div>

            <div>
              <h4 className="mb-4 font-mono text-[10px] uppercase tracking-[0.25em] text-[#f5f4ef]/45">
                Platform
              </h4>
              <ul className="space-y-2.5">
                {NAV_LINKS.map(([label, href]) => (
                  <li key={label}>
                    <a
                      href={href}
                      className="text-sm text-[#f5f4ef]/70 transition-colors hover:text-[#f5f4ef]"
                    >
                      {label}
                    </a>
                  </li>
                ))}
              </ul>
            </div>

            <div>
              <h4 className="mb-4 font-mono text-[10px] uppercase tracking-[0.25em] text-[#f5f4ef]/45">
                Get started
              </h4>
              <ul className="space-y-2.5">
                <li>
                  <Link
                    href="/request-pilot"
                    className="text-sm text-[#f5f4ef]/70 transition-colors hover:text-[#f5f4ef]"
                  >
                    Request a Pilot
                  </Link>
                </li>
                <li>
                  <Link
                    href="/signin"
                    className="text-sm text-[#f5f4ef]/70 transition-colors hover:text-[#f5f4ef]"
                  >
                    Sign In
                  </Link>
                </li>
              </ul>
            </div>

            <div>
              <h4 className="mb-4 font-mono text-[10px] uppercase tracking-[0.25em] text-[#f5f4ef]/45">
                Contact
              </h4>
              <ul className="space-y-2.5">
                <li>
                  <a
                    href="mailto:hello@runsheet.app"
                    className="text-sm text-[#f5f4ef]/70 transition-colors hover:text-[#f5f4ef]"
                  >
                    hello@runsheet.app
                  </a>
                </li>
                <li className="text-sm text-[#f5f4ef]/55">United States</li>
              </ul>
            </div>
          </div>

          <div className="mt-12 flex flex-col gap-2 border-t border-[#f5f4ef]/10 pt-6 font-mono text-[10px] uppercase tracking-[0.22em] text-[#f5f4ef]/50 sm:flex-row sm:items-center sm:justify-between">
            <span>© {new Date().getFullYear()} Runsheet · Beta</span>
            <span>
              Fuel Distribution · Runout Prevention · Dispatch Support
            </span>
          </div>
        </div>
      </footer>
    </div>
  );
}
