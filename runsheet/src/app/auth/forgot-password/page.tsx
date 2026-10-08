"use client";

/**
 * Self-initiated "forgot password" page (D33: tokens and copy only).
 *
 * Sends a SuperTokens password-reset email via the EmailPassword recipe
 * (`EmailPassword.sendPasswordResetEmail`, which posts to the SDK's
 * `/auth/user/password/reset/token` route). The email carries a link back to
 * `/auth/reset-password?token=...`, where the user sets a new password.
 *
 * Email delivery is configured on the backend (SMTP via SMTP_* env, or the
 * SuperTokens built-in service in development — see
 * auth/supertokens_init.py:_build_email_delivery).
 *
 * To prevent account enumeration the SDK returns OK regardless of whether the
 * email maps to a real user, so this page always shows the same confirmation.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";
import EmailPassword from "supertokens-auth-react/recipe/emailpassword";
import { throttledMessage } from "../../../utils/authThrottle";

const focusRing =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2";
const fieldClass = `block h-11 w-full rounded-[10px] border border-slate-400 bg-surface px-3 text-base text-text placeholder:text-slate-500 aria-[invalid=true]:border-red-700 ${focusRing}`;
const primary = `inline-flex h-11 w-full items-center justify-center rounded-[10px] bg-primary px-4 text-[15px] font-semibold text-on-primary hover:bg-primary-hover aria-disabled:cursor-not-allowed aria-disabled:opacity-70 ${focusRing}`;
const link = `inline-flex min-h-6 items-center rounded-md px-1 text-sm font-semibold text-link underline-offset-2 hover:underline ${focusRing}`;

export default function ForgotPasswordPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [sent, setSent] = useState(false);

  const handleSubmit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (isLoading) return;
    setError("");

    if (!email || !email.includes("@")) {
      setError("Please enter a valid email address");
      return;
    }

    setIsLoading(true);
    try {
      const response = await EmailPassword.sendPasswordResetEmail({
        formFields: [{ id: "email", value: email }],
      });

      if (response.status === "FIELD_ERROR") {
        setError(
          response.formFields[0]?.error ??
            "Please enter a valid email address.",
        );
        return;
      }
      // OK (or any non-field status): show the same confirmation to avoid
      // leaking whether the address maps to a real account.
      setSent(true);
    } catch (err) {
      // A throttled request (F5) rejects with the raw 429 response.
      setError(
        (await throttledMessage(err)) ??
          "Something went wrong. Please try again.",
      );
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <main className="flex min-h-screen items-center justify-center bg-canvas px-4 py-12 text-text">
      <div className="w-full max-w-[400px] rounded-2xl border border-border bg-surface p-6 shadow-sm sm:p-8">
        <p className="inline-flex items-center gap-2">
          <span
            aria-hidden="true"
            className="inline-flex h-7 w-7 items-center justify-center rounded-lg bg-brand-600 text-[13px] font-extrabold text-white"
          >
            R
          </span>
          <span className="text-[15px] font-bold">Runsheet</span>
        </p>

        {sent ? (
          <>
            <h1 className="mt-6 text-2xl font-bold">Check your email</h1>
            <p
              role="status"
              className="mt-2 text-[15px] leading-relaxed text-text-muted"
            >
              If an account exists for{" "}
              <span className="font-semibold text-text">{email}</span>, a link
              to set a new password is on its way.
            </p>
            <button
              type="button"
              onClick={() => router.replace("/signin")}
              className={`${primary} mt-6`}
            >
              Back to sign in
            </button>
          </>
        ) : (
          <>
            <h1 className="mt-6 text-2xl font-bold">Forgot your password?</h1>
            <p className="mt-1 text-[15px] text-text-muted">
              Enter your email and we&apos;ll send you a link to set a new one.
            </p>
            <form noValidate onSubmit={handleSubmit} className="mt-6 space-y-4">
              <div>
                <label
                  htmlFor="email"
                  className="mb-1.5 block text-sm font-semibold"
                >
                  Email address
                </label>
                <input
                  id="email"
                  type="email"
                  autoComplete="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  aria-invalid={error ? true : undefined}
                  aria-describedby={error ? "forgot-error" : undefined}
                  className={fieldClass}
                  placeholder="you@company.com"
                />
              </div>

              {error && (
                <div
                  className="rounded-lg border border-red-300 bg-red-50 px-3 py-2"
                  role="alert"
                >
                  <p
                    id="forgot-error"
                    className="text-sm font-medium text-red-800"
                  >
                    {error}
                  </p>
                </div>
              )}

              <button
                type="submit"
                aria-disabled={isLoading || undefined}
                className={primary}
              >
                {isLoading ? "Sending…" : "Send reset link"}
              </button>
            </form>
            <p className="mt-4 text-center">
              <button
                type="button"
                onClick={() => router.replace("/signin")}
                className={link}
              >
                Back to sign in
              </button>
            </p>
          </>
        )}
      </div>
    </main>
  );
}
