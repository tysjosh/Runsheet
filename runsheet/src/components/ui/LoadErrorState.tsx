/**
 * LoadErrorState - what a page shows when its initial fetch fails.
 *
 * Renders a distinct state per `LoadFailure.kind` (see
 * `services/apiErrors.classifyLoadError`): not found, module disabled,
 * forbidden, or a generic error banner with an optional retry.
 * Every variant offers a way back.
 *
 * A 403 shows the API's own message by default. Pages gated to Runsheet
 * staff (platform_admin) pass `staffOnly` to get the staff-access copy.
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
    case "forbidden":
      if (!staffOnly) {
        return {
          title: `You don't have access to this ${entityLabel.toLowerCase()}`,
          description: failure.message,
        };
      }
      return {
        title: "Runsheet staff access required",
        description:
          "This page is only available to Runsheet staff accounts. Your account doesn't have access to it.",
      };
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
          {failure.message}
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
