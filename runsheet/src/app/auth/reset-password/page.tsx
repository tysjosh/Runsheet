"use client";

/**
 * Self-serve password reset / set page.
 *
 * Closes the SuperTokens Auth Migration gap (design OQ6): provisioned users are
 * created with a random password and no email is sent, so an admin mints a
 * reset link (POST /api/auth/admin/password-reset-link) and hands it to the
 * user out-of-band. The user opens that link here and sets their own password.
 *
 * SuperTokens appends a `token` (and `tenantId`) query param to the reset link.
 * `EmailPassword.submitNewPassword` reads that token from the URL automatically
 * and posts the new password to the SDK's `/auth/user/password/reset` route.
 *
 * If the page is opened without a token, we show the "request a link" hint
 * rather than a broken form (no email transport ships, so we point the user at
 * their admin).
 */

import { Truck } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import EmailPassword from "supertokens-auth-react/recipe/emailpassword";

type Phase = "form" | "missing-token" | "done";

export default function ResetPasswordPage() {
  const router = useRouter();
  const [phase, setPhase] = useState<Phase>("form");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(false);

  // The reset token arrives as a `?token=...` query param on the link. Without
  // it there is nothing to submit, so steer the user to request a fresh link.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const token = new URLSearchParams(window.location.search).get("token");
    if (!token) {
      setPhase("missing-token");
    }
  }, []);

  const handleSubmit = async () => {
    setError("");

    if (!password || !confirm) {
      setError("Please fill in both fields");
      return;
    }
    if (password !== confirm) {
      setError("Passwords do not match");
      return;
    }

    setIsLoading(true);
    try {
      // The SDK reads the reset token from the URL query string.
      const response = await EmailPassword.submitNewPassword({
        formFields: [{ id: "password", value: password }],
      });

      if (response.status === "FIELD_ERROR") {
        setError(
          response.formFields[0]?.error ??
            "Password does not meet the requirements.",
        );
        return;
      }
      if (response.status === "RESET_PASSWORD_INVALID_TOKEN_ERROR") {
        setError(
          "This reset link is invalid or has expired. Ask an admin for a new one.",
        );
        setPhase("missing-token");
        return;
      }
      setPhase("done");
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

        {phase === "done" ? (
          <div className="text-center space-y-6">
            <h1 className="text-2xl font-bold text-gray-900">
              Password updated
            </h1>
            <p className="text-gray-600">
              Your password has been set. You can now sign in.
            </p>
            <button
              type="button"
              onClick={() => router.replace("/signin")}
              className="w-full bg-primary hover:bg-primary-hover text-white font-medium py-3 px-4 rounded-lg transition-all focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2"
            >
              Go to sign in
            </button>
          </div>
        ) : phase === "missing-token" ? (
          <div className="text-center space-y-4">
            <h1 className="text-2xl font-bold text-gray-900">
              Reset link required
            </h1>
            <p className="text-gray-600 leading-relaxed">
              To set your password, open the reset link an administrator
              generated for you. If you don&apos;t have one, ask your admin to
              issue a password-reset link for your account.
            </p>
            <button
              type="button"
              onClick={() => router.replace("/signin")}
              className="text-sm text-gray-900 hover:underline"
            >
              Back to sign in
            </button>
          </div>
        ) : (
          <>
            <div className="mb-8 text-center">
              <h1 className="text-2xl font-bold text-gray-900 mb-2">
                Set your password
              </h1>
              <p className="text-gray-600">
                Choose a new password for your account.
              </p>
            </div>

            <div className="space-y-6">
              <div>
                <label
                  htmlFor="new-password"
                  className="block text-sm font-medium text-gray-700 mb-2"
                >
                  New password *
                </label>
                <input
                  id="new-password"
                  type="password"
                  autoComplete="new-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full px-4 py-3 bg-white border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent transition-all"
                  placeholder="Enter a new password"
                />
              </div>

              <div>
                <label
                  htmlFor="confirm-password"
                  className="block text-sm font-medium text-gray-700 mb-2"
                >
                  Confirm password *
                </label>
                <input
                  id="confirm-password"
                  type="password"
                  autoComplete="new-password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  className="w-full px-4 py-3 bg-white border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent transition-all"
                  placeholder="Re-enter the password"
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
                {isLoading ? "Saving..." : "Set password"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
