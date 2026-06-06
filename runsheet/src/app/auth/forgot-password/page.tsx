"use client";

/**
 * Self-initiated "forgot password" page.
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

import { Truck } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import EmailPassword from "supertokens-auth-react/recipe/emailpassword";

export default function ForgotPasswordPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [sent, setSent] = useState(false);

  const handleSubmit = async () => {
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
    } catch {
      setError("Something went wrong. Please try again.");
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 p-8">
      <div className="w-full max-w-md">
        <div className="flex justify-center mb-8">
          <div className="bg-primary rounded-full p-4">
            <Truck className="w-8 h-8 text-white" />
          </div>
        </div>

        {sent ? (
          <div className="text-center space-y-6">
            <h1 className="text-2xl font-bold text-gray-900">
              Check your email
            </h1>
            <p className="text-gray-600 leading-relaxed">
              If an account exists for{" "}
              <span className="font-medium">{email}</span>, a password-reset
              link is on its way. Open it to set a new password.
            </p>
            <button
              type="button"
              onClick={() => router.replace("/signin")}
              className="w-full bg-primary hover:bg-primary-hover text-white font-medium py-3 px-4 rounded-lg transition-all focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2"
            >
              Back to sign in
            </button>
          </div>
        ) : (
          <>
            <div className="mb-8 text-center">
              <h1 className="text-2xl font-bold text-gray-900 mb-2">
                Reset your password
              </h1>
              <p className="text-gray-600">
                Enter your email and we&apos;ll send you a reset link.
              </p>
            </div>

            <div className="space-y-6">
              <div>
                <label
                  htmlFor="email"
                  className="block text-sm font-medium text-gray-700 mb-2"
                >
                  Email address *
                </label>
                <input
                  id="email"
                  type="email"
                  autoComplete="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleSubmit();
                  }}
                  className="w-full px-4 py-3 bg-white border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent transition-all"
                  placeholder="Enter your email"
                />
              </div>

              {error && (
                <div
                  className="bg-error-light border border-error-light rounded-lg p-3"
                  role="alert"
                >
                  <p className="text-sm text-error">{error}</p>
                </div>
              )}

              <button
                type="button"
                onClick={handleSubmit}
                disabled={isLoading}
                className="w-full bg-primary hover:bg-primary-hover text-white font-medium py-3 px-4 rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2"
              >
                {isLoading ? "Sending..." : "Send reset link"}
              </button>

              <button
                type="button"
                onClick={() => router.replace("/signin")}
                className="w-full text-sm text-gray-900 hover:underline"
              >
                Back to sign in
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
