"use client";

/** Invoices: list, status and date filters, CSV download (R5.1, R5.4, R5.6). */

import { useCallback, useId, useState } from "react";
import InvoiceTable from "../../../components/portal/InvoiceTable";
import LiveRegion from "../../../components/portal/LiveRegion";
import { INVOICES_UNAVAILABLE_MESSAGE } from "../../../components/portal/messages";
import {
  PortalLoadError,
  PortalLoading,
} from "../../../components/portal/PageState";
import { usePortalMe } from "../../../components/portal/PortalContext";
import {
  fieldInput,
  fieldLabel,
  pageHeading,
  secondaryButton,
} from "../../../components/portal/styles";
import { usePagedList } from "../../../components/portal/usePagedList";
import { portalErrorMessage } from "../../../components/portal/usePortalData";
import {
  downloadPortalInvoicesCsv,
  isRateLimited,
  listPortalInvoices,
  type PortalInvoiceStatus,
  rateLimitMessage,
} from "../../../services/portalApi";

const STATUS_OPTIONS: Array<{
  value: PortalInvoiceStatus | "";
  label: string;
}> = [
  { value: "", label: "All statuses" },
  { value: "open", label: "Open" },
  { value: "partial", label: "Partially paid" },
  { value: "overdue", label: "Overdue" },
  { value: "paid", label: "Paid" },
  { value: "void", label: "Void" },
];

export default function PortalInvoicesPage() {
  const me = usePortalMe();
  const uid = useId();
  const [status, setStatus] = useState<PortalInvoiceStatus | "">("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [downloadMessage, setDownloadMessage] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);

  const filters = {
    status: status || undefined,
    start_date: startDate || undefined,
    end_date: endDate || undefined,
  };
  const fetchPage = useCallback(
    (cursor: string | null) =>
      listPortalInvoices({
        status: status || undefined,
        start_date: startDate || undefined,
        end_date: endDate || undefined,
        cursor,
        limit: 25,
      }),
    [status, startDate, endDate],
  );
  const list = usePagedList(fetchPage, [fetchPage]);

  if (!me.invoices_available) {
    return (
      <div className="space-y-3">
        <h1 className={pageHeading}>Your invoices</h1>
        <p className="text-sm text-gray-800">{INVOICES_UNAVAILABLE_MESSAGE}</p>
      </div>
    );
  }

  const handleDownload = async () => {
    if (downloading) return;
    setDownloading(true);
    setDownloadMessage("Preparing your CSV…");
    try {
      await downloadPortalInvoicesCsv(filters);
      setDownloadMessage("CSV downloaded.");
    } catch (error) {
      setDownloadMessage(
        isRateLimited(error)
          ? rateLimitMessage(error)
          : "The CSV didn't download. Please try again.",
      );
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className={pageHeading}>Your invoices</h1>
        <button
          type="button"
          className={secondaryButton}
          onClick={handleDownload}
          aria-disabled={downloading ? true : undefined}
        >
          Download CSV
        </button>
      </div>
      <LiveRegion message={downloadMessage} />

      <fieldset className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <legend className="sr-only">Filter invoices</legend>
        <div>
          <label htmlFor={`${uid}-status`} className={fieldLabel}>
            Status
          </label>
          <select
            id={`${uid}-status`}
            value={status}
            onChange={(e) =>
              setStatus(e.target.value as PortalInvoiceStatus | "")
            }
            className={fieldInput}
          >
            {STATUS_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor={`${uid}-from`} className={fieldLabel}>
            Created from
          </label>
          <input
            id={`${uid}-from`}
            type="date"
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
            className={fieldInput}
          />
        </div>
        <div>
          <label htmlFor={`${uid}-to`} className={fieldLabel}>
            Created to
          </label>
          <input
            id={`${uid}-to`}
            type="date"
            value={endDate}
            onChange={(e) => setEndDate(e.target.value)}
            className={fieldInput}
          />
        </div>
      </fieldset>

      {list.loading ? (
        <PortalLoading label="Loading your invoices…" />
      ) : list.error && list.items.length === 0 ? (
        <PortalLoadError
          message={portalErrorMessage(list.error, {
            fallback: "We couldn't load your invoices.",
          })}
          onRetry={list.reload}
        />
      ) : (
        <InvoiceTable invoices={list.items} />
      )}

      {Boolean(list.error) && list.items.length > 0 && (
        <p role="alert" className="text-sm text-gray-900">
          {portalErrorMessage(list.error, {
            fallback: "We couldn't load more invoices.",
          })}
        </p>
      )}
      {list.hasMore && !list.loading && (
        <button
          type="button"
          className={secondaryButton}
          onClick={list.loadMore}
          aria-disabled={list.loadingMore ? true : undefined}
        >
          {list.loadingMore ? "Loading…" : "Show more invoices"}
        </button>
      )}
    </div>
  );
}
