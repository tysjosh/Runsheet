"use client";

/**
 * Sign-in for every Runsheet user, staff and customers alike (PD20 keeps one
 * `/signin`; D33, R14.17).
 *
 * - A real `<form>` with a submit button, so Enter submits from any field.
 * - Tab order: Email → Password → Show password → Sign in → Forgot password?
 *   ("Forgot password?" sits below the form; it used to sit between Email and
 *   Password, finding V1).
 * - Audience-neutral copy; light tokens on the form side with AA contrast and
 *   targets ≥ 24 px. The dark marketing art panel stays on ≥ 1024 px only.
 *
 * Behaviour is unchanged: the same client-side checks and messages, and
 * `onSignIn` (the SuperTokens call and the staff/customer routing) lives in
 * `app/signin/page.tsx`.
 */
import { Eye, EyeOff } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

interface SignInProps {
  onSignIn?: (email: string, password: string) => Promise<void>;
}

const focusRing =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2";
const fieldClass = `block h-11 w-full rounded-[10px] border border-slate-400 bg-surface px-3 text-base text-text placeholder:text-slate-500 aria-[invalid=true]:border-red-700 ${focusRing}`;
const labelClass = "mb-1.5 block text-sm font-semibold text-text";

export default function SignIn({ onSignIn }: SignInProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (isLoading) return;
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

  return (
    <div className="flex min-h-screen bg-canvas text-text antialiased">
      {/* Art panel (≥ 1024 px): the marketing look, decorative. */}
      <div
        aria-hidden="true"
        className="relative hidden w-1/2 overflow-hidden bg-[#0a0a0b] text-[#f5f4ef] lg:flex"
      >
        <div
          className="pointer-events-none absolute inset-0 opacity-[0.12]"
          style={{
            backgroundImage:
              "linear-gradient(#f5f4ef 1px, transparent 1px), linear-gradient(90deg, #f5f4ef 1px, transparent 1px)",
            backgroundSize: "56px 56px",
          }}
        />
        <div
          className="pointer-events-none absolute -left-24 top-1/3 h-[420px] w-[420px] rounded-full blur-[150px]"
          style={{
            background:
              "radial-gradient(circle, rgba(22,184,140,0.22), transparent 70%)",
          }}
        />
        <div className="relative z-10 flex w-full flex-col justify-center px-12 xl:px-16">
          <div className="overflow-hidden rounded-2xl border border-[#f5f4ef]/15 bg-[#101012]">
            <div className="relative aspect-[4/3] overflow-hidden">
              <img
                src="https://images.unsplash.com/photo-1601584115197-04ecc0da31d7?w=1200&auto=format&fit=crop"
                alt=""
                className="h-full w-full object-cover"
              />
              <div className="absolute inset-0 bg-gradient-to-t from-[#0a0a0b] via-transparent to-transparent" />
            </div>
          </div>
          <p className="mt-10 max-w-md text-2xl font-bold leading-snug tracking-tight">
            Fuel deliveries, planned and tracked in one place.
          </p>
        </div>
      </div>

      {/* Form side: light tokens. */}
      <main className="flex w-full items-center justify-center px-4 py-12 lg:w-1/2">
        <div className="w-full max-w-[400px] rounded-2xl border border-border bg-surface p-6 shadow-sm sm:p-8">
          <Link
            href="/"
            className={`inline-flex min-h-6 items-center gap-2 rounded-md ${focusRing}`}
          >
            <span
              aria-hidden="true"
              className="inline-flex h-7 w-7 items-center justify-center rounded-lg bg-brand-600 text-[13px] font-extrabold text-white"
            >
              R
            </span>
            <span className="text-[15px] font-bold text-text">Runsheet</span>
          </Link>

          <h1 className="mt-6 text-2xl font-bold text-text">
            Sign in to Runsheet
          </h1>
          <p className="mt-1 text-[15px] text-text-muted">
            Use the email and password for your Runsheet account.
          </p>

          <form noValidate onSubmit={handleSubmit} className="mt-6 space-y-4">
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
                aria-invalid={error ? true : undefined}
                aria-describedby={error ? "signin-error" : undefined}
                className={fieldClass}
                placeholder="you@company.com"
              />
            </div>

            <div>
              <label htmlFor="password" className={labelClass}>
                Password
              </label>
              <div className="relative">
                <input
                  id="password"
                  name="password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  aria-invalid={error ? true : undefined}
                  aria-describedby={error ? "signin-error" : undefined}
                  className={`${fieldClass} pr-12`}
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  aria-label={showPassword ? "Hide password" : "Show password"}
                  aria-pressed={showPassword}
                  className={`absolute inset-y-0 right-0 flex w-11 items-center justify-center rounded-r-[10px] text-slate-600 hover:text-text ${focusRing}`}
                >
                  {showPassword ? (
                    <EyeOff aria-hidden="true" className="h-5 w-5" />
                  ) : (
                    <Eye aria-hidden="true" className="h-5 w-5" />
                  )}
                </button>
              </div>
            </div>

            {error && (
              <div
                className="rounded-lg border border-red-300 bg-red-50 px-3 py-2"
                role="alert"
              >
                <p
                  id="signin-error"
                  className="text-sm font-medium text-red-800"
                >
                  {error}
                </p>
              </div>
            )}

            <button
              type="submit"
              aria-disabled={isLoading || undefined}
              className={`inline-flex h-11 w-full items-center justify-center gap-2 rounded-[10px] bg-primary px-4 text-[15px] font-semibold text-on-primary hover:bg-primary-hover aria-disabled:cursor-not-allowed aria-disabled:opacity-70 ${focusRing}`}
            >
              {isLoading ? (
                <>
                  <span
                    aria-hidden="true"
                    className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent"
                  />
                  Signing in…
                </>
              ) : (
                "Sign in"
              )}
            </button>
          </form>

          <p className="mt-4 text-center">
            <a
              href="/auth/forgot-password"
              className={`inline-flex min-h-6 items-center rounded-md px-1 text-sm font-semibold text-link underline-offset-2 hover:underline ${focusRing}`}
            >
              Forgot password?
            </a>
          </p>

          <p className="mt-6 border-t border-border pt-4 text-center text-sm text-text-muted">
            New to Runsheet?{" "}
            <Link
              href="/request-pilot"
              className={`inline-flex min-h-6 items-center rounded-md px-1 font-semibold text-link underline-offset-2 hover:underline ${focusRing}`}
            >
              Request a pilot
            </Link>
          </p>
        </div>
      </main>
    </div>
  );
}
