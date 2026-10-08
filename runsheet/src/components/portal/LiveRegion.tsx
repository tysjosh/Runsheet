/**
 * A polite `role="status"` live region. It is always rendered (empty when
 * there is nothing to say) so assistive technology announces changes to it.
 */
export default function LiveRegion({
  message,
  id,
  className = "",
}: {
  message: string | null | undefined;
  id?: string;
  className?: string;
}) {
  return (
    <div
      id={id}
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className={
        message
          ? `rounded-lg border border-blue-300 bg-blue-50 px-3 py-2 text-sm font-medium text-blue-800 ${className}`
          : "sr-only"
      }
    >
      {message ?? ""}
    </div>
  );
}
