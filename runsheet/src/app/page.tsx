"use client";

import {
  Activity,
  ArrowRight,
  BarChart3,
  Bot,
  CheckCircle,
  ChevronRight,
  Droplets,
  Fuel,
  Globe,
  Layers,
  MapPin,
  Menu,
  Route,
  Shield,
  Sparkles,
  Truck,
  X,
  Zap,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

/* ─── Color Palette ───────────────────────────────────────────────────────────
 * Charcoal:  #3E3E3E  — primary text, dark backgrounds
 * Gold:      #F1C40F  — primary accent, CTAs
 * Orange:    #E67E22  — secondary accent, highlights
 * Blue:      #2980B9  — tertiary accent, links
 * White:     #FFFFFF  — backgrounds, light text
 * ──────────────────────────────────────────────────────────────────────────── */

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
            const eased = 1 - Math.pow(1 - progress, 3);
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

// ─── Main Landing Page ───────────────────────────────────────────────────────

export default function LandingPage() {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 20);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <div className="min-h-screen bg-white font-[family-name:var(--font-geist-sans)]">
      {/* ── Navigation ──────────────────────────────────────────────────── */}
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
                style={{ backgroundColor: "#F1C40F" }}
              >
                <Truck className="w-5 h-5 text-white" />
              </div>
              <span
                className="text-xl font-bold tracking-tight"
                style={{ color: "#3E3E3E" }}
              >
                Runsheet
              </span>
            </Link>

            {/* Desktop Nav */}
            <div className="hidden lg:flex items-center gap-8">
              {["Features", "Platform", "Metrics", "Testimonials"].map(
                (item) => (
                  <a
                    key={item}
                    href={`#${item.toLowerCase()}`}
                    className="text-sm font-medium transition-colors hover:opacity-80"
                    style={{ color: "#3E3E3E" }}
                  >
                    {item}
                  </a>
                ),
              )}
            </div>

            {/* CTA Buttons */}
            <div className="hidden lg:flex items-center gap-3">
              <Link
                href="/signin"
                className="text-sm font-medium px-4 py-2 rounded-lg transition-all hover:bg-gray-50"
                style={{ color: "#3E3E3E" }}
              >
                Sign In
              </Link>
              <Link
                href="/signin"
                className="text-sm font-semibold px-5 py-2.5 rounded-lg text-white transition-all hover:opacity-90 shadow-sm"
                style={{ backgroundColor: "#F1C40F" }}
              >
                Get Started
              </Link>
            </div>

            {/* Mobile Menu Toggle */}
            <button
              onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
              className="lg:hidden p-2 rounded-lg hover:bg-gray-100"
              aria-label="Toggle menu"
            >
              {mobileMenuOpen ? (
                <X className="w-5 h-5" style={{ color: "#3E3E3E" }} />
              ) : (
                <Menu className="w-5 h-5" style={{ color: "#3E3E3E" }} />
              )}
            </button>
          </div>
        </div>

        {/* Mobile Menu */}
        {mobileMenuOpen && (
          <div className="lg:hidden bg-white border-t border-gray-100 shadow-lg">
            <div className="px-6 py-4 space-y-3">
              {["Features", "Platform", "Metrics", "Testimonials"].map(
                (item) => (
                  <a
                    key={item}
                    href={`#${item.toLowerCase()}`}
                    onClick={() => setMobileMenuOpen(false)}
                    className="block text-sm font-medium py-2"
                    style={{ color: "#3E3E3E" }}
                  >
                    {item}
                  </a>
                ),
              )}
              <div className="pt-3 border-t border-gray-100 flex flex-col gap-2">
                <Link
                  href="/signin"
                  className="text-sm font-medium py-2.5 text-center rounded-lg border border-gray-200"
                  style={{ color: "#3E3E3E" }}
                >
                  Sign In
                </Link>
                <Link
                  href="/signin"
                  className="text-sm font-semibold py-2.5 text-center rounded-lg text-white"
                  style={{ backgroundColor: "#F1C40F" }}
                >
                  Get Started
                </Link>
              </div>
            </div>
          </div>
        )}
      </nav>

      {/* ── Hero Section ────────────────────────────────────────────────── */}
      <section className="relative pt-32 lg:pt-40 pb-20 lg:pb-32 overflow-hidden">
        {/* Background Elements */}
        <div className="absolute inset-0 overflow-hidden pointer-events-none">
          <div
            className="absolute -top-40 -right-40 w-[600px] h-[600px] rounded-full opacity-[0.04]"
            style={{ backgroundColor: "#F1C40F" }}
          />
          <div
            className="absolute -bottom-20 -left-20 w-[400px] h-[400px] rounded-full opacity-[0.03]"
            style={{ backgroundColor: "#2980B9" }}
          />
          {/* Grid pattern */}
          <div
            className="absolute inset-0 opacity-[0.02]"
            style={{
              backgroundImage:
                "linear-gradient(#3E3E3E 1px, transparent 1px), linear-gradient(90deg, #3E3E3E 1px, transparent 1px)",
              backgroundSize: "60px 60px",
            }}
          />
        </div>

        <div className="relative max-w-7xl mx-auto px-6 lg:px-8">
          <div className="grid lg:grid-cols-2 gap-16 lg:gap-20 items-center">
            {/* Left — Copy */}
            <div>
              <FadeIn>
                <div
                  className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full text-xs font-semibold mb-6 border"
                  style={{
                    color: "#E67E22",
                    backgroundColor: "#E67E221A",
                    borderColor: "#E67E2233",
                  }}
                >
                  <Sparkles className="w-3.5 h-3.5" />
                  AI-Powered Logistics Platform
                </div>
              </FadeIn>

              <FadeIn delay={100}>
                <h1
                  className="text-4xl sm:text-5xl lg:text-6xl font-bold leading-[1.08] tracking-tight mb-6"
                  style={{ color: "#3E3E3E" }}
                >
                  Fleet operations,{" "}
                  <span
                    className="relative inline-block"
                    style={{ color: "#F1C40F" }}
                  >
                    reimagined
                    <svg
                      className="absolute -bottom-1 left-0 w-full"
                      viewBox="0 0 200 8"
                      fill="none"
                      preserveAspectRatio="none"
                    >
                      <path
                        d="M1 5.5C40 2 80 2 100 4C120 6 160 6 199 3"
                        stroke="#F1C40F"
                        strokeWidth="2.5"
                        strokeLinecap="round"
                        opacity="0.4"
                      />
                    </svg>
                  </span>
                </h1>
              </FadeIn>

              <FadeIn delay={200}>
                <p className="text-lg lg:text-xl text-gray-500 leading-relaxed mb-8 max-w-lg">
                  Runsheet unifies fleet tracking, fuel management, scheduling,
                  and AI-driven analytics into one elegant platform. Less
                  complexity, more control.
                </p>
              </FadeIn>

              <FadeIn delay={300}>
                <div className="flex flex-col sm:flex-row gap-3">
                  <Link
                    href="/signin"
                    className="inline-flex items-center justify-center gap-2 px-7 py-3.5 rounded-xl text-white font-semibold text-sm transition-all hover:opacity-90 shadow-lg shadow-yellow-500/20"
                    style={{ backgroundColor: "#F1C40F" }}
                  >
                    Start Free Trial
                    <ArrowRight className="w-4 h-4" />
                  </Link>
                  <a
                    href="#platform"
                    className="inline-flex items-center justify-center gap-2 px-7 py-3.5 rounded-xl font-semibold text-sm border border-gray-200 transition-all hover:bg-gray-50"
                    style={{ color: "#3E3E3E" }}
                  >
                    See How It Works
                  </a>
                </div>
              </FadeIn>

              <FadeIn delay={400}>
                <div className="flex items-center gap-6 mt-10 pt-8 border-t border-gray-100">
                  {[
                    { label: "Active Fleets", value: "500+" },
                    { label: "Deliveries/mo", value: "2M+" },
                    { label: "Uptime", value: "99.9%" },
                  ].map((stat) => (
                    <div key={stat.label}>
                      <p
                        className="text-lg font-bold"
                        style={{ color: "#3E3E3E" }}
                      >
                        {stat.value}
                      </p>
                      <p className="text-xs text-gray-400">{stat.label}</p>
                    </div>
                  ))}
                </div>
              </FadeIn>
            </div>

            {/* Right — Dashboard Preview */}
            <FadeIn delay={200}>
              <div className="relative">
                {/* Glow */}
                <div
                  className="absolute -inset-4 rounded-3xl opacity-20 blur-3xl"
                  style={{
                    background:
                      "linear-gradient(135deg, #F1C40F 0%, #E67E22 50%, #2980B9 100%)",
                  }}
                />

                {/* Main Card */}
                <div className="relative bg-white rounded-2xl shadow-2xl border border-gray-100 overflow-hidden">
                  {/* Title Bar */}
                  <div className="flex items-center gap-2 px-5 py-3 border-b border-gray-100 bg-gray-50/50">
                    <div className="flex gap-1.5">
                      <div className="w-2.5 h-2.5 rounded-full bg-red-400" />
                      <div className="w-2.5 h-2.5 rounded-full bg-yellow-400" />
                      <div className="w-2.5 h-2.5 rounded-full bg-green-400" />
                    </div>
                    <span className="text-[10px] text-gray-400 ml-2 font-mono">
                      runsheet.app/dashboard
                    </span>
                  </div>

                  {/* Dashboard Content */}
                  <div className="p-5">
                    {/* Top Stats Row */}
                    <div className="grid grid-cols-3 gap-3 mb-4">
                      {[
                        {
                          label: "Active Trucks",
                          value: "47",
                          icon: Truck,
                          color: "#F1C40F",
                        },
                        {
                          label: "Fuel Efficiency",
                          value: "94%",
                          icon: Fuel,
                          color: "#E67E22",
                        },
                        {
                          label: "On-Time Rate",
                          value: "98.2%",
                          icon: CheckCircle,
                          color: "#2980B9",
                        },
                      ].map((stat) => (
                        <div
                          key={stat.label}
                          className="rounded-xl p-3 border border-gray-100"
                        >
                          <div className="flex items-center gap-2 mb-2">
                            <div
                              className="w-6 h-6 rounded-md flex items-center justify-center"
                              style={{
                                backgroundColor: `${stat.color}15`,
                              }}
                            >
                              <stat.icon
                                className="w-3.5 h-3.5"
                                style={{ color: stat.color }}
                              />
                            </div>
                          </div>
                          <p
                            className="text-lg font-bold"
                            style={{ color: "#3E3E3E" }}
                          >
                            {stat.value}
                          </p>
                          <p className="text-[10px] text-gray-400">
                            {stat.label}
                          </p>
                        </div>
                      ))}
                    </div>

                    {/* Chart Placeholder */}
                    <div className="rounded-xl border border-gray-100 p-4 mb-4">
                      <div className="flex items-center justify-between mb-3">
                        <span
                          className="text-xs font-semibold"
                          style={{ color: "#3E3E3E" }}
                        >
                          Fleet Activity
                        </span>
                        <span className="text-[10px] text-gray-400">
                          Last 7 days
                        </span>
                      </div>
                      <div className="flex items-end gap-1.5 h-20">
                        {[40, 65, 45, 80, 55, 90, 70].map((h, i) => (
                          <div
                            key={i}
                            className="flex-1 rounded-t-md transition-all"
                            style={{
                              height: `${h}%`,
                              backgroundColor:
                                i === 5 ? "#F1C40F" : "#F1C40F33",
                            }}
                          />
                        ))}
                      </div>
                    </div>

                    {/* Live Routes */}
                    <div className="space-y-2">
                      {[
                        {
                          id: "TRK-042",
                          route: "Lagos → Ibadan",
                          status: "In Transit",
                          color: "#2980B9",
                        },
                        {
                          id: "TRK-018",
                          route: "Abuja → Kano",
                          status: "Loading",
                          color: "#E67E22",
                        },
                        {
                          id: "TRK-091",
                          route: "PH → Enugu",
                          status: "Delivered",
                          color: "#27ae60",
                        },
                      ].map((truck) => (
                        <div
                          key={truck.id}
                          className="flex items-center justify-between py-2 px-3 rounded-lg bg-gray-50/50"
                        >
                          <div className="flex items-center gap-3">
                            <div
                              className="w-1.5 h-1.5 rounded-full"
                              style={{ backgroundColor: truck.color }}
                            />
                            <span
                              className="text-xs font-medium"
                              style={{ color: "#3E3E3E" }}
                            >
                              {truck.id}
                            </span>
                            <span className="text-[10px] text-gray-400">
                              {truck.route}
                            </span>
                          </div>
                          <span
                            className="text-[10px] font-medium px-2 py-0.5 rounded-full"
                            style={{
                              color: truck.color,
                              backgroundColor: `${truck.color}15`,
                            }}
                          >
                            {truck.status}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>

                {/* Floating AI Badge */}
                <div
                  className="absolute -bottom-3 -left-3 bg-white rounded-xl shadow-lg border border-gray-100 px-4 py-2.5 flex items-center gap-2"
                >
                  <div
                    className="w-7 h-7 rounded-lg flex items-center justify-center"
                    style={{ backgroundColor: "#E67E22" }}
                  >
                    <Bot className="w-4 h-4 text-white" />
                  </div>
                  <div>
                    <p
                      className="text-[10px] font-semibold"
                      style={{ color: "#3E3E3E" }}
                    >
                      AI Agent Active
                    </p>
                    <p className="text-[9px] text-gray-400">
                      Optimizing 12 routes
                    </p>
                  </div>
                </div>
              </div>
            </FadeIn>
          </div>
        </div>
      </section>

      {/* ── Trusted By ──────────────────────────────────────────────────── */}
      <section className="py-12 border-y border-gray-100 bg-gray-50/50">
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <p className="text-center text-xs font-medium text-gray-400 uppercase tracking-widest mb-8">
            Trusted by logistics leaders across Africa
          </p>
          <div className="flex flex-wrap items-center justify-center gap-x-12 gap-y-6">
            {[
              "DanCom",
              "PetroCorp",
              "SwiftHaul",
              "NaijaFleet",
              "TransAfrica",
              "FuelNet",
            ].map((name) => (
              <span
                key={name}
                className="text-lg font-bold tracking-tight opacity-20"
                style={{ color: "#3E3E3E" }}
              >
                {name}
              </span>
            ))}
          </div>
        </div>
      </section>

      {/* ── Features Section ────────────────────────────────────────────── */}
      <section id="features" className="py-24 lg:py-32">
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <FadeIn>
            <div className="text-center max-w-2xl mx-auto mb-16">
              <p
                className="text-sm font-semibold uppercase tracking-widest mb-3"
                style={{ color: "#E67E22" }}
              >
                Capabilities
              </p>
              <h2
                className="text-3xl lg:text-4xl font-bold tracking-tight mb-4"
                style={{ color: "#3E3E3E" }}
              >
                Everything your fleet needs.
                <br />
                Nothing it doesn&apos;t.
              </h2>
              <p className="text-gray-500 text-lg">
                Six integrated modules that replace a dozen disconnected tools.
              </p>
            </div>
          </FadeIn>

          <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-6">
            {[
              {
                icon: Truck,
                title: "Fleet Tracking",
                desc: "Real-time GPS tracking with live map view, geofencing alerts, and historical route playback.",
                color: "#F1C40F",
              },
              {
                icon: Droplets,
                title: "Fuel Distribution",
                desc: "AI-optimized fuel plans with compartment loading, route planning, and demand forecasting.",
                color: "#E67E22",
              },
              {
                icon: Route,
                title: "Smart Scheduling",
                desc: "Drag-and-drop job board with automated asset assignment, delay detection, and cargo tracking.",
                color: "#2980B9",
              },
              {
                icon: BarChart3,
                title: "Analytics & Insights",
                desc: "Failure analytics, scheduling metrics, completion rates, and asset utilization dashboards.",
                color: "#F1C40F",
              },
              {
                icon: Bot,
                title: "AI Agents",
                desc: "Autonomous agents for delay response, fuel management, and SLA monitoring with configurable autonomy.",
                color: "#E67E22",
              },
              {
                icon: Activity,
                title: "Ops Monitoring",
                desc: "Pipeline health dashboards with ingestion, indexing, and poison queue metrics — auto-refreshing.",
                color: "#2980B9",
              },
            ].map((feature, i) => (
              <FadeIn key={feature.title} delay={i * 80}>
                <div className="group relative bg-white rounded-2xl border border-gray-100 p-7 hover:shadow-lg hover:border-gray-200 transition-all duration-300 h-full">
                  <div
                    className="w-11 h-11 rounded-xl flex items-center justify-center mb-5"
                    style={{ backgroundColor: `${feature.color}12` }}
                  >
                    <feature.icon
                      className="w-5 h-5"
                      style={{ color: feature.color }}
                    />
                  </div>
                  <h3
                    className="text-base font-semibold mb-2"
                    style={{ color: "#3E3E3E" }}
                  >
                    {feature.title}
                  </h3>
                  <p className="text-sm text-gray-500 leading-relaxed">
                    {feature.desc}
                  </p>
                  <div
                    className="absolute bottom-0 left-7 right-7 h-0.5 rounded-full opacity-0 group-hover:opacity-100 transition-opacity"
                    style={{ backgroundColor: feature.color }}
                  />
                </div>
              </FadeIn>
            ))}
          </div>
        </div>
      </section>

      {/* ── Platform Section ─────────────────────────────────────────────── */}
      <section id="platform" className="py-24 lg:py-32" style={{ backgroundColor: "#3E3E3E" }}>
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <div className="grid lg:grid-cols-2 gap-16 items-center">
            {/* Left — Visual */}
            <FadeIn>
              <div className="relative">
                <div className="bg-white/5 backdrop-blur-sm rounded-2xl border border-white/10 p-8">
                  <div className="space-y-5">
                    {[
                      {
                        icon: Globe,
                        label: "Real-Time Visibility",
                        desc: "Track every asset across your entire network",
                        active: true,
                      },
                      {
                        icon: Zap,
                        label: "Instant Replanning",
                        desc: "AI responds to disruptions in seconds, not hours",
                        active: false,
                      },
                      {
                        icon: Shield,
                        label: "Enterprise Security",
                        desc: "Role-based access, audit logs, and tenant isolation",
                        active: false,
                      },
                      {
                        icon: Layers,
                        label: "Modular Architecture",
                        desc: "Use what you need — each module works independently",
                        active: false,
                      },
                    ].map((item) => (
                      <div
                        key={item.label}
                        className={`flex items-start gap-4 p-4 rounded-xl transition-all ${
                          item.active
                            ? "bg-white/10 border border-white/10"
                            : "hover:bg-white/5"
                        }`}
                      >
                        <div
                          className="w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0"
                          style={{
                            backgroundColor: item.active
                              ? "#F1C40F"
                              : "rgba(255,255,255,0.08)",
                          }}
                        >
                          <item.icon
                            className="w-5 h-5"
                            style={{
                              color: item.active ? "#fff" : "rgba(255,255,255,0.5)",
                            }}
                          />
                        </div>
                        <div>
                          <p className="text-sm font-semibold text-white">
                            {item.label}
                          </p>
                          <p className="text-xs text-white/50 mt-0.5">
                            {item.desc}
                          </p>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </FadeIn>

            {/* Right — Copy */}
            <div>
              <FadeIn>
                <p
                  className="text-sm font-semibold uppercase tracking-widest mb-3"
                  style={{ color: "#F1C40F" }}
                >
                  Platform
                </p>
              </FadeIn>
              <FadeIn delay={100}>
                <h2 className="text-3xl lg:text-4xl font-bold tracking-tight text-white mb-6">
                  Built for the way
                  <br />
                  logistics actually works
                </h2>
              </FadeIn>
              <FadeIn delay={200}>
                <p className="text-white/60 text-lg leading-relaxed mb-8">
                  Most platforms bolt on features as afterthoughts. Runsheet was
                  designed from day one as an integrated system — fleet, fuel,
                  scheduling, and intelligence working together seamlessly.
                </p>
              </FadeIn>
              <FadeIn delay={300}>
                <div className="space-y-4">
                  {[
                    "Unified data model across all modules",
                    "Real-time WebSocket updates under 5 seconds",
                    "AI agents with configurable autonomy levels",
                    "Multi-tenant with full data isolation",
                  ].map((point) => (
                    <div key={point} className="flex items-center gap-3">
                      <CheckCircle
                        className="w-4 h-4 flex-shrink-0"
                        style={{ color: "#F1C40F" }}
                      />
                      <span className="text-sm text-white/80">{point}</span>
                    </div>
                  ))}
                </div>
              </FadeIn>
            </div>
          </div>
        </div>
      </section>

      {/* ── Metrics Section ──────────────────────────────────────────────── */}
      <section id="metrics" className="py-24 lg:py-32 bg-gray-50/50">
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <FadeIn>
            <div className="text-center max-w-2xl mx-auto mb-16">
              <p
                className="text-sm font-semibold uppercase tracking-widest mb-3"
                style={{ color: "#2980B9" }}
              >
                Impact
              </p>
              <h2
                className="text-3xl lg:text-4xl font-bold tracking-tight mb-4"
                style={{ color: "#3E3E3E" }}
              >
                Numbers that move the needle
              </h2>
              <p className="text-gray-500 text-lg">
                Real results from real operations running on Runsheet.
              </p>
            </div>
          </FadeIn>

          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-6">
            {[
              {
                value: 35,
                suffix: "%",
                label: "Fuel cost reduction",
                desc: "Through AI-optimized distribution",
                color: "#F1C40F",
              },
              {
                value: 98,
                suffix: "%",
                label: "On-time delivery rate",
                desc: "With predictive delay detection",
                color: "#E67E22",
              },
              {
                value: 60,
                suffix: "%",
                label: "Faster replanning",
                desc: "AI agents respond in seconds",
                color: "#2980B9",
              },
              {
                value: 12,
                suffix: "x",
                label: "ROI in first year",
                desc: "Average across all customers",
                color: "#3E3E3E",
              },
            ].map((metric, i) => (
              <FadeIn key={metric.label} delay={i * 100}>
                <div className="bg-white rounded-2xl border border-gray-100 p-7 text-center hover:shadow-md transition-shadow">
                  <p
                    className="text-4xl lg:text-5xl font-bold mb-2"
                    style={{ color: metric.color }}
                  >
                    <AnimatedCounter
                      end={metric.value}
                      suffix={metric.suffix}
                    />
                  </p>
                  <p
                    className="text-sm font-semibold mb-1"
                    style={{ color: "#3E3E3E" }}
                  >
                    {metric.label}
                  </p>
                  <p className="text-xs text-gray-400">{metric.desc}</p>
                </div>
              </FadeIn>
            ))}
          </div>
        </div>
      </section>

      {/* ── Testimonials ─────────────────────────────────────────────────── */}
      <section id="testimonials" className="py-24 lg:py-32">
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <FadeIn>
            <div className="text-center max-w-2xl mx-auto mb-16">
              <p
                className="text-sm font-semibold uppercase tracking-widest mb-3"
                style={{ color: "#F1C40F" }}
              >
                Testimonials
              </p>
              <h2
                className="text-3xl lg:text-4xl font-bold tracking-tight"
                style={{ color: "#3E3E3E" }}
              >
                Loved by operations teams
              </h2>
            </div>
          </FadeIn>

          <div className="grid md:grid-cols-3 gap-6">
            {[
              {
                quote:
                  "Runsheet replaced four separate tools for us. The AI fuel optimization alone saved us millions in the first quarter.",
                name: "Adebayo Ogunlesi",
                role: "VP Operations, PetroCorp",
                color: "#F1C40F",
              },
              {
                quote:
                  "The real-time visibility is incredible. We went from guessing where trucks were to knowing exactly — down to the minute.",
                name: "Chioma Nwosu",
                role: "Fleet Manager, SwiftHaul",
                color: "#E67E22",
              },
              {
                quote:
                  "The scheduling module with cargo tracking transformed how we manage multi-stop deliveries. Our on-time rate jumped 23%.",
                name: "Ibrahim Musa",
                role: "Logistics Director, TransAfrica",
                color: "#2980B9",
              },
            ].map((testimonial, i) => (
              <FadeIn key={testimonial.name} delay={i * 100}>
                <div className="bg-white rounded-2xl border border-gray-100 p-7 hover:shadow-md transition-shadow h-full flex flex-col">
                  <div
                    className="w-8 h-1 rounded-full mb-5"
                    style={{ backgroundColor: testimonial.color }}
                  />
                  <p
                    className="text-sm leading-relaxed flex-1 mb-6"
                    style={{ color: "#3E3E3E" }}
                  >
                    &ldquo;{testimonial.quote}&rdquo;
                  </p>
                  <div className="flex items-center gap-3">
                    <div
                      className="w-9 h-9 rounded-full flex items-center justify-center text-white text-xs font-bold"
                      style={{ backgroundColor: testimonial.color }}
                    >
                      {testimonial.name
                        .split(" ")
                        .map((n) => n[0])
                        .join("")}
                    </div>
                    <div>
                      <p
                        className="text-sm font-semibold"
                        style={{ color: "#3E3E3E" }}
                      >
                        {testimonial.name}
                      </p>
                      <p className="text-xs text-gray-400">
                        {testimonial.role}
                      </p>
                    </div>
                  </div>
                </div>
              </FadeIn>
            ))}
          </div>
        </div>
      </section>

      {/* ── CTA Section ──────────────────────────────────────────────────── */}
      <section className="py-24 lg:py-32">
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <FadeIn>
            <div
              className="relative rounded-3xl overflow-hidden px-8 py-16 lg:px-16 lg:py-20 text-center"
              style={{ backgroundColor: "#3E3E3E" }}
            >
              {/* Background accents */}
              <div
                className="absolute top-0 right-0 w-80 h-80 rounded-full opacity-10 blur-3xl"
                style={{ backgroundColor: "#F1C40F" }}
              />
              <div
                className="absolute bottom-0 left-0 w-60 h-60 rounded-full opacity-10 blur-3xl"
                style={{ backgroundColor: "#2980B9" }}
              />

              <div className="relative">
                <h2 className="text-3xl lg:text-5xl font-bold text-white tracking-tight mb-4">
                  Ready to transform your
                  <br />
                  fleet operations?
                </h2>
                <p className="text-white/60 text-lg max-w-xl mx-auto mb-8">
                  Join hundreds of logistics companies already using Runsheet to
                  move smarter, faster, and more efficiently.
                </p>
                <div className="flex flex-col sm:flex-row items-center justify-center gap-3">
                  <Link
                    href="/signin"
                    className="inline-flex items-center gap-2 px-8 py-4 rounded-xl text-white font-semibold transition-all hover:opacity-90 shadow-lg"
                    style={{ backgroundColor: "#F1C40F" }}
                  >
                    Get Started Free
                    <ArrowRight className="w-4 h-4" />
                  </Link>
                  <a
                    href="#features"
                    className="inline-flex items-center gap-2 px-8 py-4 rounded-xl font-semibold text-white/80 border border-white/20 transition-all hover:bg-white/5"
                  >
                    Explore Features
                  </a>
                </div>
              </div>
            </div>
          </FadeIn>
        </div>
      </section>

      {/* ── Footer ───────────────────────────────────────────────────────── */}
      <footer className="border-t border-gray-100 py-16">
        <div className="max-w-7xl mx-auto px-6 lg:px-8">
          <div className="grid md:grid-cols-4 gap-10 mb-12">
            {/* Brand */}
            <div className="md:col-span-1">
              <div className="flex items-center gap-2.5 mb-4">
                <div
                  className="w-8 h-8 rounded-lg flex items-center justify-center"
                  style={{ backgroundColor: "#F1C40F" }}
                >
                  <Truck className="w-4 h-4 text-white" />
                </div>
                <span
                  className="text-lg font-bold"
                  style={{ color: "#3E3E3E" }}
                >
                  Runsheet
                </span>
              </div>
              <p className="text-sm text-gray-400 leading-relaxed">
                AI-powered fleet management platform for modern logistics
                operations.
              </p>
            </div>

            {/* Links */}
            {[
              {
                title: "Product",
                links: [
                  "Fleet Tracking",
                  "Fuel Management",
                  "Scheduling",
                  "Analytics",
                  "AI Agents",
                ],
              },
              {
                title: "Company",
                links: ["About", "Careers", "Blog", "Press", "Contact"],
              },
              {
                title: "Resources",
                links: [
                  "Documentation",
                  "API Reference",
                  "Status",
                  "Security",
                  "Privacy",
                ],
              },
            ].map((col) => (
              <div key={col.title}>
                <p
                  className="text-xs font-semibold uppercase tracking-widest mb-4"
                  style={{ color: "#3E3E3E" }}
                >
                  {col.title}
                </p>
                <ul className="space-y-2.5">
                  {col.links.map((link) => (
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
            ))}
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
