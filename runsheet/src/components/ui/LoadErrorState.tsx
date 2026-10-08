/**
 * LoadErrorState - what a page shows when its initial fetch fails.
 *
 * Renders a distinct state per `LoadFailure.kind` (see
 * `services/apiErrors.classifyLoadError`): not found, module disabled,
 * forbidden, or a generic error banner with an optional retry.
 * Every variant offers a way back.
 *
 * A 403 shows "You don't have access to this …" with the API's message and,
 * when the envelope names them, the roles that grant access (R11.3). The
 * Runsheet-staff copy appears only when the API says the page needs
 * platform_admin (`details.reason === "platform_admin_required"`), or for a
 * `staffOnly` page whose 403 carries no reason (OI-48).
 *
 * A request that never reached the API (`kind: "network"`) shows "Can't reach
 * Runsheet…" with Retry instead of the browser's "Failed to fetch" (R11.1).
 * Pages that already pad their content pass `embedded` to drop the outer
 * padding.
 */

import Link from "next/link";
import type { LoadFailure } from "../../services/apiErrors";
import { Button } from "./Button";

export interface LoadErrorStateProps {
  failure: LoadFailure;
  /** Singular label for the thing being loaded, e.g. "Terminal". */
  entityLabel: string;
  entityId?: string;
  onBack: () => void;
  backLabel?: string;
  homeHref?: string;
  homeLabel?: string;
  onRetry?: () => void;
  /** The page is Runsheet-staff only: a 403 shows the staff-access copy. */
  staffOnly?: boolean;
  /** Rendered inside an already-padded page: drop the outer `p-6`. */
  embedded?: boolean;
}

export const NETWORK_COPY =
  "Can't reach Runsheet. Check your connection and retry.";

/** "Ask an administrator for the admin or dispatcher role." from the envelope. */
function roleHint(details: Record<string, unknown> | undefined): string | null {
  const raw = details?.required_roles ?? details?.required_role;
  const roles = (Array.isArray(raw) ? raw : [raw]).filter(
    (r): r is string => typeof r === "string" && r.length > 0,
  );
  if (roles.length === 0) return null;
  const list =
    roles.length === 1
      ? roles[0]
      : `${roles.slice(0, -1).join(", ")} or ${roles[roles.length - 1]}`;
  return `Ask an administrator for the ${list.replace(/_/g, " ")} role.`;
}

function copyFor(
  failure: LoadFailure,
  entityLabel: string,
  entityId: string | undefined,
  staffOnly: boolean,
): { title: string; description: string } | null {
  switch (failure.kind) {
    case "not_found":
      return {
        title: `${entityLabel} not found`,
        description: `We couldn't find ${entityLabel.toLowerCase()}${
          entityId ? ` "${entityId}"` : ""
        }. It may have been removed, or the link may be wrong.`,
      };
    case "module_disabled":
      return {
        title: `${failure.moduleName ?? entityLabel} isn't enabled for your account`,
        description:
          "This module is turned off for your workspace. Contact your Runsheet administrator if you need it.",
      };
    case "forbidden": {
      const reason = failure.details?.reason;
      const staffCopy =
        reason === "platform_admin_required" ||
        (staffOnly && reason === undefined);
      if (!staffCopy) {
        const hint = roleHint(failure.details);
        return {
          title: `You don't have access to this ${entityLabel.toLowerCase()}`,
          description: hint ? `${failure.message} ${hint}` : failure.message,
        };
      }
      return {
        title: "Runsheet staff access required",
        description:
          "This page is only available to Runsheet staff accounts. Your account doesn't have access to it.",
      };
    }
    default:
      return null;
  }
}

export function LoadErrorState({
  failure,
  entityLabel,
  entityId,
  onBack,
  backLabel,
  homeHref,
  homeLabel,
  onRetry,
  staffOnly = false,
  embedded = false,
}: LoadErrorStateProps) {
  const copy = copyFor(failure, entityLabel, entityId, staffOnly);
  const wrapperClass = embedded ? undefined : "p-6";

  const actions = (
    <div className="flex flex-wrap items-center gap-4 mt-4">
      {!copy && onRetry && (
        <Button variant="secondary" onClick={onRetry}>
          Try again
        </Button>
      )}
      <Button variant="ghost" onClick={onBack}>
        ← {backLabel ?? "Go back"}
      </Button>
      {homeHref && (
        <Link
          href={homeHref}
          className="text-sm font-medium text-primary underline hover:no-underline"
        >
          {homeLabel ?? "Go to dashboard"}
        </Link>
      )}
    </div>
  );

  if (!copy) {
    return (
      <div className={wrapperClass}>
        <div
          role="alert"
          className="bg-error-light border border-error-light text-error-dark p-4 rounded"
        >
          {failure.kind === "network" ? NETWORK_COPY : failure.message}
        </div>
        {actions}
      </div>
    );
  }

  return (
    <div className={wrapperClass}>
      <div role="status" className="max-w-xl">
        <h2 className="text-xl font-semibold text-gray-900">{copy.title}</h2>
        <p className="mt-2 text-gray-600">{copy.description}</p>
      </div>
      {actions}
    </div>
  );
}

export default LoadErrorState;
