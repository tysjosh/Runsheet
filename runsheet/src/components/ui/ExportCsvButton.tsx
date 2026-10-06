"use client";

/**
 * ExportCsvButton - role-gated "Export CSV" action for list pages.
 *
 * Downloads the page's current filtered view as CSV through
 * `downloadCsvExport`. The role gate is presentational only; the backend
 * re-checks roles on every export. Errors are announced through an adjacent
 * `role="alert"` paragraph (the shared toast has no live region), and success
 * through a visually hidden `role="status"` message.
 *
 * The accessible name is "Export CSV: <subject>": the visible label plus
 * screen-reader-only context, with no `aria-label`, so it always contains the
 * visible text (WCAG 2.5.3 Label in Name).
 */
import { Download } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { hasAnyRole } from "../../config/modules";
import { ApiError } from "../../services/api";
import {
  downloadCsvExport,
  type ExportParams,
  type ExportType,
} from "../../services/exportApi";
import { getCurrentUserRoles } from "../../utils/auth";
import { Button } from "./Button";

export interface ExportCsvButtonProps {
  type: ExportType;
  /** Current page filters, already mapped to the export route's param names. */
  params: ExportParams;
  /** Roles allowed to see the button (exact match via hasAnyRole). */
  allowedRoles: readonly string[];
  /** Screen-reader context appended to the visible label, e.g. "orders". */
  subject: string;
  /** Test seam; when omitted the roles come from getCurrentUserRoles(). */
  rolesOverride?: readonly string[] | null;
}

export const EXPORT_MESSAGES = {
  tooLarge:
    "This export is over 50,000 rows. Narrow the filters and try again.",
  rateLimited: "Too many exports in a short time. Wait a minute and try again.",
  forbidden: "You don't have permission to export this data.",
  generic: "The export didn't complete. Try again.",
  success: "Export downloaded",
} as const;

function messageFor(error: unknown): string {
  const status = error instanceof ApiError ? error.status : 0;
  if (status === 413) return EXPORT_MESSAGES.tooLarge;
  if (status === 429) return EXPORT_MESSAGES.rateLimited;
  if (status === 403) return EXPORT_MESSAGES.forbidden;
  return EXPORT_MESSAGES.generic;
}

export function ExportCsvButton({
  type,
  params,
  allowedRoles,
  subject,
  rolesOverride,
}: ExportCsvButtonProps) {
  const [resolvedRoles, setResolvedRoles] = useState<readonly string[] | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const inFlight = useRef(false);
  const useOverride = rolesOverride !== undefined;

  useEffect(() => {
    if (useOverride) return;
    let cancelled = false;
    (async () => {
      const r = await getCurrentUserRoles();
      if (!cancelled) setResolvedRoles(r);
    })();
    return () => {
      cancelled = true;
    };
  }, [useOverride]);

  const roles = useOverride ? rolesOverride : resolvedRoles;
  if (!roles || !hasAnyRole(roles, allowedRoles)) return null;

  const onClick = async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    setStatus("");
    try {
      await downloadCsvExport(type, params);
      setStatus(EXPORT_MESSAGES.success);
    } catch (e) {
      setError(messageFor(e));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  return (
    <div className="inline-flex flex-col items-end">
      <Button
        type="button"
        variant="secondary"
        size="sm"
        icon={<Download className="w-4 h-4" aria-hidden="true" />}
        loading={busy}
        aria-busy={busy}
        onClick={onClick}
      >
        Export CSV<span className="sr-only">: {subject}</span>
      </Button>
      <p role="alert" className={error ? "text-xs text-error mt-1" : "sr-only"}>
        {error ?? ""}
      </p>
      <p role="status" className="sr-only">
        {status}
      </p>
    </div>
  );
}

export default ExportCsvButton;
