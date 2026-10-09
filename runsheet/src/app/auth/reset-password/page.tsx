"use client";

/**
 * Set a password from a link (D33, R14.18, PE5).
 *
 * The same page serves a password reset and a portal invite (the invite link
 * carries `invite=1`, PE5), so the copy is neutral: "Set your password", with
 * a welcome line for invites. The password rule is shown before the first
 * attempt, and each field is linked to its error with `aria-describedby`.
 *
 * SuperTokens appends a `token` (and `tenantId`) query param to the link.
 * `EmailPassword.submitNewPassword` reads that token from the URL and posts
 * the new password to the SDK's `/auth/user/password/reset` route. The calls
 * and the status → state mapping are unchanged; only the look and copy moved
 * to the tokens.
 */

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import EmailPassword from "supertokens-auth-react/recipe/emailpassword";
import { PASSWORD_RULE } from "./copy";

type Phase = "form" | "missing-token" | "done";
type ErrorField = "password" | "confirm" | "form";

const focusRing =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2";
const fieldClass = `block h-11 w-full rounded-[10px] border border-slate-400 bg-surface px-3 text-base text-text placeholder:text-slate-500 aria-[invalid=true]:border-red-700 ${focusRing}`;
const labelClass = "mb-1.5 block text-sm font-semibold text-text";
const primary = `inline-flex h-11 w-full items-center justify-center rounded-[10px] bg-primary px-4 text-[15px] font-semibold text-on-primary hover:bg-primary-hover aria-disabled:cursor-not-allowed aria-disabled:opacity-70 ${focusRing}`;
const link = `inline-flex min-h-6 items-center rounded-md px-1 text-sm font-semibold text-link underline-offset-2 hover:underline ${focusRing}`;

function Card({ children }: { children: React.ReactNode }) {
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
        {children}
      </div>
    </main>
  );
}

export default function ResetPasswordPage() {
  const router = useRouter();
  const [phase, setPhase] = useState<Phase>("form");
  const [invite, setInvite] = useState(false);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<{
    field: ErrorField;
    message: string;
  } | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  // The token arrives as a `?token=...` query param on the link. Without it
  // there is nothing to submit, so steer the user to request a fresh link.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (!params.get("token")) setPhase("missing-token");
    setInvite(params.get("invite") === "1");
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isLoading) return;
    setError(null);

    if (!password) {
      setError({ field: "password", message: "Please fill in both fields" });
      return;
    }
    if (!confirm) {
      setError({ field: "confirm", message: "Please fill in both fields" });
      return;
    }
    if (password !== confirm) {
      setError({ field: "confirm", message: "Passwords do not match" });
      return;
    }

    setIsLoading(true);
    try {
      // The SDK reads the reset token from the URL query string.
      const response = await EmailPassword.submitNewPassword({
        formFields: [{ id: "password", value: password }],
      });

      if (response.status === "FIELD_ERROR") {
        setError({
          field: "password",
          message:
            response.formFields[0]?.error ??
            "Password does not meet the requirements.",
        });
        return;
      }
      if (response.status === "RESET_PASSWORD_INVALID_TOKEN_ERROR") {
        setError({
          field: "form",
          message:
            "This link is invalid or has expired. Request a new one below.",
        });
        setPhase("missing-token");
        return;
      }
      setPhase("done");
    } catch {
      setError({
        field: "form",
        message: "Something went wrong. Please try again.",
      });
    } finally {
      setIsLoading(false);
    }
  };

  if (phase === "done") {
    return (
      <Card>
        <h1 className="mt-6 text-2xl font-bold">Password set</h1>
        <p role="status" className="mt-1 text-[15px] text-text-muted">
          Your password is set. You can now sign in.
        </p>
        <button
          type="button"
          onClick={() => router.replace("/signin")}
          className={`${primary} mt-6`}
        >
          Go to sign in
        </button>
      </Card>
    );
  }

  if (phase === "missing-token") {
    return (
      <Card>
        <h1 className="mt-6 text-2xl font-bold">Reset link required</h1>
        {error?.field === "form" && (
          <p
            role="alert"
            className="mt-3 rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-sm font-medium text-red-800"
          >
            {error.message}
          </p>
        )}
        <p className="mt-2 text-[15px] leading-relaxed text-text-muted">
          To set your password, open the link from your email. If it has
          expired, request a new one with your email address.
        </p>
        <div className="mt-6 flex flex-col items-center gap-2">
          <a href="/auth/forgot-password" className={link}>
            Request a new link
          </a>
          <button
            type="button"
            onClick={() => router.replace("/signin")}
            className={link}
          >
            Back to sign in
          </button>
        </div>
      </Card>
    );
  }

  const describe = (field: ErrorField, hint?: string) =>
    [hint, error?.field === field ? "reset-error" : undefined]
      .filter(Boolean)
      .join(" ") || undefined;

  return (
    <Card>
      <h1 className="mt-6 text-2xl font-bold">Set your password</h1>
      <p className="mt-1 text-[15px] text-text-muted">
        {invite
          ? "Welcome. Choose a password to finish setting up your account."
          : "Choose a password for your account."}
      </p>

      <form noValidate onSubmit={handleSubmit} className="mt-6 space-y-4">
        <div>
          <label htmlFor="new-password" className={labelClass}>
            New password
          </label>
          <input
            id="new-password"
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-invalid={error?.field === "password" || undefined}
            aria-describedby={describe("password", "password-rule")}
            className={fieldClass}
          />
          <p id="password-rule" className="mt-1 text-sm text-text-muted">
            {PASSWORD_RULE}
          </p>
        </div>

        <div>
          <label htmlFor="confirm-password" className={labelClass}>
            Confirm password
          </label>
          <input
            id="confirm-password"
            type="password"
            autoComplete="new-password"
            required
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            aria-invalid={error?.field === "confirm" || undefined}
            aria-describedby={describe("confirm")}
            className={fieldClass}
          />
        </div>

        {error && (
          <div
            className="rounded-lg border border-red-300 bg-red-50 px-3 py-2"
            role="alert"
          >
            <p id="reset-error" className="text-sm font-medium text-red-800">
              {error.message}
            </p>
          </div>
        )}

        <button
          type="submit"
          aria-disabled={isLoading || undefined}
          className={primary}
        >
          {isLoading ? "Saving…" : "Set password"}
        </button>
      </form>
    </Card>
  );
}
