import { secondaryButton } from "./styles";

/** Loading text in a `role="status"` region. */
export function PortalLoading({ label = "Loading…" }: { label?: string }) {
  return (
    <p role="status" className="py-6 text-sm text-gray-700">
      {label}
    </p>
  );
}

/** A failed load, announced with `role="alert"`, with an optional retry. */
export function PortalLoadError({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="space-y-3 py-6">
      <p role="alert" className="text-sm text-gray-900">
        {message}
      </p>
      {onRetry && (
        <button type="button" className={secondaryButton} onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}
