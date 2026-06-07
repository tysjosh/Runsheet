"use client";

/**
 * Request a Pilot — prospect lead-capture page.
 *
 * Distinct from /signin (which authenticates existing operators). This is the
 * sales entry point: a short qualification form for distributors who want to
 * trial Runsheet. Matches the landing page's dark editorial aesthetic.
 *
 * The submit handler currently resolves locally into a success state — there
 * is no lead-capture backend yet. Wire `submitPilotRequest` to a real endpoint
 * (or a CRM webhook) when one exists; the validated payload shape is ready.
 */

import { ArrowLeft, ArrowRight, Check, Loader2 } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

const FLEET_SIZES = [
  "1–10 trucks",
  "11–50 trucks",
  "51–200 trucks",
  "200+ trucks",
] as const;

interface PilotForm {
  name: string;
  email: string;
  company: string;
  fleetSize: string;
  message: string;
}

interface PilotErrors {
  name?: string;
  email?: string;
  company?: string;
  fleetSize?: string;
}

const EMPTY: PilotForm = {
  name: "",
  email: "",
  company: "",
  fleetSize: "",
  message: "",
};

function validate(form: PilotForm): PilotErrors {
  const errors: PilotErrors = {};
  if (!form.name.trim()) errors.name = "Your name is required";
  if (!form.email.trim()) {
    errors.email = "A work email is required";
  } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.email.trim())) {
    errors.email = "Enter a valid email address";
  }
  if (!form.company.trim()) errors.company = "Company is required";
  if (!form.fleetSize) errors.fleetSize = "Select a fleet size";
  return errors;
}

/**
 * Placeholder submission. Resolves after a short delay to simulate a network
 * round-trip. Replace the body with a POST to the real lead endpoint.
 */
async function submitPilotRequest(_payload: PilotForm): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 900));
}

export default function RequestPilotPage() {
  const [form, setForm] = useState<PilotForm>(EMPTY);
  const [errors, setErrors] = useState<PilotErrors>({});
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);

  const update = (field: keyof PilotForm, value: string) => {
    setForm((prev) => ({ ...prev, [field]: value }));
    setErrors((prev) => ({ ...prev, [field]: undefined }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const next = validate(form);
    if (Object.keys(next).length > 0) {
      setErrors(next);
      return;
    }
    setSubmitting(true);
    try {
      await submitPilotRequest(form);
      setDone(true);
    } finally {
      setSubmitting(false);
    }
  };

  const fieldClass =
    "w-full rounded-lg border bg-[#0a0a0b] px-4 py-3 text-sm text-[#f5f4ef] placeholder:text-[#f5f4ef]/30 transition-colors focus:outline-none focus:ring-2 focus:ring-[#16b88c]/50";
  const labelClass =
    "mb-2 block font-mono text-[10px] uppercase tracking-[0.2em] text-[#f5f4ef]/50";

  return (
    <div className="min-h-screen bg-[#0a0a0b] text-[#f5f4ef] antialiased">
      {/* grid backdrop */}
      <div
        aria-hidden
        className="pointer-events-none fixed inset-0 opacity-[0.1]"
        style={{
          backgroundImage:
            "linear-gradient(#f5f4ef 1px, transparent 1px), linear-gradient(90deg, #f5f4ef 1px, transparent 1px)",
          backgroundSize: "56px 56px",
          maskImage:
            "radial-gradient(ellipse 70% 60% at 50% 0%, #000 30%, transparent 75%)",
          WebkitMaskImage:
            "radial-gradient(ellipse 70% 60% at 50% 0%, #000 30%, transparent 75%)",
        }}
      />

      {/* top bar */}
      <header className="relative border-b border-[#f5f4ef]/10">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4 lg:px-10">
          <Link href="/" className="flex items-baseline gap-px">
            <span className="text-lg font-black uppercase tracking-tight">
              RUN<span className="text-[#16b88c]">/</span>SHEET
            </span>
            <span className="ml-1.5 font-mono text-[9px] uppercase tracking-[0.3em] text-[#f5f4ef]/40">
              beta
            </span>
          </Link>
          <Link
            href="/signin"
            className="font-mono text-[11px] uppercase tracking-[0.2em] text-[#f5f4ef]/60 transition-colors hover:text-[#f5f4ef]"
          >
            Already a customer? Sign In
          </Link>
        </div>
      </header>

      <main className="relative mx-auto grid max-w-5xl gap-12 px-6 py-16 lg:grid-cols-2 lg:gap-16 lg:px-10 lg:py-24">
        {/* LEFT — pitch */}
        <div>
          <Link
            href="/"
            className="inline-flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.2em] text-[#f5f4ef]/50 transition-colors hover:text-[#f5f4ef]"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            Back
          </Link>

          <div className="mt-8 flex items-center gap-3 font-mono text-[11px] uppercase tracking-[0.3em] text-[#16b88c]">
            <span className="h-px w-8 bg-[#16b88c]" />
            Request a Pilot
          </div>

          <h1 className="mt-6 text-[clamp(2.5rem,7vw,4.5rem)] font-black uppercase leading-[0.9] tracking-[-0.03em]">
            See it on
            <br />
            <span className="text-[#16b88c]">your fleet.</span>
          </h1>

          <p className="mt-6 max-w-md text-base leading-relaxed text-[#f5f4ef]/70">
            Tell us about your operation and we'll set up a guided pilot on your
            own stations and trucks — agents start in shadow mode, so there's no
            risk to live dispatch.
          </p>

          <ul className="mt-8 space-y-3">
            {[
              "Runout forecasting on your tank telemetry",
              "Load + route optimization against your fleet",
              "No rip-and-replace — runs alongside your stack",
            ].map((line) => (
              <li key={line} className="flex items-start gap-3">
                <Check className="mt-0.5 h-4 w-4 shrink-0 text-[#16b88c]" />
                <span className="text-sm text-[#f5f4ef]/70">{line}</span>
              </li>
            ))}
          </ul>
        </div>

        {/* RIGHT — form / success */}
        <div className="rounded-2xl border border-[#f5f4ef]/12 bg-[#101012] p-6 lg:p-8">
          {done ? (
            <div className="flex h-full flex-col items-center justify-center py-10 text-center">
              <div className="flex h-14 w-14 items-center justify-center rounded-full bg-[#16b88c]/15">
                <Check className="h-7 w-7 text-[#16b88c]" />
              </div>
              <h2 className="mt-6 text-2xl font-black uppercase tracking-tight">
                Request received
              </h2>
              <p className="mt-3 max-w-sm text-sm text-[#f5f4ef]/65">
                Thanks, {form.name.split(" ")[0] || "there"}. Our team will
                reach out at{" "}
                <span className="text-[#f5f4ef]">{form.email}</span> to schedule
                your pilot.
              </p>
              <Link
                href="/"
                className="mt-8 inline-flex items-center gap-2 rounded-full border border-[#f5f4ef]/20 px-6 py-3 text-sm font-bold uppercase tracking-[0.12em] transition-all hover:border-[#f5f4ef]/50"
              >
                Back to home
              </Link>
            </div>
          ) : (
            <form onSubmit={handleSubmit} noValidate>
              <div className="mb-5">
                <label htmlFor="rp-name" className={labelClass}>
                  Full name
                </label>
                <input
                  id="rp-name"
                  type="text"
                  value={form.name}
                  onChange={(e) => update("name", e.target.value)}
                  placeholder="Jordan Rivera"
                  className={fieldClass}
                  style={{
                    borderColor: errors.name
                      ? "#ef4444"
                      : "rgba(245,244,239,0.15)",
                  }}
                />
                {errors.name && (
                  <p className="mt-1.5 text-xs text-[#ef4444]">{errors.name}</p>
                )}
              </div>

              <div className="mb-5">
                <label htmlFor="rp-email" className={labelClass}>
                  Work email
                </label>
                <input
                  id="rp-email"
                  type="email"
                  value={form.email}
                  onChange={(e) => update("email", e.target.value)}
                  placeholder="jordan@distributor.com"
                  className={fieldClass}
                  style={{
                    borderColor: errors.email
                      ? "#ef4444"
                      : "rgba(245,244,239,0.15)",
                  }}
                />
                {errors.email && (
                  <p className="mt-1.5 text-xs text-[#ef4444]">
                    {errors.email}
                  </p>
                )}
              </div>

              <div className="mb-5">
                <label htmlFor="rp-company" className={labelClass}>
                  Company
                </label>
                <input
                  id="rp-company"
                  type="text"
                  value={form.company}
                  onChange={(e) => update("company", e.target.value)}
                  placeholder="Acme Fuel Co"
                  className={fieldClass}
                  style={{
                    borderColor: errors.company
                      ? "#ef4444"
                      : "rgba(245,244,239,0.15)",
                  }}
                />
                {errors.company && (
                  <p className="mt-1.5 text-xs text-[#ef4444]">
                    {errors.company}
                  </p>
                )}
              </div>

              <div className="mb-5">
                <label htmlFor="rp-fleet" className={labelClass}>
                  Fleet size
                </label>
                <select
                  id="rp-fleet"
                  value={form.fleetSize}
                  onChange={(e) => update("fleetSize", e.target.value)}
                  className={fieldClass}
                  style={{
                    borderColor: errors.fleetSize
                      ? "#ef4444"
                      : "rgba(245,244,239,0.15)",
                  }}
                >
                  <option value="" disabled>
                    Select range…
                  </option>
                  {FLEET_SIZES.map((size) => (
                    <option key={size} value={size}>
                      {size}
                    </option>
                  ))}
                </select>
                {errors.fleetSize && (
                  <p className="mt-1.5 text-xs text-[#ef4444]">
                    {errors.fleetSize}
                  </p>
                )}
              </div>

              <div className="mb-6">
                <label htmlFor="rp-message" className={labelClass}>
                  What do you want to solve?{" "}
                  <span className="text-[#f5f4ef]/30">(optional)</span>
                </label>
                <textarea
                  id="rp-message"
                  value={form.message}
                  onChange={(e) => update("message", e.target.value)}
                  placeholder="e.g. cutting runouts across 40 retail stations"
                  rows={3}
                  className={`${fieldClass} resize-none`}
                  style={{ borderColor: "rgba(245,244,239,0.15)" }}
                />
              </div>

              <button
                type="submit"
                disabled={submitting}
                className="group inline-flex w-full items-center justify-center gap-2 rounded-full bg-[#16b88c] px-7 py-3.5 text-sm font-bold uppercase tracking-[0.12em] text-[#06231b] transition-all hover:bg-[#1ed3a0] disabled:opacity-60"
              >
                {submitting ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Sending…
                  </>
                ) : (
                  <>
                    Request Pilot
                    <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
                  </>
                )}
              </button>

              <p className="mt-4 text-center text-xs text-[#f5f4ef]/40">
                We'll only use your details to set up the pilot.
              </p>
            </form>
          )}
        </div>
      </main>
    </div>
  );
}
