"use client";

import {
  ArrowRight,
  BarChart3,
  Bot,
  CheckCircle,
  Clock,
  Droplets,
  Eye,
  Globe,
  Link2,
  Lock,
  Menu,
  Route,
  Shield,
  Sparkles,
  Target,
  Truck,
  Users,
  X,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

/* ─── Landing Color Tokens ─────────────────────────────────────────────────── */

const landingColors = {
  ink: "var(--color-ink)",
  accent: "var(--color-brand-accent)",
  secondary: "var(--color-brand-secondary)",
  info: "var(--color-info)",
  success: "var(--color-success)",
  white: "var(--color-surface)",
  gray100: "var(--color-gray-100)",
  gray300: "var(--color-gray-300)",
  gray400: "var(--color-gray-400)",
} as const;

const softColor = (color: string, amount = 12) =>
  `color-mix(in srgb, ${color} ${amount}%, transparent)`;

const landingGridBackground = `linear-gradient(${landingColors.ink} 1px, transparent 1px), linear-gradient(90deg, ${landingColors.ink} 1px, transparent 1px)`;

// ─── Animated Counter ────────────────────────────────────────────────────────

function AnimatedCounter({
  end,
  suffix = "",
  duration = 2000,
}: {
  end: number;
  suffix?: string;
  duration?: number;
}) {
  const [count, setCount] = useState(0);
  const ref = useRef<HTMLSpanElement>(null);
  const hasAnimated = useRef(false);

  useEffect(() => {
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting && !hasAnimated.current) {
          hasAnimated.current = true;
          const startTime = performance.now();
          const animate = (now: number) => {
            const elapsed = now - startTime;
            const progress = Math.min(elapsed / duration, 1);
            const eased = 1 - (1 - progress) ** 3;
            setCount(Math.floor(eased * end));
            if (progress < 1) requestAnimationFrame(animate);
          };
          requestAnimationFrame(animate);
        }
      },
      { threshold: 0.3 },
    );
    if (ref.current) observer.observe(ref.current);
    return () => observer.disconnect();
  }, [end, duration]);

  return (
    <span ref={ref}>
      {count}
      {suffix}
    </span>
  );
}

// ─── Fade-in on Scroll ───────────────────────────────────────────────────────

function FadeIn({
  children,
  className = "",
  delay = 0,
}: {
  children: React.ReactNode;
  className?: string;
  delay?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) setVisible(true);
      },
      { threshold: 0.15 },
    );
    if (ref.current) observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);

  return (
    <div
      ref={ref}
      className={className}
      style={{
        opacity: visible ? 1 : 0,
        transform: visible ? "translateY(0)" : "translateY(32px)",
        transition: `opacity 0.7s ease ${delay}ms, transform 0.7s ease ${delay}ms`,
      }}
    >
      {children}
    </div>
  );
}

// ─── Product UI Mockup: Dashboard ────────────────────────────────────────────

function DashboardMockup() {
  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="flex items-center gap-2 px-4 py-2.5 border-b border-gray-100 bg-gray-50/80">
        <div className="flex gap-1.5">
          <div className="w-2 h-2 rounded-full bg-error" />
          <div className="w-2 h-2 rounded-full bg-warning" />
          <div className="w-2 h-2 rounded-full bg-success" />
        </div>
        <span className="text-[9px] text-gray-400 ml-2 font-mono">
          runsheet.app/dashboard
        </span>
      </div>
      <div className="p-4">
        <div className="grid grid-cols-3 gap-2 mb-3">
          {[
            {
              label: "Active Trucks",
              value: "47",
              color: landingColors.accent,
            },
            {
              label: "Fuel Efficiency",
              value: "94%",
              color: landingColors.secondary,
            },
            {
              label: "On-Time Rate",
              value: "98.2%",
              color: landingColors.info,
            },
          ].map((s) => (
            <div
              key={s.label}
              className="rounded-lg p-2.5 border border-gray-100"
            >
              <div
                className="w-5 h-5 rounded mb-1.5"
                style={{ backgroundColor: softColor(s.color) }}
              />
              <p
                className="text-sm font-bold"
                style={{ color: landingColors.ink }}
              >
                {s.value}
              </p>
              <p className="text-[9px] text-gray-400">{s.label}</p>
            </div>
          ))}
        </div>
        <div className="rounded-lg border border-gray-100 p-3 mb-3">
          <div className="flex justify-between mb-2">
            <span
              className="text-[10px] font-semibold"
              style={{ color: landingColors.ink }}
            >
              Fleet Activity
            </span>
            <span className="text-[9px] text-gray-400">Last 7 days</span>
          </div>
          <div className="flex items-end gap-1 h-16">
            {[40, 65, 45, 80, 55, 90, 70].map((h, i) => (
              <div
                key={i}
                className="flex-1 rounded-t"
                style={{
                  height: `${h}%`,
                  backgroundColor:
                    i === 5
                      ? landingColors.accent
                      : softColor(landingColors.accent, 20),
                }}
              />
            ))}
          </div>
        </div>
        <div className="space-y-1.5">
          {[
            {
              id: "TRK-042",
              route: "Houston → Dallas",
              status: "In Transit",
              color: landingColors.info,
            },
            {
              id: "TRK-018",
              route: "Chicago → Detroit",
              status: "Loading",
              color: landingColors.secondary,
            },
            {
              id: "TRK-091",
              route: "Atlanta → Charlotte",
              status: "Delivered",
              color: landingColors.success,
            },
          ].map((t) => (
            <div
              key={t.id}
              className="flex items-center justify-between py-1.5 px-2.5 rounded-md bg-gray-50/80"
            >
              <div className="flex items-center gap-2">
                <div
                  className="w-1.5 h-1.5 rounded-full"
                  style={{ backgroundColor: t.color }}
                />
                <span
                  className="text-[10px] font-medium"
                  style={{ color: landingColors.ink }}
                >
                  {t.id}
                </span>
                <span className="text-[9px] text-gray-400">{t.route}</span>
              </div>
              <span
                className="text-[9px] font-medium px-1.5 py-0.5 rounded-full"
                style={{ color: t.color, backgroundColor: softColor(t.color) }}
              >
                {t.status}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ─── Product UI Mockup: Scheduling Board ─────────────────────────────────────

function SchedulingMockup() {
  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="flex items-center gap-2 px-4 py-2.5 border-b border-gray-100 bg-gray-50/80">
        <div className="flex gap-1.5">
          <div className="w-2 h-2 rounded-full bg-error" />
          <div className="w-2 h-2 rounded-full bg-warning" />
          <div className="w-2 h-2 rounded-full bg-success" />
        </div>
        <span className="text-[9px] text-gray-400 ml-2 font-mono">
          runsheet.app/scheduling
        </span>
      </div>
      <div className="p-4">
        <div className="grid grid-cols-3 gap-2">
          {["Pending", "In Progress", "Completed"].map((col, ci) => (
            <div key={col}>
              <div className="flex items-center gap-1.5 mb-2">
                <div
                  className="w-2 h-2 rounded-full"
                  style={{
                    backgroundColor: [
                      landingColors.secondary,
                      landingColors.info,
                      landingColors.success,
                    ][ci],
                  }}
                />
                <span
                  className="text-[10px] font-semibold"
                  style={{ color: landingColors.ink }}
                >
                  {col}
                </span>
              </div>
              <div className="space-y-1.5">
                {[0, 1].map((j) => (
                  <div
                    key={j}
                    className="rounded-lg border border-gray-100 p-2.5"
                  >
                    <div className="flex items-center gap-1.5 mb-1">
                      <div className="w-4 h-4 rounded bg-gray-100" />
                      <span
                        className="text-[9px] font-medium"
                        style={{ color: landingColors.ink }}
                      >
                        JOB-{ci * 2 + j + 1}
                        {String(ci * 2 + j + 1).padStart(2, "0")}
                      </span>
                    </div>
                    <div className="h-1 rounded-full bg-gray-100 mb-1">
                      <div
                        className="h-full rounded-full"
                        style={{
                          width: `${[30, 60, 45, 80, 100, 100][ci * 2 + j]}%`,
                          backgroundColor: [
                            landingColors.secondary,
                            landingColors.info,
                            landingColors.success,
                          ][ci],
                        }}
                      />
                    </div>
                    <p className="text-[8px] text-gray-400">
                      {
                        [
                          "Fuel delivery",
                          "Route pickup",
                          "Depot transfer",
                          "Express haul",
                          "Bulk cargo",
                          "Last mile",
                        ][ci * 2 + j]
                      }
                    </p>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ─── Product UI Mockup: Fuel Distribution Map ────────────────────────────────

function FuelMapMockup() {
  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="flex items-center gap-2 px-4 py-2.5 border-b border-gray-100 bg-gray-50/80">
        <div className="flex gap-1.5">
          <div className="w-2 h-2 rounded-full bg-error" />
          <div className="w-2 h-2 rounded-full bg-warning" />
          <div className="w-2 h-2 rounded-full bg-success" />
        </div>
        <span className="text-[9px] text-gray-400 ml-2 font-mono">
          runsheet.app/fuel-map
        </span>
      </div>
      <div className="p-4">
        <div className="relative rounded-lg bg-gradient-to-br from-info-light to-success-light h-40 mb-3 overflow-hidden">
          {/* Map grid lines */}
          <div
            className="absolute inset-0 opacity-10"
            style={{
              backgroundImage: landingGridBackground,
              backgroundSize: "20px 20px",
            }}
          />
          {/* Station markers */}
          {[
            {
              top: "20%",
              left: "25%",
              label: "Depot A",
              color: landingColors.accent,
            },
            {
              top: "50%",
              left: "60%",
              label: "Station B",
              color: landingColors.secondary,
            },
            {
              top: "30%",
              left: "75%",
              label: "Station C",
              color: landingColors.info,
            },
            {
              top: "70%",
              left: "35%",
              label: "Depot D",
              color: landingColors.success,
            },
          ].map((m) => (
            <div
              key={m.label}
              className="absolute flex flex-col items-center"
              style={{ top: m.top, left: m.left }}
            >
              <div
                className="w-3 h-3 rounded-full border-2 border-white shadow-sm"
                style={{ backgroundColor: m.color }}
              />
              <span
                className="text-[7px] font-medium mt-0.5 bg-white/80 px-1 rounded"
                style={{ color: m.color }}
              >
                {m.label}
              </span>
            </div>
          ))}
          {/* Route lines */}
          <svg className="absolute inset-0 w-full h-full" viewBox="0 0 200 160">
            <path
              d="M50 32 L120 80"
              stroke={landingColors.secondary}
              strokeWidth="1"
              strokeDasharray="4 2"
              fill="none"
              opacity="0.5"
            />
            <path
              d="M120 80 L150 48"
              stroke={landingColors.info}
              strokeWidth="1"
              strokeDasharray="4 2"
              fill="none"
              opacity="0.5"
            />
            <path
              d="M50 32 L70 112"
              stroke={landingColors.success}
              strokeWidth="1"
              strokeDasharray="4 2"
              fill="none"
              opacity="0.5"
            />
          </svg>
        </div>
        <div className="grid grid-cols-2 gap-2">
          {[
            {
              label: "Total Volume",
              value: "45,000L",
              color: landingColors.accent,
            },
            {
              label: "Stations Active",
              value: "12/14",
              color: landingColors.info,
            },
          ].map((s) => (
            <div
              key={s.label}
              className="rounded-lg border border-gray-100 p-2 text-center"
            >
              <p className="text-xs font-bold" style={{ color: s.color }}>
                {s.value}
              </p>
              <p className="text-[8px] text-gray-400">{s.label}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ─── Product UI Mockup: Analytics Dashboard ──────────────────────────────────

function AnalyticsMockup() {
  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="flex items-center gap-2 px-4 py-2.5 border-b border-gray-100 bg-gray-50/80">
        <div className="flex gap-1.5">
          <div className="w-2 h-2 rounded-full bg-error" />
          <div className="w-2 h-2 rounded-full bg-warning" />
          <div className="w-2 h-2 rounded-full bg-success" />
        </div>
        <span className="text-[9px] text-gray-400 ml-2 font-mono">
          runsheet.app/analytics
        </span>
      </div>
      <div className="p-4">
        <div className="grid grid-cols-2 gap-2 mb-3">
          {[
            {
              label: "Revenue",
              value: "$12.4M",
              change: "+18%",
              color: landingColors.success,
            },
            {
              label: "Cost/mi",
              value: "$42.30",
              change: "-12%",
              color: landingColors.info,
            },
            {
              label: "Utilization",
              value: "87%",
              change: "+5%",
              color: landingColors.accent,
            },
            {
              label: "SLA Score",
              value: "96.8%",
              change: "+2.1%",
              color: landingColors.secondary,
            },
          ].map((m) => (
            <div
              key={m.label}
              className="rounded-lg border border-gray-100 p-2.5"
            >
              <p className="text-[9px] text-gray-400 mb-0.5">{m.label}</p>
              <div className="flex items-baseline gap-1.5">
                <span
                  className="text-sm font-bold"
                  style={{ color: landingColors.ink }}
                >
                  {m.value}
                </span>
                <span
                  className="text-[8px] font-medium"
                  style={{ color: m.color }}
                >
                  {m.change}
                </span>
              </div>
            </div>
          ))}
        </div>
        {/* Trend lines */}
        <div className="rounded-lg border border-gray-100 p-3">
          <p
            className="text-[10px] font-semibold mb-2"
            style={{ color: landingColors.ink }}
          >
            Monthly Trend
          </p>
          <svg viewBox="0 0 200 60" className="w-full h-12">
            <path
              d="M0 50 L30 40 L60 45 L90 30 L120 25 L150 15 L180 20 L200 10"
              stroke={landingColors.accent}
              strokeWidth="2"
              fill="none"
            />
            <path
              d="M0 55 L30 50 L60 48 L90 42 L120 38 L150 35 L180 30 L200 28"
              stroke={landingColors.info}
              strokeWidth="1.5"
              fill="none"
              opacity="0.5"
            />
          </svg>
          <div className="flex gap-4 mt-1">
            <div className="flex items-center gap-1">
              <div
                className="w-2 h-0.5 rounded"
                style={{ backgroundColor: landingColors.accent }}
              />
              <span className="text-[8px] text-gray-400">Revenue</span>
            </div>
            <div className="flex items-center gap-1">
              <div
                className="w-2 h-0.5 rounded"
                style={{ backgroundColor: landingColors.info }}
              />
              <span className="text-[8px] text-gray-400">Efficiency</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Marquee CSS (injected via style tag) ────────────────────────────────────

function MarqueeStyles() {
  return (
    <style>{`
      @keyframes marquee-left {
        0% { transform: translateX(0); }
        100% { transform: translateX(-50%); }
      }
      @keyframes marquee-right {
        0% { transform: translateX(-50%); }
        100% { transform: translateX(0); }
      }
      .animate-marquee-left {
        animation: marquee-left 30s linear infinite;
      }
      .animate-marquee-right {
        animation: marquee-right 30s linear infinite;
      }
    `}</style>
  );
}

// ─── Main Landing Page ───────────────────────────────────────────────────────

export default function LandingPage() {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const [heroSlide, setHeroSlide] = useState(0);
  const [activeTab, setActiveTab] = useState(0);

  // Scroll listener for sticky nav
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 20);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  // Hero carousel auto-advance
  useEffect(() => {
    const timer = setInterval(() => {
      setHeroSlide((prev) => (prev + 1) % 3);
    }, 4000);
    return () => clearInterval(timer);
  }, []);

  const heroSlides = [
    { component: <DashboardMockup />, label: "Fleet Dashboard" },
    { component: <SchedulingMockup />, label: "Smart Scheduling" },
    { component: <FuelMapMockup />, label: "Fuel Distribution" },
  ];

  const workflowTabs = [
    {
      title: "Explore insights",
      icon: Eye,
      desc: "Surface hidden patterns in fleet performance, fuel consumption, and delivery timelines with AI-powered analytics.",
      mockup: <AnalyticsMockup />,
    },
    {
      title: "Build schedules",
      icon: Clock,
      desc: "Create optimized delivery schedules with drag-and-drop job boards, automated driver assignment, and capacity planning.",
      mockup: <SchedulingMockup />,
    },
    {
      title: "Optimize routes",
      icon: Route,
      desc: "Let AI agents find the fastest, most fuel-efficient routes — and replan in real time when disruptions hit.",
      mockup: <FuelMapMockup />,
    },
    {
      title: "Generate reports",
      icon: BarChart3,
      desc: "Auto-generate compliance reports, cost breakdowns, and performance summaries for stakeholders.",
      mockup: <AnalyticsMockup />,
    },
  ];

  const navLinks = ["Platform", "Solutions", "Customers", "Pricing"];

  return (
    <div className="min-h-screen bg-white">
      <MarqueeStyles />

      {/* ═══════════════════════════════════════════════════════════════════
          1. STICKY NAV
      ═══════════════════════════════════════════════════════════════════ */}
      <nav
        className={`fixed top-0 left-0 right-0 z-50 transition-all duration-300 ${
          scrolled
            ? "bg-white/95 backdrop-blur-md shadow-sm border-b border-gray-100"
            : "bg-transparent"
        }`}
      >
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <div className="flex items-center justify-between h-16 lg:h-20">
            {/* Logo */}
            <Link href="/" className="flex items-center gap-2.5">
              <div
                className="w-9 h-9 rounded-lg flex items-center justify-center"
                style={{ backgroundColor: landingColors.accent }}
              >
                <Truck className="w-5 h-5 text-white" />
              </div>
              <span
                className="text-xl font-bold tracking-tight"
                style={{ color: landingColors.ink }}
              >
                Runsheet
              </span>
            </Link>

            {/* Desktop Nav Links */}
            <div className="hidden lg:flex items-center gap-8">
              {navLinks.map((item) => (
                <a
                  key={item}
                  href={`#${item.toLowerCase()}`}
                  className="text-sm font-medium transition-colors hover:opacity-80"
                  style={{ color: landingColors.ink }}
                >
                  {item}
                </a>
              ))}
            </div>

            {/* CTA Buttons */}
            <div className="hidden lg:flex items-center gap-3">
              <Link
                href="/signin"
                className="text-sm font-medium px-4 py-2 rounded-lg transition-all hover:bg-gray-50"
                style={{ color: landingColors.ink }}
              >
                Sign In
              </Link>
              <Link
                href="/demo"
                className="text-sm font-semibold px-6 py-2.5 rounded-full text-white transition-all hover:opacity-90 shadow-sm"
                style={{ backgroundColor: landingColors.accent }}
              >
                Get a Demo
              </Link>
            </div>

            {/* Mobile Menu Toggle */}
            <button
              onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
              className="lg:hidden p-2 rounded-lg hover:bg-gray-100"
              aria-label="Toggle menu"
            >
              {mobileMenuOpen ? (
                <X className="w-5 h-5" style={{ color: landingColors.ink }} />
              ) : (
                <Menu
                  className="w-5 h-5"
                  style={{ color: landingColors.ink }}
                />
              )}
            </button>
          </div>
        </div>

        {/* Mobile Menu */}
        {mobileMenuOpen && (
          <div className="lg:hidden bg-white border-t border-gray-100 shadow-lg">
            <div className="px-6 py-4 space-y-3">
              {navLinks.map((item) => (
                <a
                  key={item}
                  href={`#${item.toLowerCase()}`}
                  onClick={() => setMobileMenuOpen(false)}
                  className="block text-sm font-medium py-2"
                  style={{ color: landingColors.ink }}
                >
                  {item}
                </a>
              ))}
              <div className="pt-3 border-t border-gray-100 flex flex-col gap-2">
                <Link
                  href="/signin"
                  className="text-sm font-medium py-2.5 text-center rounded-lg border border-gray-200"
                  style={{ color: landingColors.ink }}
                >
                  Sign In
                </Link>
                <Link
                  href="/demo"
                  className="text-sm font-semibold py-2.5 text-center rounded-full text-white"
                  style={{ backgroundColor: landingColors.accent }}
                >
                  Get a Demo
                </Link>
              </div>
            </div>
          </div>
        )}
      </nav>

      {/* ═══════════════════════════════════════════════════════════════════
          2. HERO — Centered headline + full-width product carousel below
      ═══════════════════════════════════════════════════════════════════ */}
      <section className="pt-28 lg:pt-36 pb-16 lg:pb-24">
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          {/* Centered copy block */}
          <div className="text-center max-w-3xl mx-auto mb-16">
            <FadeIn>
              <h1
                className="text-5xl sm:text-6xl lg:text-7xl font-bold leading-[1.05] tracking-tight mb-6"
                style={{ color: landingColors.ink }}
              >
                Fleet operations look different here
              </h1>
            </FadeIn>

            <FadeIn delay={100}>
              <h2 className="text-lg lg:text-xl text-gray-500 leading-relaxed mb-10 max-w-2xl mx-auto font-normal">
                Go from blind spots to full visibility in days — powered by all
                your fleet data, operational knowledge, and AI.
              </h2>
            </FadeIn>

            <FadeIn delay={200}>
              <Link
                href="/demo"
                className="inline-flex items-center justify-center gap-2 px-8 py-3.5 rounded-full text-white font-semibold text-sm transition-all hover:opacity-90 shadow-lg shadow-emerald-500/20"
                style={{ backgroundColor: landingColors.accent }}
              >
                Get a Demo
                <ArrowRight className="w-4 h-4" />
              </Link>
            </FadeIn>
          </div>

          {/* Full-width product carousel below */}
          <FadeIn delay={300}>
            <div className="relative max-w-5xl mx-auto">
              <div className="relative">
                {heroSlides.map((slide, i) => (
                  <div
                    key={slide.label}
                    className="transition-all duration-700 ease-in-out"
                    style={{
                      opacity: heroSlide === i ? 1 : 0,
                      transform: heroSlide === i ? "scale(1)" : "scale(0.97)",
                      position: heroSlide === i ? "relative" : "absolute",
                      top: 0,
                      left: 0,
                      right: 0,
                      pointerEvents: heroSlide === i ? "auto" : "none",
                    }}
                  >
                    {slide.component}
                  </div>
                ))}
              </div>

              {/* Carousel navigation */}
              <div className="flex items-center justify-center gap-3 mt-6">
                {heroSlides.map((slide, i) => (
                  <button
                    key={slide.label}
                    onClick={() => setHeroSlide(i)}
                    className="group flex items-center gap-2 transition-all duration-300"
                    aria-label={`Show ${slide.label}`}
                  >
                    <div
                      className={`transition-all duration-300 rounded-full ${
                        heroSlide === i ? "w-8 h-2" : "w-2 h-2"
                      }`}
                      style={{
                        backgroundColor:
                          heroSlide === i
                            ? landingColors.accent
                            : landingColors.gray300,
                      }}
                    />
                  </button>
                ))}
              </div>
            </div>
          </FadeIn>
        </div>
      </section>

      {/* ═══════════════════════════════════════════════════════════════════
          3. LOGO PROOF STRIP
      ═══════════════════════════════════════════════════════════════════ */}
      <section className="py-12 border-y border-gray-100 bg-gray-50/50">
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <p className="text-center text-xs font-medium text-gray-400 uppercase tracking-widest mb-8">
            Trusted by fuel distributors across America
          </p>
          <div className="flex flex-wrap items-center justify-center gap-x-12 gap-y-6 mb-6">
            {[
              "PetroCorp",
              "FuelNet",
              "SwiftFuel",
              "TankPro",
              "FleetEnergy",
              "RouteMax",
              "LogiPrime",
              "TransHaul",
            ].map((name) => (
              <span
                key={name}
                className="text-lg font-bold tracking-tight opacity-20"
                style={{ color: landingColors.ink }}
              >
                {name}
              </span>
            ))}
          </div>
          <p className="text-center">
            <a
              href="#customers"
              className="text-sm font-medium inline-flex items-center gap-1 transition-colors hover:opacity-80"
              style={{ color: landingColors.info }}
            >
              Read case studies <ArrowRight className="w-3.5 h-3.5" />
            </a>
          </p>
        </div>
      </section>

      {/* ═══════════════════════════════════════════════════════════════════
          4. TWO-PRODUCT SHOWCASE
      ═══════════════════════════════════════════════════════════════════ */}
      <section id="platform" className="py-24 lg:py-32">
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <FadeIn>
            <div className="text-center max-w-2xl mx-auto mb-16">
              <p
                className="text-sm font-semibold uppercase tracking-widest mb-3"
                style={{ color: landingColors.secondary }}
              >
                The Platform
              </p>
              <h2
                className="text-3xl lg:text-4xl font-bold tracking-tight mb-4"
                style={{ color: landingColors.ink }}
              >
                Two products. One outcome: operational excellence.
              </h2>
            </div>
          </FadeIn>

          <div className="grid md:grid-cols-2 gap-6">
            {/* Fleet Intelligence Platform */}
            <FadeIn>
              <div className="group bg-white rounded-2xl border border-gray-100 hover:shadow-lg transition-all duration-300 overflow-hidden h-full">
                <div className="p-8">
                  <div
                    className="w-12 h-12 rounded-xl flex items-center justify-center mb-5"
                    style={{ backgroundColor: softColor(landingColors.accent) }}
                  >
                    <Truck
                      className="w-6 h-6"
                      style={{ color: landingColors.accent }}
                    />
                  </div>
                  <h3
                    className="text-xl font-bold mb-2"
                    style={{ color: landingColors.ink }}
                  >
                    Fleet Intelligence Platform
                  </h3>
                  <p className="text-gray-500 text-sm mb-4">
                    Real-time visibility, scheduling, and fuel management for
                    every vehicle in your network.
                  </p>
                  <a
                    href="#"
                    className="text-sm font-medium inline-flex items-center gap-1 transition-colors hover:opacity-80"
                    style={{ color: landingColors.accent }}
                  >
                    Learn more <ArrowRight className="w-3.5 h-3.5" />
                  </a>
                </div>
                <div className="px-6 pb-6">
                  <DashboardMockup />
                </div>
              </div>
            </FadeIn>

            {/* AI Operations Suite */}
            <FadeIn delay={100}>
              <div className="group bg-white rounded-2xl border border-gray-100 hover:shadow-lg transition-all duration-300 overflow-hidden h-full">
                <div className="p-8">
                  <div
                    className="w-12 h-12 rounded-xl flex items-center justify-center mb-5"
                    style={{
                      backgroundColor: softColor(landingColors.secondary),
                    }}
                  >
                    <Bot
                      className="w-6 h-6"
                      style={{ color: landingColors.secondary }}
                    />
                  </div>
                  <h3
                    className="text-xl font-bold mb-2"
                    style={{ color: landingColors.ink }}
                  >
                    AI Operations Suite
                  </h3>
                  <p className="text-gray-500 text-sm mb-4">
                    Autonomous agents that detect disruptions, replan routes,
                    and optimize fuel distribution.
                  </p>
                  <a
                    href="#"
                    className="text-sm font-medium inline-flex items-center gap-1 transition-colors hover:opacity-80"
                    style={{ color: landingColors.secondary }}
                  >
                    Learn more <ArrowRight className="w-3.5 h-3.5" />
                  </a>
                </div>
                <div className="px-6 pb-6">
                  <AnalyticsMockup />
                </div>
              </div>
            </FadeIn>
          </div>
        </div>
      </section>

      {/* ═══════════════════════════════════════════════════════════════════
          5. CAPABILITY CARDS — 2×2 grid
      ═══════════════════════════════════════════════════════════════════ */}
      <section id="solutions" className="py-24 lg:py-32 bg-gray-50/50">
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <FadeIn>
            <div className="text-center max-w-2xl mx-auto mb-16">
              <p
                className="text-sm font-semibold uppercase tracking-widest mb-3"
                style={{ color: landingColors.info }}
              >
                Capabilities
              </p>
              <h2
                className="text-3xl lg:text-4xl font-bold tracking-tight mb-4"
                style={{ color: landingColors.ink }}
              >
                Built for every operational challenge
              </h2>
            </div>
          </FadeIn>

          <div className="grid md:grid-cols-2 gap-6">
            {[
              {
                title: "Complete fleet visibility",
                desc: "Track every vehicle, driver, and delivery in real time across your entire network.",
                color: landingColors.accent,
                icon: Globe,
                mockup: (
                  <div className="rounded-lg border border-gray-100 p-3 bg-white mt-4">
                    <div className="flex items-center gap-2 mb-2">
                      <div className="w-2 h-2 rounded-full bg-success animate-pulse" />
                      <span
                        className="text-[9px] font-medium"
                        style={{ color: landingColors.ink }}
                      >
                        47 vehicles online
                      </span>
                    </div>
                    <div className="grid grid-cols-4 gap-1">
                      {Array.from({ length: 8 }).map((_, i) => (
                        <div
                          key={i}
                          className="h-6 rounded bg-gray-50 flex items-center justify-center"
                        >
                          <Truck
                            className="w-3 h-3"
                            style={{
                              color:
                                i < 6
                                  ? landingColors.success
                                  : landingColors.secondary,
                            }}
                          />
                        </div>
                      ))}
                    </div>
                  </div>
                ),
              },
              {
                title: "Proactive AI insights",
                desc: "AI agents detect delays, predict failures, and surface optimization opportunities before they become problems.",
                color: landingColors.secondary,
                icon: Sparkles,
                mockup: (
                  <div className="rounded-lg border border-gray-100 p-3 bg-white mt-4">
                    <div className="space-y-1.5">
                      {[
                        {
                          text: "Route TRK-042 delay predicted — rerouting",
                          type: "warning",
                        },
                        {
                          text: "Fuel savings opportunity: $240K/week",
                          type: "success",
                        },
                        {
                          text: "Driver fatigue alert: shift limit in 45min",
                          type: "info",
                        },
                      ].map((alert) => (
                        <div
                          key={alert.text}
                          className="flex items-center gap-2 py-1.5 px-2 rounded-md bg-gray-50/80"
                        >
                          <div
                            className="w-1.5 h-1.5 rounded-full flex-shrink-0"
                            style={{
                              backgroundColor:
                                alert.type === "warning"
                                  ? landingColors.secondary
                                  : alert.type === "success"
                                    ? landingColors.success
                                    : landingColors.info,
                            }}
                          />
                          <span
                            className="text-[9px]"
                            style={{ color: landingColors.ink }}
                          >
                            {alert.text}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                ),
              },
              {
                title: "Optimized fuel distribution",
                desc: "AI-powered compartment loading and route optimization that cuts fuel costs by up to 35%.",
                color: landingColors.info,
                icon: Droplets,
                mockup: (
                  <div className="rounded-lg border border-gray-100 p-3 bg-white mt-4">
                    <div className="flex items-center justify-between mb-2">
                      <span
                        className="text-[9px] font-medium"
                        style={{ color: landingColors.ink }}
                      >
                        Compartment Loading
                      </span>
                      <span
                        className="text-[9px] font-medium"
                        style={{ color: landingColors.success }}
                      >
                        98% utilized
                      </span>
                    </div>
                    <div className="flex gap-0.5 h-8">
                      {[
                        {
                          w: "30%",
                          color: landingColors.accent,
                          label: "Diesel",
                        },
                        {
                          w: "25%",
                          color: landingColors.info,
                          label: "Gasoline",
                        },
                        {
                          w: "20%",
                          color: landingColors.secondary,
                          label: "Heating Oil",
                        },
                        {
                          w: "23%",
                          color: landingColors.success,
                          label: "Propane",
                        },
                      ].map((c) => (
                        <div
                          key={c.label}
                          className="rounded flex items-center justify-center"
                          style={{
                            width: c.w,
                            backgroundColor: softColor(c.color, 16),
                          }}
                        >
                          <span
                            className="text-[7px] font-bold"
                            style={{ color: c.color }}
                          >
                            {c.label}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                ),
              },
              {
                title: "Real-time scheduling",
                desc: "Drag-and-drop job boards with automated assignment, capacity planning, and SLA tracking.",
                color: landingColors.ink,
                icon: Clock,
                mockup: (
                  <div className="rounded-lg border border-gray-100 p-3 bg-white mt-4">
                    <div className="space-y-1">
                      {[
                        {
                          job: "JOB-101",
                          driver: "Mike J.",
                          time: "08:00",
                          status: "Assigned",
                        },
                        {
                          job: "JOB-102",
                          driver: "Sarah W.",
                          time: "09:30",
                          status: "En Route",
                        },
                        {
                          job: "JOB-103",
                          driver: "James R.",
                          time: "11:00",
                          status: "Pending",
                        },
                      ].map((j) => (
                        <div
                          key={j.job}
                          className="flex items-center justify-between py-1.5 px-2 rounded-md bg-gray-50/80"
                        >
                          <div className="flex items-center gap-2">
                            <span
                              className="text-[9px] font-medium"
                              style={{ color: landingColors.ink }}
                            >
                              {j.job}
                            </span>
                            <span className="text-[8px] text-gray-400">
                              {j.driver}
                            </span>
                          </div>
                          <span className="text-[8px] text-gray-400">
                            {j.time}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                ),
              },
            ].map((cap, i) => (
              <FadeIn key={cap.title} delay={i * 80}>
                <div className="bg-white rounded-2xl border border-gray-100 p-7 hover:shadow-lg transition-all duration-300 h-full">
                  <div className="flex items-start gap-4">
                    <div
                      className="w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0"
                      style={{ backgroundColor: softColor(cap.color, 10) }}
                    >
                      <cap.icon
                        className="w-5 h-5"
                        style={{ color: cap.color }}
                      />
                    </div>
                    <div>
                      <h3
                        className="text-base font-semibold mb-1"
                        style={{ color: landingColors.ink }}
                      >
                        {cap.title}
                      </h3>
                      <p className="text-sm text-gray-500">{cap.desc}</p>
                    </div>
                  </div>
                  {cap.mockup}
                </div>
              </FadeIn>
            ))}
          </div>
        </div>
      </section>

      {/* ═══════════════════════════════════════════════════════════════════
          6. TABBED WORKFLOW SECTION
      ═══════════════════════════════════════════════════════════════════ */}
      <section className="py-24 lg:py-32">
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <FadeIn>
            <div className="text-center max-w-3xl mx-auto mb-16">
              <h2
                className="text-3xl lg:text-4xl font-bold tracking-tight mb-4"
                style={{ color: landingColors.ink }}
              >
                Plan, track, optimize, and report — faster and more autonomously
              </h2>
              <p className="text-gray-500 text-lg">
                A single workflow that covers your entire operational cycle.
              </p>
            </div>
          </FadeIn>

          <FadeIn delay={100}>
            <div className="grid lg:grid-cols-5 gap-8 items-start">
              {/* Left — Tabs */}
              <div className="lg:col-span-2 space-y-2">
                {workflowTabs.map((tab, i) => (
                  <button
                    key={tab.title}
                    onClick={() => setActiveTab(i)}
                    className={`w-full text-left p-5 rounded-xl transition-all duration-300 border ${
                      activeTab === i
                        ? "bg-white shadow-md border-gray-200"
                        : "bg-transparent border-transparent hover:bg-gray-50"
                    }`}
                  >
                    <div className="flex items-center gap-3 mb-2">
                      <div
                        className="w-8 h-8 rounded-lg flex items-center justify-center transition-colors"
                        style={{
                          backgroundColor:
                            activeTab === i
                              ? landingColors.accent
                              : landingColors.gray100,
                        }}
                      >
                        <tab.icon
                          className="w-4 h-4"
                          style={{
                            color:
                              activeTab === i
                                ? landingColors.white
                                : landingColors.gray400,
                          }}
                        />
                      </div>
                      <span
                        className="text-sm font-semibold"
                        style={{
                          color:
                            activeTab === i
                              ? landingColors.ink
                              : landingColors.gray400,
                        }}
                      >
                        {tab.title}
                      </span>
                    </div>
                    {activeTab === i && (
                      <p className="text-sm text-gray-500 pl-11 leading-relaxed">
                        {tab.desc}
                      </p>
                    )}
                  </button>
                ))}
              </div>

              {/* Right — Mockup */}
              <div className="lg:col-span-3">
                <div className="relative">
                  {workflowTabs.map((tab, i) => (
                    <div
                      key={tab.title}
                      className="transition-all duration-500"
                      style={{
                        opacity: activeTab === i ? 1 : 0,
                        position: activeTab === i ? "relative" : "absolute",
                        top: 0,
                        left: 0,
                        right: 0,
                        pointerEvents: activeTab === i ? "auto" : "none",
                      }}
                    >
                      {tab.mockup}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </FadeIn>
        </div>
      </section>

      {/* ═══════════════════════════════════════════════════════════════════
          7. ANALYST / AWARD CALLOUT
      ═══════════════════════════════════════════════════════════════════ */}
      <section
        className="py-24 lg:py-32"
        style={{ backgroundColor: landingColors.ink }}
      >
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <FadeIn>
            <div className="flex flex-col lg:flex-row items-center gap-12 lg:gap-20">
              {/* Award Graphic */}
              <div className="flex-shrink-0">
                <div className="w-40 h-40 lg:w-52 lg:h-52 rounded-2xl border border-white/10 bg-white/5 flex flex-col items-center justify-center">
                  <div
                    className="w-16 h-16 rounded-full flex items-center justify-center mb-3"
                    style={{ backgroundColor: landingColors.accent }}
                  >
                    <Target className="w-8 h-8 text-white" />
                  </div>
                  <p className="text-[10px] font-bold text-white/60 uppercase tracking-widest">
                    2024 Leader
                  </p>
                  <p className="text-[9px] text-white/40">
                    Fuel Distribution Tech
                  </p>
                </div>
              </div>

              {/* Copy */}
              <div>
                <p
                  className="text-sm font-semibold uppercase tracking-widest mb-3"
                  style={{ color: landingColors.accent }}
                >
                  Recognition
                </p>
                <h2 className="text-3xl lg:text-4xl font-bold tracking-tight text-white mb-4">
                  Recognized as a leader in fuel distribution technology
                </h2>
                <p className="text-white/50 text-lg leading-relaxed max-w-xl">
                  Runsheet was named a top logistics platform in the 2024 Fuel
                  Distribution Technology Awards for operational innovation,
                  AI-driven fleet management, and measurable customer impact.
                </p>
              </div>
            </div>
          </FadeIn>
        </div>
      </section>

      {/* ═══════════════════════════════════════════════════════════════════
          8. CASE STUDIES CAROUSEL + METRIC CALLOUTS
      ═══════════════════════════════════════════════════════════════════ */}
      <section id="customers" className="py-24 lg:py-32 bg-gray-50/50">
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <FadeIn>
            <div className="text-center max-w-2xl mx-auto mb-12">
              <p
                className="text-sm font-semibold uppercase tracking-widest mb-3"
                style={{ color: landingColors.accent }}
              >
                Customer Stories
              </p>
              <h2
                className="text-3xl lg:text-4xl font-bold tracking-tight mb-4"
                style={{ color: landingColors.ink }}
              >
                Proven results across industries
              </h2>
            </div>
          </FadeIn>

          {/* Horizontal scrollable cards */}
          <FadeIn delay={100}>
            <div className="flex gap-6 overflow-x-auto pb-4 snap-x snap-mandatory scrollbar-hide -mx-6 px-6">
              {[
                {
                  company: "PetroCorp",
                  industry: "Fuel Distribution",
                  title: "How PetroCorp cut fuel costs by 45% in one quarter",
                  color: landingColors.accent,
                },
                {
                  company: "SwiftFuel",
                  industry: "Last-Mile Delivery",
                  title:
                    "SwiftFuel hit 98% on-time delivery with AI scheduling",
                  color: landingColors.secondary,
                },
                {
                  company: "RouteMax",
                  industry: "Cross-Country Logistics",
                  title: "RouteMax replans 3x faster with autonomous agents",
                  color: landingColors.info,
                },
                {
                  company: "TankPro",
                  industry: "Fleet Management",
                  title: "TankPro gained full visibility across 200+ vehicles",
                  color: landingColors.success,
                },
                {
                  company: "FuelNet",
                  industry: "Fuel Distribution",
                  title:
                    "FuelNet optimized compartment loading for 52% less waste",
                  color: landingColors.accent,
                },
              ].map((study) => (
                <div
                  key={study.company}
                  className="flex-shrink-0 w-80 snap-start bg-white rounded-2xl border border-gray-100 p-7 hover:shadow-md transition-shadow"
                >
                  <div className="flex items-center gap-3 mb-5">
                    <div
                      className="w-10 h-10 rounded-full flex items-center justify-center text-white text-xs font-bold"
                      style={{ backgroundColor: study.color }}
                    >
                      {study.company.slice(0, 2)}
                    </div>
                    <div>
                      <p
                        className="text-sm font-semibold"
                        style={{ color: landingColors.ink }}
                      >
                        {study.company}
                      </p>
                      <p className="text-xs text-gray-400">{study.industry}</p>
                    </div>
                  </div>
                  <h3
                    className="text-sm font-semibold leading-snug mb-5"
                    style={{ color: landingColors.ink }}
                  >
                    {study.title}
                  </h3>
                  <a
                    href="#"
                    className="text-sm font-medium inline-flex items-center gap-1 transition-colors hover:opacity-80"
                    style={{ color: study.color }}
                  >
                    Read story <ArrowRight className="w-3.5 h-3.5" />
                  </a>
                </div>
              ))}
            </div>
          </FadeIn>

          {/* Metric callouts */}
          <FadeIn delay={200}>
            <div className="grid sm:grid-cols-3 gap-6 mt-16">
              {[
                {
                  value: 500,
                  suffix: "+",
                  label: "Fleets managed",
                  color: landingColors.accent,
                },
                {
                  value: 200,
                  suffix: "%",
                  label: "Boost in efficiency",
                  color: landingColors.secondary,
                },
                {
                  value: 52,
                  suffix: "%",
                  label: "Reduction in fuel waste",
                  color: landingColors.info,
                },
              ].map((m) => (
                <div key={m.label} className="text-center">
                  <p
                    className="text-4xl lg:text-5xl font-bold mb-1"
                    style={{ color: m.color }}
                  >
                    <AnimatedCounter end={m.value} suffix={m.suffix} />
                  </p>
                  <p className="text-sm text-gray-500">{m.label}</p>
                </div>
              ))}
            </div>
          </FadeIn>
        </div>
      </section>

      {/* ═══════════════════════════════════════════════════════════════════
          9. SECURITY SECTION
      ═══════════════════════════════════════════════════════════════════ */}
      <section className="py-24 lg:py-32">
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <FadeIn>
            <div className="text-center max-w-2xl mx-auto mb-12">
              <p
                className="text-sm font-semibold uppercase tracking-widest mb-3"
                style={{ color: landingColors.info }}
              >
                Security
              </p>
              <h2
                className="text-3xl lg:text-4xl font-bold tracking-tight mb-4"
                style={{ color: landingColors.ink }}
              >
                Enterprise-grade security, by default
              </h2>
              <p className="text-gray-500 text-lg">
                Your fleet data is protected by the same standards trusted by
                Fortune 500 logistics companies.
              </p>
            </div>
          </FadeIn>

          <FadeIn delay={100}>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4">
              {[
                {
                  badge: "SOC 2 Type II",
                  desc: "Audited controls",
                  icon: Shield,
                },
                { badge: "GDPR", desc: "Data privacy by design", icon: Lock },
                {
                  badge: "ISO 27001",
                  desc: "Information security",
                  icon: Shield,
                },
                {
                  badge: "HIPAA",
                  desc: "Health data compliance",
                  icon: CheckCircle,
                },
                { badge: "SSO / RBAC", desc: "Access management", icon: Users },
              ].map((item) => (
                <div
                  key={item.badge}
                  className="bg-white rounded-2xl border border-gray-100 p-6 text-center hover:shadow-lg transition-all"
                >
                  <div
                    className="w-12 h-12 rounded-xl flex items-center justify-center mx-auto mb-3"
                    style={{ backgroundColor: softColor(landingColors.info) }}
                  >
                    <item.icon
                      className="w-5 h-5"
                      style={{ color: landingColors.info }}
                    />
                  </div>
                  <p
                    className="text-sm font-semibold mb-1"
                    style={{ color: landingColors.ink }}
                  >
                    {item.badge}
                  </p>
                  <p className="text-xs text-gray-400">{item.desc}</p>
                </div>
              ))}
            </div>
          </FadeIn>
        </div>
      </section>

      {/* ═══════════════════════════════════════════════════════════════════
          10. AWARDS / RECOGNITION GRID — Marquee
      ═══════════════════════════════════════════════════════════════════ */}
      <section className="py-16 bg-gray-50/50 overflow-hidden">
        <div className="max-w-7xl mx-auto px-6 lg:px-8 mb-8">
          <p className="text-center text-xs font-medium text-gray-400 uppercase tracking-widest">
            Awards &amp; Recognition
          </p>
        </div>
        <div className="relative">
          <div className="flex animate-marquee-left whitespace-nowrap">
            {[...Array(2)].map((_, setIdx) => (
              <div key={setIdx} className="flex gap-6 px-3">
                {[
                  "Fuel Tech Awards 2024",
                  "Best Fleet Platform",
                  "AI Innovation Award",
                  "Logistics Leader",
                  "Top 50 Startups",
                  "Enterprise Ready",
                  "ISO Certified",
                  "SOC 2 Verified",
                  "GDPR Compliant",
                  "Best UX Design",
                ].map((award) => (
                  <div
                    key={`${setIdx}-${award}`}
                    className="inline-flex items-center gap-2 px-5 py-2.5 rounded-full border border-gray-200 bg-white"
                  >
                    <Target
                      className="w-3.5 h-3.5"
                      style={{ color: landingColors.accent }}
                    />
                    <span
                      className="text-xs font-medium whitespace-nowrap"
                      style={{ color: landingColors.ink }}
                    >
                      {award}
                    </span>
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ═══════════════════════════════════════════════════════════════════
          11. INTEGRATIONS SECTION — Two-row marquee
      ═══════════════════════════════════════════════════════════════════ */}
      <section className="py-24 lg:py-32 overflow-hidden">
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <FadeIn>
            <div className="text-center max-w-2xl mx-auto mb-12">
              <p
                className="text-sm font-semibold uppercase tracking-widest mb-3"
                style={{ color: landingColors.secondary }}
              >
                Integrations
              </p>
              <h2
                className="text-3xl lg:text-4xl font-bold tracking-tight mb-4"
                style={{ color: landingColors.ink }}
              >
                Connect to 50+ tools
              </h2>
              <p className="text-gray-500 text-lg">
                Runsheet fits into your existing stack — not the other way
                around.
              </p>
            </div>
          </FadeIn>
        </div>

        {/* Row 1 — scrolls left */}
        <div className="relative mb-4">
          <div className="flex animate-marquee-left whitespace-nowrap">
            {[...Array(2)].map((_, setIdx) => (
              <div key={setIdx} className="flex gap-3 px-1.5">
                {[
                  "SAP ERP",
                  "Oracle TMS",
                  "Google Maps",
                  "Fleetio",
                  "QuickBooks",
                  "Xero",
                  "Slack",
                  "Microsoft Teams",
                  "Twilio",
                  "SendGrid",
                  "Stripe",
                  "Square",
                ].map((tool) => (
                  <div
                    key={`${setIdx}-${tool}`}
                    className="inline-flex items-center gap-2 px-4 py-2 rounded-lg border border-gray-200 bg-white hover:shadow-sm transition-shadow"
                  >
                    <Link2 className="w-3 h-3 text-gray-400" />
                    <span
                      className="text-xs font-medium whitespace-nowrap"
                      style={{ color: landingColors.ink }}
                    >
                      {tool}
                    </span>
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>

        {/* Row 2 — scrolls right */}
        <div className="relative">
          <div className="flex animate-marquee-right whitespace-nowrap">
            {[...Array(2)].map((_, setIdx) => (
              <div key={setIdx} className="flex gap-3 px-1.5">
                {[
                  "Salesforce",
                  "HubSpot",
                  "Zendesk",
                  "Jira",
                  "Power BI",
                  "Tableau",
                  "AWS S3",
                  "Google Cloud",
                  "MongoDB",
                  "PostgreSQL",
                  "Redis",
                  "Elasticsearch",
                ].map((tool) => (
                  <div
                    key={`${setIdx}-${tool}`}
                    className="inline-flex items-center gap-2 px-4 py-2 rounded-lg border border-gray-200 bg-white hover:shadow-sm transition-shadow"
                  >
                    <Link2 className="w-3 h-3 text-gray-400" />
                    <span
                      className="text-xs font-medium whitespace-nowrap"
                      style={{ color: landingColors.ink }}
                    >
                      {tool}
                    </span>
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ═══════════════════════════════════════════════════════════════════
          12. FINAL CTA
      ═══════════════════════════════════════════════════════════════════ */}
      <section
        className="py-24 lg:py-32"
        style={{ backgroundColor: landingColors.ink }}
      >
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <FadeIn>
            <div className="relative text-center">
              {/* Background accents */}
              <div
                className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[600px] h-[300px] rounded-full opacity-10 blur-3xl pointer-events-none"
                style={{ backgroundColor: landingColors.accent }}
              />

              <div className="relative">
                <h2 className="text-3xl lg:text-5xl font-bold text-white tracking-tight mb-4">
                  Get a personalized demo
                </h2>
                <p className="text-white/60 text-lg max-w-xl mx-auto mb-8">
                  See how Runsheet can transform your fleet operations. Our team
                  will walk you through the platform with your data.
                </p>
                <Link
                  href="/demo"
                  className="inline-flex items-center gap-2 px-8 py-4 rounded-full text-white font-semibold transition-all hover:opacity-90 shadow-lg"
                  style={{ backgroundColor: landingColors.accent }}
                >
                  Get a Demo
                  <ArrowRight className="w-4 h-4" />
                </Link>
              </div>
            </div>
          </FadeIn>
        </div>
      </section>

      {/* ═══════════════════════════════════════════════════════════════════
          13. ENTERPRISE FOOTER — 5 columns
      ═══════════════════════════════════════════════════════════════════ */}
      <footer className="border-t border-gray-100 py-16">
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-10 mb-12">
            {/* Brand */}
            <div className="col-span-2 md:col-span-3 lg:col-span-1">
              <div className="flex items-center gap-2.5 mb-4">
                <div
                  className="w-8 h-8 rounded-lg flex items-center justify-center"
                  style={{ backgroundColor: landingColors.accent }}
                >
                  <Truck className="w-4 h-4 text-white" />
                </div>
                <span
                  className="text-lg font-bold"
                  style={{ color: landingColors.ink }}
                >
                  Runsheet
                </span>
              </div>
              <p className="text-sm text-gray-400 leading-relaxed">
                AI-powered fleet management for modern logistics operations.
              </p>
            </div>

            {/* Platform */}
            <div>
              <p
                className="text-xs font-semibold uppercase tracking-widest mb-4"
                style={{ color: landingColors.ink }}
              >
                Platform
              </p>
              <ul className="space-y-2.5">
                {[
                  "Fleet Tracking",
                  "Fuel Management",
                  "Scheduling",
                  "Analytics",
                  "AI Agents",
                ].map((link) => (
                  <li key={link}>
                    <a
                      href="#"
                      className="text-sm text-gray-400 hover:text-gray-600 transition-colors"
                    >
                      {link}
                    </a>
                  </li>
                ))}
              </ul>
            </div>

            {/* Solutions */}
            <div>
              <p
                className="text-xs font-semibold uppercase tracking-widest mb-4"
                style={{ color: landingColors.ink }}
              >
                Solutions
              </p>
              <ul className="space-y-2.5">
                {[
                  "Fuel Distribution",
                  "Last-Mile Delivery",
                  "Cross-Border",
                  "Fleet Operators",
                  "Enterprise",
                ].map((link) => (
                  <li key={link}>
                    <a
                      href="#"
                      className="text-sm text-gray-400 hover:text-gray-600 transition-colors"
                    >
                      {link}
                    </a>
                  </li>
                ))}
              </ul>
            </div>

            {/* Resources */}
            <div>
              <p
                className="text-xs font-semibold uppercase tracking-widest mb-4"
                style={{ color: landingColors.ink }}
              >
                Resources
              </p>
              <ul className="space-y-2.5">
                {[
                  "Documentation",
                  "API Reference",
                  "Blog",
                  "Case Studies",
                  "Status",
                ].map((link) => (
                  <li key={link}>
                    <a
                      href="#"
                      className="text-sm text-gray-400 hover:text-gray-600 transition-colors"
                    >
                      {link}
                    </a>
                  </li>
                ))}
              </ul>
            </div>

            {/* Company */}
            <div>
              <p
                className="text-xs font-semibold uppercase tracking-widest mb-4"
                style={{ color: landingColors.ink }}
              >
                Company
              </p>
              <ul className="space-y-2.5">
                {["About", "Careers", "Press", "Contact", "Partners"].map(
                  (link) => (
                    <li key={link}>
                      <a
                        href="#"
                        className="text-sm text-gray-400 hover:text-gray-600 transition-colors"
                      >
                        {link}
                      </a>
                    </li>
                  ),
                )}
              </ul>
            </div>

            {/* Legal */}
            <div>
              <p
                className="text-xs font-semibold uppercase tracking-widest mb-4"
                style={{ color: landingColors.ink }}
              >
                Legal
              </p>
              <ul className="space-y-2.5">
                {[
                  "Terms of Service",
                  "Privacy Policy",
                  "Cookie Policy",
                  "Security",
                  "GDPR",
                ].map((link) => (
                  <li key={link}>
                    <a
                      href="#"
                      className="text-sm text-gray-400 hover:text-gray-600 transition-colors"
                    >
                      {link}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          </div>

          <div className="flex flex-col sm:flex-row items-center justify-between pt-8 border-t border-gray-100 gap-4">
            <p className="text-xs text-gray-400">
              © 2025 Runsheet. All rights reserved.
            </p>
            <div className="flex items-center gap-6">
              {["Terms", "Privacy", "Cookies"].map((link) => (
                <a
                  key={link}
                  href="#"
                  className="text-xs text-gray-400 hover:text-gray-600 transition-colors"
                >
                  {link}
                </a>
              ))}
            </div>
          </div>
        </div>
      </footer>
    </div>
  );
}
