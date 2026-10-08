"use client";

/**
 * Invoices (R5.1, R5.4, R5.6, R14.9, R14.10): status chips in one row
 * (horizontal scroll on phones), a "Dates" control that opens a popover (a
 * bottom sheet below 640 px), CSV in the title row from 768 px or in "⋯" on
 * phones, then the list.
 */
import { CalendarRange, Download, Ellipsis, ReceiptText } from "lucide-react";
import { useCallback, useId, useState } from "react";
import InvoiceTable from "../../../components/portal/InvoiceTable";
import LiveRegion from "../../../components/portal/LiveRegion";
import { INVOICES_UNAVAILABLE_MESSAGE } from "../../../components/portal/messages";
import {
  PortalBanner,
  PortalEmpty,
  PortalLoading,
  PortalSectionError,
} from "../../../components/portal/PageState";
import { usePortalMe } from "../../../components/portal/PortalContext";
import PortalPopover from "../../../components/portal/PortalPopover";
import PortalTitleRow from "../../../components/portal/PortalTitleRow";
import { date as formatDate } from "../../../components/portal/portalFormat";
import {
  fieldInput,
  fieldLabel,
  focusRing,
  listSection,
  primaryButton,
  secondaryButton,
  space,
} from "../../../components/portal/styles";
import {
  PORTAL_TABS_IN_TOP_BAR,
  useMediaQuery,
} from "../../../components/portal/useMediaQuery";
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
  { value: "", label: "All" },
  { value: "open", label: "Open" },
  { value: "partial", label: "Partially paid" },
  { value: "overdue", label: "Overdue" },
  { value: "paid", label: "Paid" },
  { value: "void", label: "Void" },
];

const chip = `inline-flex h-11 shrink-0 items-center whitespace-nowrap rounded-full border px-3.5 text-sm font-semibold md:h-9 ${focusRing}`;

function datesLabel(start: string, end: string): string {
  if (!start && !end) return "Dates";
  if (start && end) return `${formatDate(start)} – ${formatDate(end)}`;
  return start ? `From ${formatDate(start)}` : `Until ${formatDate(end)}`;
}

export default function PortalInvoicesPage() {
  const me = usePortalMe();
  const uid = useId();
  const wide = useMediaQuery(PORTAL_TABS_IN_TOP_BAR);
  const [status, setStatus] = useState<PortalInvoiceStatus | "">("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [draft, setDraft] = useState({ start: "", end: "" });
  const [downloadMessage, setDownloadMessage] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);

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
      <>
        <PortalTitleRow title="Invoices" />
        <div data-portal-first>
          <PortalBanner tone="info">
            {INVOICES_UNAVAILABLE_MESSAGE}
          </PortalBanner>
        </div>
      </>
    );
  }

  const handleDownload = async () => {
    if (downloading) return;
    setDownloading(true);
    setDownloadMessage("Preparing your CSV…");
    try {
      await downloadPortalInvoicesCsv({
        status: status || undefined,
        start_date: startDate || undefined,
        end_date: endDate || undefined,
      });
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

  const csvAction = wide ? (
    <button
      type="button"
      className={secondaryButton}
      onClick={() => void handleDownload()}
      aria-disabled={downloading ? true : undefined}
    >
      <Download aria-hidden="true" className="h-4 w-4" />
      Download CSV
    </button>
  ) : (
    <PortalPopover
      label="More actions"
      panelLabel="More actions"
      buttonContent={<Ellipsis aria-hidden="true" className="h-5 w-5" />}
      buttonClassName={`inline-flex h-11 w-11 items-center justify-center rounded-full text-slate-700 hover:bg-slate-100 ${focusRing}`}
    >
      {(close) => (
        <button
          type="button"
          className={`${secondaryButton} w-full justify-start border-transparent`}
          aria-disabled={downloading ? true : undefined}
          onClick={() => {
            close();
            void handleDownload();
          }}
        >
          <Download aria-hidden="true" className="h-4 w-4" />
          Download CSV
        </button>
      )}
    </PortalPopover>
  );

  const datesSet = Boolean(startDate || endDate);

  return (
    <>
      <PortalTitleRow title="Invoices" action={csvAction} />
      <LiveRegion message={downloadMessage} className="mb-2" />
      {/* The filter row is this page's first content block (design §11.2). */}
      <div
        data-portal-first
        className="-mx-5 mb-2 flex min-h-11 items-center gap-2 overflow-x-auto px-5 pb-1 md:mx-0 md:px-0"
      >
        <div
          role="group"
          aria-label="Invoice status"
          className="flex items-center gap-1.5"
        >
          {STATUS_OPTIONS.map((o) => {
            const on = status === o.value;
            return (
              <button
                key={o.value || "all"}
                type="button"
                aria-pressed={on}
                onClick={() => setStatus(o.value)}
                className={`${chip} ${
                  on
                    ? "border-primary bg-primary text-on-primary"
                    : "border-slate-300 bg-surface text-slate-800 hover:bg-slate-50"
                }`}
              >
                {o.label}
              </button>
            );
          })}
        </div>
        <PortalPopover
          sheet
          panelLabel="Filter by date created"
          buttonContent={
            <>
              <CalendarRange aria-hidden="true" className="h-4 w-4" />
              {datesLabel(startDate, endDate)}
            </>
          }
          buttonClassName={`${chip} gap-1.5 ${
            datesSet
              ? "border-primary bg-primary-soft text-brand-800"
              : "border-slate-300 bg-surface text-slate-800 hover:bg-slate-50"
          }`}
        >
          {(close) => (
            <form
              className="space-y-3"
              onSubmit={(e) => {
                e.preventDefault();
                setStartDate(draft.start);
                setEndDate(draft.end);
                close();
              }}
            >
              <p className="text-sm font-semibold text-text">Date created</p>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label htmlFor={`${uid}-from`} className={fieldLabel}>
                    From
                  </label>
                  <input
                    id={`${uid}-from`}
                    type="date"
                    value={draft.start}
                    onChange={(e) =>
                      setDraft((d) => ({ ...d, start: e.target.value }))
                    }
                    className={`${fieldInput} mt-1`}
                  />
                </div>
                <div>
                  <label htmlFor={`${uid}-to`} className={fieldLabel}>
                    To
                  </label>
                  <input
                    id={`${uid}-to`}
                    type="date"
                    value={draft.end}
                    onChange={(e) =>
                      setDraft((d) => ({ ...d, end: e.target.value }))
                    }
                    className={`${fieldInput} mt-1`}
                  />
                </div>
              </div>
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  className={secondaryButton}
                  onClick={() => {
                    setDraft({ start: "", end: "" });
                    setStartDate("");
                    setEndDate("");
                    close();
                  }}
                >
                  Clear
                </button>
                <button type="submit" className={primaryButton}>
                  Apply
                </button>
              </div>
            </form>
          )}
        </PortalPopover>
      </div>

      {list.loading ? (
        <div className={`${listSection} ${space.inset}`}>
          <PortalLoading label="Loading your invoices…" rows={5} />
        </div>
      ) : list.error && list.items.length === 0 ? (
        <div className={`${listSection} ${space.inset}`}>
          <PortalSectionError
            message={portalErrorMessage(list.error, {
              fallback: "We couldn't load your invoices.",
            })}
            onRetry={list.reload}
          />
        </div>
      ) : list.items.length === 0 ? (
        <div className={listSection}>
          <PortalEmpty
            icon={<ReceiptText className="h-8 w-8" />}
            title="No invoices to show."
            description={
              status || datesSet
                ? "Try another status or date range."
                : `Invoices from ${me.supplier_name} will show here.`
            }
          />
        </div>
      ) : (
        <div className={`${listSection} md:overflow-clip`}>
          <InvoiceTable invoices={list.items} caption="Your invoices" />
        </div>
      )}
      {Boolean(list.error) && list.items.length > 0 && (
        <PortalBanner tone="critical" className="mt-3">
          {portalErrorMessage(list.error, {
            fallback: "We couldn't load more invoices.",
          })}
        </PortalBanner>
      )}
      {list.hasMore && !list.loading && (
        <div className="mt-3 flex justify-center">
          <button
            type="button"
            className={secondaryButton}
            onClick={list.loadMore}
            aria-disabled={list.loadingMore ? true : undefined}
          >
            {list.loadingMore ? "Loading…" : "Show more invoices"}
          </button>
        </div>
      )}
    </>
  );
}
