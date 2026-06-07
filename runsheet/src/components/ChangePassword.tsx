"use client";

/**
 * Self-service "change password" for the signed-in user.
 *
 * Posts to the backend `POST /api/auth/account/change-password`, which
 * re-verifies the current password (via the SuperTokens EmailPassword recipe)
 * before applying the new one. No email transport is involved — this is the
 * "I know my current password" path, distinct from the forgot-password reset
 * flow.
 */

import { Eye, EyeOff, Lock } from "lucide-react";
import { useState } from "react";
import { apiService } from "../services/api";

export default function ChangePassword() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [show, setShow] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);
  const [isSaving, setIsSaving] = useState(false);

  const reset = () => {
    setCurrent("");
    setNext("");
    setConfirm("");
  };

  const handleSubmit = async () => {
    setError("");
    setSuccess(false);

    if (!current || !next || !confirm) {
      setError("Please fill in all fields");
      return;
    }
    if (next !== confirm) {
      setError("The new passwords do not match");
      return;
    }
    if (next === current) {
      setError("The new password must be different from the current one");
      return;
    }

    setIsSaving(true);
    try {
      await apiService.changePassword(current, next);
      setSuccess(true);
      reset();
    } catch (err) {
      setError(
        err instanceof Error && err.message
          ? err.message
          : "Could not change your password. Please try again.",
      );
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="max-w-md p-6">
      <div className="flex items-center gap-2 mb-1">
        <Lock className="w-5 h-5 text-primary" />
        <h2 className="text-lg font-semibold text-gray-900">Change password</h2>
      </div>
      <p className="text-sm text-gray-600 mb-6">
        Update the password for your account. You&apos;ll need your current
        password.
      </p>

      <div className="space-y-5">
        <PasswordField
          id="current-password"
          label="Current password"
          value={current}
          autoComplete="current-password"
          show={show}
          onChange={setCurrent}
        />
        <PasswordField
          id="new-password"
          label="New password"
          value={next}
          autoComplete="new-password"
          show={show}
          onChange={setNext}
        />
        <PasswordField
          id="confirm-password"
          label="Confirm new password"
          value={confirm}
          autoComplete="new-password"
          show={show}
          onChange={setConfirm}
          onEnter={handleSubmit}
        />

        <label className="flex items-center gap-2 text-sm text-gray-600 cursor-pointer">
          <input
            type="checkbox"
            checked={show}
            onChange={(e) => setShow(e.target.checked)}
            className="rounded border-gray-300"
          />
          Show passwords
        </label>

        {error && (
          <div
            className="bg-error-light border border-error-light rounded-lg p-3"
            role="alert"
          >
            <p className="text-sm text-error">{error}</p>
          </div>
        )}
        {success && (
          <div
            className="bg-green-50 border border-green-200 rounded-lg p-3"
            role="status"
          >
            <p className="text-sm text-green-700">
              Your password has been changed.
            </p>
          </div>
        )}

        <button
          type="button"
          onClick={handleSubmit}
          disabled={isSaving}
          className="w-full bg-primary hover:bg-primary-hover text-white font-medium py-3 px-4 rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2"
        >
          {isSaving ? "Saving..." : "Change password"}
        </button>
      </div>
    </div>
  );
}

interface PasswordFieldProps {
  id: string;
  label: string;
  value: string;
  autoComplete: string;
  show: boolean;
  onChange: (value: string) => void;
  onEnter?: () => void;
}

function PasswordField({
  id,
  label,
  value,
  autoComplete,
  show,
  onChange,
  onEnter,
}: PasswordFieldProps) {
  const [localShow, setLocalShow] = useState(false);
  const visible = show || localShow;

  return (
    <div>
      <label
        htmlFor={id}
        className="block text-sm font-medium text-gray-700 mb-2"
      >
        {label} *
      </label>
      <div className="relative">
        <input
          id={id}
          type={visible ? "text" : "password"}
          autoComplete={autoComplete}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && onEnter) onEnter();
          }}
          className="w-full px-4 py-3 pr-12 bg-white border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent transition-all"
        />
        <button
          type="button"
          onClick={() => setLocalShow((v) => !v)}
          aria-label={visible ? "Hide password" : "Show password"}
          className="absolute inset-y-0 right-0 pr-4 flex items-center text-gray-500 hover:text-gray-600 transition-colors focus:outline-none focus:ring-2 focus:ring-gray-500 rounded"
        >
          {visible ? (
            <EyeOff className="w-5 h-5" />
          ) : (
            <Eye className="w-5 h-5" />
          )}
        </button>
      </div>
    </div>
  );
}
