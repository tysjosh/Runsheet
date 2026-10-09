/**
 * Turn a snake_case status (e.g. ``in_progress``) into title case
 * (``In Progress``). A missing status renders as an em dash instead of
 * throwing, so a partial API payload can't crash the page.
 */
export function formatStatus(status: string | null | undefined): string {
  if (!status) return "—";
  return status
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}
