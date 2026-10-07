"use client";

/**
 * DriverActivitySection - the job's driver messages and exceptions (G1).
 *
 * Reads ``GET /scheduling/jobs/{id}/driver-activity`` (dispatcher/admin only),
 * newest first, with a type filter and previous/next paging. Errors show the
 * API's own message (the client extracts it with ``extractApiErrorMessage``).
 */
import { MessageSquare } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { extractApiErrorMessage } from "../../services/apiErrors";
import {
  type DriverActivityItem,
  type DriverActivityParams,
  type DriverActivityType,
  getJobDriverActivity,
} from "../../services/schedulingApi";

const PAGE_SIZE = 20;
// The backend reads at most 1000 rows (page * size); past that it returns 422.
const MAX_PAGES = Math.floor(1000 / PAGE_SIZE);

function formatTimestamp(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function typeLabel(item: DriverActivityItem): string {
  if (item.type === "message") return "Message";
  const detail = [item.exception_type, item.severity]
    .filter(Boolean)
    .join(", ");
  return detail ? `Exception (${detail})` : "Exception";
}

export interface DriverActivitySectionProps {
  jobId: string;
}

export default function DriverActivitySection({
  jobId,
}: DriverActivitySectionProps) {
  const headingId = useId();
  const filterId = useId();
  const [typeFilter, setTypeFilter] = useState<DriverActivityType | "">("");
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<DriverActivityItem[]>([]);
  const [totalPages, setTotalPages] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Only the latest request may update state, so a slow earlier response
  // (an old page or filter) can't overwrite a newer one.
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    const params: DriverActivityParams = { page, size: PAGE_SIZE };
    if (typeFilter) params.type = typeFilter;
    try {
      const res = await getJobDriverActivity(jobId, params);
      if (seq !== requestSeq.current) return;
      setItems(res.data ?? []);
      setTotalPages(Math.min(res.pagination?.total_pages ?? 0, MAX_PAGES));
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setItems([]);
      setError(
        err instanceof Error && err.message
          ? err.message
          : extractApiErrorMessage(err, "Couldn't load driver activity"),
      );
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [jobId, page, typeFilter]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section
      aria-labelledby={headingId}
      className="bg-white border border-gray-100 rounded-xl overflow-hidden"
    >
      <div className="px-6 py-4 border-b border-gray-100 flex flex-wrap items-center justify-between gap-3">
        <h2
          id={headingId}
          className="text-sm font-medium text-gray-600 uppercase tracking-wider"
        >
          Driver activity
        </h2>
        <div className="flex items-center gap-2">
          <label htmlFor={filterId} className="text-xs text-gray-600">
            Activity type
          </label>
          <select
            id={filterId}
            value={typeFilter}
            onChange={(e) => {
              setTypeFilter(e.target.value as DriverActivityType | "");
              setPage(1);
            }}
            className="text-sm border border-gray-200 rounded-md px-2 py-1 bg-white"
          >
            <option value="">All</option>
            <option value="message">Messages</option>
            <option value="exception">Exceptions</option>
          </select>
        </div>
      </div>

      <div className="px-6 py-4">
        {loading ? (
          <div role="status" className="py-6 text-center text-sm text-gray-500">
            Loading driver activity…
          </div>
        ) : error ? (
          <div
            role="alert"
            className="bg-error-light border border-error-light text-error-dark p-3 rounded text-sm"
          >
            <p>{error}</p>
            <button
              type="button"
              onClick={load}
              className="mt-2 text-sm font-medium underline hover:no-underline"
            >
              Try again
            </button>
          </div>
        ) : items.length === 0 ? (
          <div className="text-center py-6 text-gray-500">
            <MessageSquare
              className="w-8 h-8 mx-auto mb-2 opacity-50"
              aria-hidden="true"
            />
            <p className="text-sm">
              No driver messages or exceptions for this job
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 uppercase tracking-wider">
                  <th scope="col" className="py-2 pr-4 font-medium">
                    Time
                  </th>
                  <th scope="col" className="py-2 pr-4 font-medium">
                    Driver
                  </th>
                  <th scope="col" className="py-2 pr-4 font-medium">
                    Type
                  </th>
                  <th scope="col" className="py-2 font-medium">
                    Details
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr
                    key={`${item.type}-${item.id}`}
                    className="border-t border-gray-100 align-top"
                  >
                    <td className="py-2 pr-4 whitespace-nowrap text-gray-600">
                      {item.timestamp ? (
                        <time dateTime={item.timestamp}>
                          {formatTimestamp(item.timestamp)}
                        </time>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="py-2 pr-4 whitespace-nowrap text-gray-700">
                      {item.driver_id || "—"}
                    </td>
                    <td className="py-2 pr-4 whitespace-nowrap text-gray-700">
                      {typeLabel(item)}
                    </td>
                    <td className="py-2 text-gray-900">{item.text || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {!error && totalPages > 1 && (
          <nav
            aria-label="Driver activity pages"
            className="flex items-center justify-end gap-3 mt-4 text-sm"
          >
            <button
              type="button"
              aria-label="Previous page"
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={loading || page <= 1}
              className="px-3 py-1 rounded-md border border-gray-200 disabled:opacity-50"
            >
              Previous
            </button>
            <span className="text-gray-600">
              Page {page} of {totalPages}
            </span>
            <button
              type="button"
              aria-label="Next page"
              onClick={() => setPage((p) => p + 1)}
              disabled={loading || page >= totalPages}
              className="px-3 py-1 rounded-md border border-gray-200 disabled:opacity-50"
            >
              Next
            </button>
          </nav>
        )}
      </div>
    </section>
  );
}
