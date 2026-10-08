/**
 * CSV exports (data-export v1).
 *
 * Each export is `GET <list endpoint>/export` with the page's current filters.
 * The response is streamed CSV; we read it into a Blob and save it through a
 * temporary `<a download>`, taking the filename from `Content-Disposition`.
 */
import { ApiError, ApiTimeoutError, fetchWithSession } from "./api";
import { apiErrorFromResponse } from "./apiErrors";
import { buildQueryString, fetchWithTimeout } from "./utils";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080/api";

export type ExportType =
  | "ifta"
  | "orders"
  | "jobs"
  | "reconciliation"
  | "invoices"
  | "driver_qualifications"
  | "margin";

export type ExportParams = Record<
  string,
  string | number | boolean | undefined | null
>;

export const EXPORT_PATHS: Record<ExportType, string> = {
  ifta: "/compliance/ifta/report/export",
  orders: "/orders/export",
  jobs: "/scheduling/jobs/export",
  reconciliation: "/fuel/mvp/reconciliation/export",
  invoices: "/commerce/invoices/export",
  // OI-57. The driver hours export has no admin page, so no entry here.
  driver_qualifications: "/compliance/drivers/export",
  // margin-feed FR7: tenant admin only.
  margin: "/commerce/margin/records/export",
};

/** Large exports stream for a while; the timeout bounds time-to-headers. */
export const EXPORT_TIMEOUT_MS = 120_000;

/** Extract the filename from a `Content-Disposition` header. */
export function filenameFromContentDisposition(
  header: string | null,
  fallback: string,
): string {
  if (!header) return fallback;
  const match = /filename="?([^";]+)"?/i.exec(header);
  const name = match?.[1]?.trim();
  return name ? name : fallback;
}

/** Save a downloaded Blob through a temporary `<a download>`. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    URL.revokeObjectURL(url);
  }
}

/**
 * Download one CSV export with the given filters.
 *
 * Throws `ApiError` with the HTTP status on a non-2xx response (413 too
 * large, 429 rate limited, 403 forbidden, ...), and `ApiError` status 0 when
 * the transfer fails, including a stream the server aborted after the
 * headers arrived.
 */
export async function downloadCsvExport(
  type: ExportType,
  params: ExportParams,
): Promise<{ filename: string }> {
  const url = `${API_BASE_URL}${EXPORT_PATHS[type]}${buildQueryString(params)}`;
  try {
    const response = await fetchWithSession(
      fetchWithTimeout,
      url,
      { method: "GET", headers: { Accept: "text/csv" } },
      EXPORT_TIMEOUT_MS,
    );
    if (!response.ok) {
      throw await apiErrorFromResponse(response);
    }
    const blob = await response.blob();
    const filename = filenameFromContentDisposition(
      response.headers.get("Content-Disposition"),
      `${type}_export.csv`,
    );
    saveBlob(blob, filename);
    return { filename };
  } catch (error) {
    if (error instanceof ApiTimeoutError || error instanceof ApiError) {
      throw error;
    }
    throw new ApiError(
      error instanceof Error ? error.message : "Unknown error",
      0,
    );
  }
}
