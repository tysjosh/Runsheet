"use client";

/**
 * Tenant-wide portal online ordering switch (portal-fixes B2), shown at the
 * top of the Portal access panel because that's where staff manage the
 * portal. Admin only, like the panel; the API re-checks (`PUT
 * /api/commerce/portal-settings`). Default on. It is separate from the
 * `order_intake_pipeline` rollout flag: turning it off only stops new portal
 * requests; requests already sent stay in the awaiting-confirmation queue.
 *
 * A native checkbox (label + hint + live status), so keyboard and screen
 * reader behaviour come from the platform. Hidden when the API answers 404
 * (portal off).
 */
import { useEffect, useId, useState } from "react";
import { ApiError } from "../../services/api";
import {
  getPortalSettings,
  updatePortalSettings,
} from "../../services/portalApi";

type State =
  | { kind: "loading" }
  | { kind: "hidden" }
  | { kind: "error"; message: string }
  | { kind: "ready"; enabled: boolean };

export default function PortalOrderingSetting() {
  const uid = useId();
  const [state, setState] = useState<State>({ kind: "loading" });
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");

  useEffect(() => {
    let cancelled = false;
    getPortalSettings()
      .then((s) => {
        if (!cancelled)
          setState({ kind: "ready", enabled: s.ordering_enabled });
      })
      .catch((error) => {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 404) {
          setState({ kind: "hidden" });
        } else {
          setState({
            kind: "error",
            message: "The online ordering setting couldn't be loaded.",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.kind === "hidden" || state.kind === "loading") return null;
  if (state.kind === "error") {
    return <p className="text-sm text-error-dark">{state.message}</p>;
  }

  const inputId = `${uid}-ordering`;
  const hintId = `${uid}-ordering-hint`;

  const toggle = async (next: boolean) => {
    if (saving) return;
    setSaving(true);
    setStatus("");
    try {
      const saved = await updatePortalSettings({ ordering_enabled: next });
      setState({ kind: "ready", enabled: saved.ordering_enabled });
      setStatus(
        saved.ordering_enabled
          ? "Online ordering is on for all portal customers."
          : "Online ordering is off. Customers are asked to contact you instead.",
      );
    } catch (error) {
      setStatus(
        error instanceof ApiError && error.status === 403
          ? "Only an admin can change this setting."
          : "The setting couldn't be saved. Try again.",
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-1 border-b pb-4">
      <div className="flex items-start gap-3">
        <input
          id={inputId}
          type="checkbox"
          checked={state.enabled}
          disabled={saving}
          aria-describedby={hintId}
          onChange={(e) => void toggle(e.target.checked)}
          className="mt-0.5 h-5 w-5 shrink-0 accent-[var(--rs-primary)]"
        />
        <div>
          <label htmlFor={inputId} className="block text-sm font-medium">
            Customers can request deliveries online
          </label>
          <p id={hintId} className="text-xs text-gray-600">
            Applies to every customer with portal access. Requests arrive as
            awaiting confirmation for a dispatcher to confirm or decline.
            Turning it off doesn&apos;t affect requests already sent.
          </p>
        </div>
      </div>
      <p role="status" className="text-sm text-gray-700">
        {status}
      </p>
    </div>
  );
}
