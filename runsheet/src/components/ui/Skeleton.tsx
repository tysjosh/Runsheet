/**
 * Skeleton: placeholder rows for content areas while they load (replaces the
 * inline spinners; `LoadingSpinner` stays for full-page boot only). The
 * wrapper announces "Loading" once; the bars are decorative.
 */
export interface SkeletonProps {
  rows?: number;
  /** Row height in px. */
  height?: number;
  label?: string;
  className?: string;
}

export function Skeleton({
  rows = 3,
  height = 16,
  label = "Loading",
  className = "",
}: SkeletonProps) {
  return (
    <div
      role="status"
      aria-live="polite"
      aria-label={label}
      className={`space-y-2 ${className}`}
    >
      {Array.from({ length: rows }, (_, i) => (
        <div
          key={i}
          aria-hidden="true"
          className="animate-pulse rounded bg-slate-200"
          style={{ height, width: `${92 - ((i * 17) % 30)}%` }}
        />
      ))}
    </div>
  );
}
