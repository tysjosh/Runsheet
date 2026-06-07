"use client";

import { ArrowLeft, ArrowRight, Eye, EyeOff } from "lucide-react";
import Link from "next/link";
import type React from "react";
import { useState } from "react";

interface SignInProps {
  onSignIn?: (email: string, password: string) => Promise<void>;
}

export default function SignIn({ onSignIn }: SignInProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async () => {
    setError("");
    setIsLoading(true);

    if (!email || !password) {
      setError("Please fill in all fields");
      setIsLoading(false);
      return;
    }

    if (!email.includes("@")) {
      setError("Please enter a valid email address");
      setIsLoading(false);
      return;
    }

    try {
      if (onSignIn) {
        await onSignIn(email, password);
      }
    } catch (err) {
      setError(
        err instanceof Error && err.message
          ? err.message
          : "Invalid credentials. Please try again.",
      );
    } finally {
      setIsLoading(false);
    }
  };

  const handleKeyPress = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      handleSubmit();
    }
  };

  const fieldClass =
    "w-full rounded-lg border bg-[#0a0a0b] px-4 py-3 text-sm text-[#f5f4ef] placeholder:text-[#f5f4ef]/30 transition-colors focus:outline-none focus:ring-2 focus:ring-[#16b88c]/50";
  const labelClass =
    "mb-2 block font-mono text-[10px] uppercase tracking-[0.2em] text-[#f5f4ef]/50";

  return (
    <div className="flex min-h-screen bg-[#0a0a0b] text-[#f5f4ef] antialiased">
      {/* ─── LEFT — branded photo panel (hidden on small) ─── */}
      <div className="relative hidden w-1/2 overflow-hidden border-r border-[#f5f4ef]/10 lg:flex">
        {/* grid backdrop */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 opacity-[0.12]"
          style={{
            backgroundImage:
              "linear-gradient(#f5f4ef 1px, transparent 1px), linear-gradient(90deg, #f5f4ef 1px, transparent 1px)",
            backgroundSize: "56px 56px",
          }}
        />
        {/* accent glow */}
        <div
          aria-hidden
          className="pointer-events-none absolute -left-24 top-1/3 h-[420px] w-[420px] rounded-full blur-[150px]"
          style={{
            background:
              "radial-gradient(circle, rgba(22,184,140,0.22), transparent 70%)",
          }}
        />

        {/* logo */}
        <Link
          href="/"
          className="absolute left-8 top-7 z-20 flex items-baseline gap-px"
        >
          <span className="text-lg font-black uppercase tracking-tight">
            RUN<span className="text-[#16b88c]">/</span>SHEET
          </span>
          <span className="ml-1.5 font-mono text-[9px] uppercase tracking-[0.3em] text-[#f5f4ef]/40">
            beta
          </span>
        </Link>

        <div className="relative z-10 flex w-full flex-col justify-center px-12 xl:px-16">
          {/* framed image (console style) */}
          <div className="overflow-hidden rounded-2xl border border-[#f5f4ef]/12 bg-[#101012]">
            <div className="flex items-center gap-2 border-b border-[#f5f4ef]/10 px-4 py-2.5">
              <span className="flex gap-1.5">
                <span className="h-2 w-2 rounded-full bg-[#ef4444]" />
                <span className="h-2 w-2 rounded-full bg-[#f59e0b]" />
                <span className="h-2 w-2 rounded-full bg-[#16b88c]" />
              </span>
              <span className="ml-2 font-mono text-[10px] uppercase tracking-[0.2em] text-[#f5f4ef]/45">
                fleet.live
              </span>
            </div>
            <div className="relative aspect-[4/3] overflow-hidden">
              <img
                src="https://images.unsplash.com/photo-1601584115197-04ecc0da31d7?w=1200&auto=format&fit=crop"
                alt="Fuel distribution fleet"
                className="h-full w-full object-cover"
              />
              <div className="absolute inset-0 bg-gradient-to-t from-[#0a0a0b] via-transparent to-transparent" />
            </div>
          </div>

          {/* tagline */}
          <div className="mt-10 max-w-md">
            <div className="mb-4 flex items-center gap-3 font-mono text-[11px] uppercase tracking-[0.3em] text-[#16b88c]">
              <span className="h-px w-8 bg-[#16b88c]" />
              Dispatch Console
            </div>
            <p className="text-2xl font-bold leading-snug tracking-tight">
              Forecast the runout. Optimize the load. Replan in seconds.
            </p>
            <p className="mt-4 font-mono text-[11px] uppercase tracking-[0.2em] text-[#f5f4ef]/45">
              Runsheet — autonomous fuel operations
            </p>
          </div>
        </div>
      </div>

      {/* ─── RIGHT — sign-in form ─── */}
      <div className="relative flex w-full items-center justify-center px-6 py-12 lg:w-1/2 lg:px-16">
        {/* mobile grid backdrop */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 opacity-[0.08] lg:hidden"
          style={{
            backgroundImage:
              "linear-gradient(#f5f4ef 1px, transparent 1px), linear-gradient(90deg, #f5f4ef 1px, transparent 1px)",
            backgroundSize: "48px 48px",
          }}
        />

        <div className="relative w-full max-w-sm">
          {/* mobile logo + back */}
          <div className="mb-10 flex items-center justify-between lg:hidden">
            <Link href="/" className="flex items-baseline gap-px">
              <span className="text-lg font-black uppercase tracking-tight">
                RUN<span className="text-[#16b88c]">/</span>SHEET
              </span>
            </Link>
            <Link
              href="/"
              className="inline-flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-[0.2em] text-[#f5f4ef]/50 transition-colors hover:text-[#f5f4ef]"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              Home
            </Link>
          </div>

          {/* header */}
          <div className="mb-8">
            <div className="mb-5 flex items-center gap-3 font-mono text-[11px] uppercase tracking-[0.3em] text-[#16b88c]">
              <span className="h-px w-8 bg-[#16b88c]" />
              Operator Access
            </div>
            <h1 className="text-[clamp(2rem,5vw,3rem)] font-black uppercase leading-[0.92] tracking-[-0.02em]">
              Sign in.
            </h1>
            <p className="mt-3 text-sm text-[#f5f4ef]/55">
              Welcome back — enter your credentials to reach the dispatch
              console.
            </p>
          </div>

          {/* form */}
          <div className="space-y-5">
            <div>
              <label htmlFor="email" className={labelClass}>
                Email address
              </label>
              <input
                id="email"
                name="email"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                onKeyPress={handleKeyPress}
                aria-describedby={error ? "signin-error" : undefined}
                className={fieldClass}
                style={{ borderColor: "rgba(245,244,239,0.15)" }}
                placeholder="you@distributor.com"
              />
            </div>

            <div>
              <div className="mb-2 flex items-center justify-between">
                <label htmlFor="password" className={`${labelClass} mb-0`}>
                  Password
                </label>
                <a
                  href="/auth/forgot-password"
                  className="font-mono text-[10px] uppercase tracking-[0.15em] text-[#f5f4ef]/45 transition-colors hover:text-[#16b88c]"
                >
                  Forgot?
                </a>
              </div>
              <div className="relative">
                <input
                  id="password"
                  name="password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  onKeyPress={handleKeyPress}
                  className={`${fieldClass} pr-12`}
                  style={{ borderColor: "rgba(245,244,239,0.15)" }}
                  placeholder="Enter your password"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  aria-label={showPassword ? "Hide password" : "Show password"}
                  className="absolute inset-y-0 right-0 flex items-center pr-4 text-[#f5f4ef]/40 transition-colors hover:text-[#f5f4ef]"
                >
                  {showPassword ? (
                    <EyeOff className="h-5 w-5" />
                  ) : (
                    <Eye className="h-5 w-5" />
                  )}
                </button>
              </div>
            </div>

            {error && (
              <div
                className="rounded-lg border border-[#ef4444]/40 bg-[#ef4444]/10 p-3"
                role="alert"
              >
                <p id="signin-error" className="text-sm text-[#ef4444]">
                  {error}
                </p>
              </div>
            )}

            <button
              type="button"
              onClick={handleSubmit}
              disabled={isLoading}
              className="group inline-flex w-full items-center justify-center gap-2 rounded-full bg-[#16b88c] px-7 py-3.5 text-sm font-bold uppercase tracking-[0.12em] text-[#06231b] transition-all hover:bg-[#1ed3a0] disabled:cursor-not-allowed disabled:opacity-60"
            >
              {isLoading ? (
                <>
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-[#06231b] border-t-transparent" />
                  Signing in…
                </>
              ) : (
                <>
                  Sign In
                  <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
                </>
              )}
            </button>
          </div>

          {/* footer — prospect path */}
          <p className="mt-8 text-center font-mono text-[11px] uppercase tracking-[0.15em] text-[#f5f4ef]/40">
            No account yet?{" "}
            <Link
              href="/request-pilot"
              className="text-[#16b88c] hover:underline"
            >
              Request a Pilot
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
