"use client";

/**
 * Request a delivery (D24, D25, R14.11, R14.12, design §11.6): the owner's
 * create-dialog pattern, md (560 px), and a full-screen sheet below 640 px.
 *
 * Built on the shared `Modal` (focus trap, Escape, focus return) with
 * FormDialog's look (title + ×, divider, labelled single column, two-column
 * From/To, Cancel as text + filled primary, discard confirm on a dirty form).
 * It doesn't use `FormDialog` itself because the shared component has no
 * `mobile="sheet"`, no `aria-disabled` submit and closes on success, while
 * this dialog keeps the "Request sent" confirmation inside (follow-up: add
 * those to `FormDialog`, design §11.3).
 */
import { Fuel } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { PortalOrder, PortalTank } from "../../services/portalApi";
import { Modal } from "../ui/Modal";
import { NumberField } from "../ui/NumberField";
import { ORDERING_UNAVAILABLE_MESSAGE } from "./messages";
import {
  PortalBanner,
  PortalEmpty,
  PortalLoading,
  PortalSectionError,
} from "./PageState";
import PortalStatus from "./PortalStatus";
import { number, zoneLongName } from "./portalFormat";
import {
  fieldError,
  fieldHint,
  fieldInput,
  fieldLabel,
  primaryButton,
  secondaryButton,
} from "./styles";
import TankPicker from "./TankPicker";
import { PORTAL_PHONE, useMediaQuery } from "./useMediaQuery";
import {
  FIELD_ORDER,
  MAX_DAYS_AHEAD,
  maxGallons,
  minGallons,
  NOTES_MAX,
  PO_MAX,
  type RequestErrors,
  type RequestField,
  useOrderRequest,
} from "./useOrderRequest";

export { ORDERING_UNAVAILABLE_MESSAGE };

/** "Between 25 and 1,700 gal." with the room left at a fresh reading. */
export function gallonsHint(tank: PortalTank, unit = "gal"): string {
  const max = maxGallons(tank);
  if (max < minGallons(tank)) {
    return "The tank is nearly full at its latest reading. Choose Fill to full.";
  }
  const room = max < tank.capacity_gallons;
  const range = `Between ${number(minGallons(tank))} and ${number(max)} ${unit}`;
  return room ? `${range}, the room left at the latest reading.` : `${range}.`;
}

export interface RequestDeliveryDialogProps {
  open: boolean;
  onClose: () => void;
  tanks: PortalTank[];
  titles: Map<string, string>;
  tanksLoading?: boolean;
  tanksError?: string | null;
  onRetryTanks?: () => void;
  orderingAvailable: boolean;
  supplierName: string;
  unit?: string;
  /** Preselected tank (a tank row's button or `?tank=`). */
  initialTankId?: string | null;
  /** A request was sent; the page refreshes its lists. */
  onCreated?: (order: PortalOrder) => void;
}

export default function RequestDeliveryDialog({
  open,
  onClose,
  tanks,
  titles,
  tanksLoading = false,
  tanksError = null,
  onRetryTanks,
  orderingAvailable,
  supplierName,
  unit = "gal",
  initialTankId,
  onCreated,
}: RequestDeliveryDialogProps) {
  const uid = useId();
  const phone = useMediaQuery(PORTAL_PHONE);
  const req = useOrderRequest({
    tanks,
    orderingAvailable,
    onTankGone: onRetryTanks,
  });
  const { reset } = req;
  const [confirming, setConfirming] = useState(false);
  const summaryRef = useRef<HTMLDivElement>(null);
  const createdRef = useRef<HTMLHeadingElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const keepRef = useRef<HTMLButtonElement>(null);
  const reported = useRef<string | null>(null);

  const firstTankId = tanks[0]?.customer_tank_id ?? "";
  const startTank =
    initialTankId && tanks.some((t) => t.customer_tank_id === initialTankId)
      ? initialTankId
      : firstTankId;

  // A fresh request every time the dialog opens (new client_event_id). When
  // the tanks arrive after opening (a deep link), the untouched request picks
  // up the preselected tank.
  const wasOpen = useRef(false);
  const untouched = !req.dirty && req.values.tankId === "";
  useEffect(() => {
    const justOpened = open && !wasOpen.current;
    wasOpen.current = open;
    if (justOpened) {
      reset(startTank);
      setConfirming(false);
      reported.current = null;
    } else if (open && untouched && startTank) {
      reset(startTank);
    }
  }, [open, startTank, reset, untouched]);

  // Start in the selected tank (Modal focuses the × first).
  useEffect(() => {
    if (!open || tanksLoading) return;
    const t = setTimeout(() => {
      const body = bodyRef.current;
      (
        body?.querySelector<HTMLElement>(
          'input[type="radio"]:checked:not([disabled])',
        ) ??
        body?.querySelector<HTMLElement>(
          "input:not([disabled]), textarea:not([disabled])",
        )
      )?.focus();
    }, 0);
    return () => clearTimeout(t);
  }, [open, tanksLoading]);

  useEffect(() => {
    if (confirming) keepRef.current?.focus();
  }, [confirming]);

  // Report a created order once, and move focus to the confirmation.
  useEffect(() => {
    if (req.created && reported.current !== req.created.order_id) {
      reported.current = req.created.order_id;
      onCreated?.(req.created);
      setTimeout(() => createdRef.current?.focus(), 0);
    }
  }, [req.created, onCreated]);

  const stateRef = useRef({
    dirty: req.dirty,
    confirming,
    submitting: req.submitting,
    onClose,
  });
  stateRef.current = {
    dirty: req.dirty,
    confirming,
    submitting: req.submitting,
    onClose,
  };
  const requestClose = useMemo(
    () => () => {
      const s = stateRef.current;
      if (s.submitting) return;
      if (s.confirming) {
        setConfirming(false);
        return;
      }
      if (s.dirty) {
        setConfirming(true);
        return;
      }
      s.onClose();
    },
    [],
  );

  const ids = {
    tankLabel: `${uid}-tank-label`,
    fill: `${uid}-fill`,
    gallonsMode: `${uid}-gallons-mode`,
    gallons: `${uid}-gallons`,
    date: `${uid}-date`,
    start: `${uid}-start`,
    end: `${uid}-end`,
    po: `${uid}-po`,
    notes: `${uid}-notes`,
    form: `${uid}-form`,
  };
  const errorId = (f: RequestField) => `${uid}-${f}-error`;
  const describe = (f: RequestField, hint?: string) =>
    [hint, req.errors[f] ? errorId(f) : undefined].filter(Boolean).join(" ") ||
    undefined;
  const anchorFor: Record<RequestField, string> = {
    form: `${ids.form}-tank-${req.values.tankId || firstTankId}`,
    tank: `${ids.form}-tank-${req.values.tankId || firstTankId}`,
    gallons: ids.gallons,
    date: ids.date,
    time: ids.start,
    po_number: ids.po,
    notes: ids.notes,
  };

  const showErrors = (errs: RequestErrors) => {
    if (Object.keys(errs).length > 0) {
      setTimeout(() => summaryRef.current?.focus(), 0);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (req.disabled) return;
    showErrors(await req.submit());
  };

  const statusMessage = req.unavailable
    ? ORDERING_UNAVAILABLE_MESSAGE
    : req.notice;
  const errorEntries = FIELD_ORDER.filter((f) => req.errors[f]).map(
    (f) => [f, req.errors[f] as string] as const,
  );
  const fieldsDisabled = req.unavailable;
  const noTanks = !tanksLoading && !tanksError && tanks.length === 0;
  const showForm = !tanksLoading && !tanksError && !noTanks && !req.created;

  const footer = req.created ? (
    <button
      type="button"
      className={`${primaryButton} max-sm:flex-1`}
      onClick={onClose}
    >
      Close
    </button>
  ) : (
    <>
      <button
        type="button"
        className={`${secondaryButton} border-transparent bg-transparent max-sm:flex-1`}
        onClick={requestClose}
        disabled={req.submitting}
      >
        Cancel
      </button>
      {showForm && (
        <button
          type="submit"
          form={ids.form}
          aria-disabled={req.disabled ? true : undefined}
          className={`${primaryButton} max-sm:flex-1`}
        >
          {req.submitting ? "Sending request…" : "Send request"}
        </button>
      )}
    </>
  );

  const overlay = confirming ? (
    <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/80 sm:rounded-xl">
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={`${uid}-discard`}
        className="w-72 rounded-xl border border-border bg-surface p-4 shadow-lg"
      >
        <p
          id={`${uid}-discard`}
          className="text-[15px] font-semibold text-text"
        >
          Discard this request?
        </p>
        <p className="mt-1 text-sm text-text-muted">
          What you entered will be lost.
        </p>
        <div className="mt-3 flex justify-end gap-2">
          <button
            ref={keepRef}
            type="button"
            className={secondaryButton}
            onClick={() => setConfirming(false)}
          >
            Keep editing
          </button>
          <button
            type="button"
            className={`${primaryButton} border-red-600 bg-red-600 hover:bg-red-700`}
            onClick={() => {
              setConfirming(false);
              onClose();
            }}
          >
            Discard
          </button>
        </div>
      </div>
    </div>
  ) : null;

  return (
    <Modal
      isOpen={open}
      onClose={requestClose}
      title="Request a delivery"
      size="custom"
      closeLabel="Close"
      className={`max-h-[calc(100vh-32px)] sm:max-w-[560px] max-sm:mx-0 max-sm:h-[100dvh] max-sm:max-h-none max-sm:rounded-none`}
      bodyClassName="flex min-h-0 flex-1 flex-col"
      footer={footer}
      overlay={overlay}
    >
      {/* The scrolling body. While every field is disabled it has nothing
          focusable, so it takes focus itself (axe scrollable-region-focusable). */}
      <div
        ref={bodyRef}
        data-sheet={phone || undefined}
        tabIndex={fieldsDisabled ? 0 : undefined}
        role={fieldsDisabled ? "region" : undefined}
        aria-label={fieldsDisabled ? "Request details" : undefined}
        className="min-h-0 flex-1 overflow-y-auto px-5 py-4 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus"
      >
        {/* Always mounted so the PD10 and 429 texts are announced (R4.6). */}
        <div
          role="status"
          aria-live="polite"
          aria-atomic="true"
          className={statusMessage ? "mb-3" : "sr-only"}
        >
          {statusMessage ? (
            <PortalBanner
              tone={req.unavailable ? "warning" : "info"}
              live={false}
            >
              {statusMessage}
            </PortalBanner>
          ) : null}
        </div>

        {!req.created && tanksLoading && (
          <PortalLoading label="Loading your tanks…" rows={3} />
        )}
        {!req.created && tanksError && (
          <PortalSectionError message={tanksError} onRetry={onRetryTanks} />
        )}
        {!req.created && noTanks && (
          <PortalEmpty
            icon={<Fuel className="h-8 w-8" />}
            title="No tanks are set up yet."
            description={`Contact ${supplierName} to add a tank for online ordering.`}
          />
        )}

        {req.created && (
          <section
            aria-labelledby={`${uid}-created`}
            className="space-y-3 py-2"
          >
            <h3
              id={`${uid}-created`}
              ref={createdRef}
              tabIndex={-1}
              className="text-lg font-bold text-text focus:outline-none"
            >
              Request sent
            </h3>
            <p className="flex flex-wrap items-center gap-2 text-[15px] text-text">
              Status:
              <PortalStatus
                kind="order"
                code={req.created.status_code}
                label={req.created.status_label}
              />
            </p>
            <p className="text-[15px] text-text">
              {supplierName} will confirm the delivery. You can follow it under
              Orders.
            </p>
          </section>
        )}

        {showForm && (
          <form
            id={ids.form}
            noValidate
            onSubmit={handleSubmit}
            className="space-y-4"
          >
            {errorEntries.length > 0 && (
              <div
                ref={summaryRef}
                role="alert"
                tabIndex={-1}
                className="rounded-lg border border-red-300 bg-red-50 px-3 py-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-700"
              >
                <p className="text-sm font-semibold text-red-800">
                  Please fix the following:
                </p>
                <ul className="mt-1 list-disc pl-5 text-sm text-red-800">
                  {errorEntries.map(([field, message]) => (
                    <li key={field}>
                      <a href={`#${anchorFor[field]}`} className="underline">
                        {message}
                      </a>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div>
              <p id={ids.tankLabel} className={fieldLabel}>
                Tank
              </p>
              <div className="mt-1.5">
                <TankPicker
                  tanks={tanks}
                  titles={titles}
                  value={req.values.tankId}
                  onChange={(id) => req.set("tankId", id)}
                  name={`${ids.form}-tank`}
                  labelledBy={ids.tankLabel}
                  describedBy={req.errors.tank ? errorId("tank") : undefined}
                  invalid={Boolean(req.errors.tank)}
                  disabled={fieldsDisabled}
                  unit={unit}
                />
              </div>
              {req.errors.tank && (
                <p id={errorId("tank")} className={fieldError}>
                  {req.errors.tank}
                </p>
              )}
            </div>

            <fieldset disabled={fieldsDisabled}>
              <legend className={fieldLabel}>Quantity</legend>
              <div className="mt-1.5 flex flex-wrap gap-2">
                {(
                  [
                    ["fill_to_full", ids.fill, "Fill to full"],
                    ["gallons", ids.gallonsMode, "Gallons"],
                  ] as const
                ).map(([mode, id, label]) => (
                  <label
                    key={mode}
                    htmlFor={id}
                    className={`inline-flex h-11 items-center gap-2 rounded-[10px] border px-3 text-[15px] font-medium text-text md:h-10 ${
                      req.values.mode === mode
                        ? "border-primary bg-primary-soft"
                        : "border-slate-300 bg-surface"
                    }`}
                  >
                    <input
                      id={id}
                      type="radio"
                      name={`${uid}-mode`}
                      value={mode}
                      checked={req.values.mode === mode}
                      onChange={() => req.set("mode", mode)}
                      className="h-5 w-5 accent-[var(--rs-primary)]"
                    />
                    {label}
                  </label>
                ))}
              </div>
              {req.values.mode === "gallons" && (
                <div className="mt-3">
                  <label htmlFor={ids.gallons} className={fieldLabel}>
                    Gallons requested
                  </label>
                  <div className="mt-1">
                    <NumberField
                      id={ids.gallons}
                      value={req.values.gallons}
                      onChange={(v) => req.set("gallons", v)}
                      unit={unit}
                      decimals={0}
                      min={minGallons(req.tank)}
                      max={req.tank ? maxGallons(req.tank) : undefined}
                      disabled={fieldsDisabled}
                      aria-invalid={req.errors.gallons ? true : undefined}
                      aria-describedby={describe(
                        "gallons",
                        req.tank ? `${ids.gallons}-hint` : undefined,
                      )}
                      className="!h-11 !rounded-[10px] !border-slate-400 !text-base"
                    />
                  </div>
                  {req.tank && (
                    <p id={`${ids.gallons}-hint`} className={fieldHint}>
                      {gallonsHint(req.tank, unit)}
                    </p>
                  )}
                  {req.errors.gallons && (
                    <p id={errorId("gallons")} className={fieldError}>
                      {req.errors.gallons}
                    </p>
                  )}
                </div>
              )}
            </fieldset>

            <div>
              <label htmlFor={ids.date} className={fieldLabel}>
                Delivery date
              </label>
              <div className="mt-1">
                <input
                  id={ids.date}
                  type="date"
                  min={req.minDate}
                  max={req.maxDate}
                  value={req.values.date}
                  onChange={(e) => req.set("date", e.target.value)}
                  disabled={fieldsDisabled}
                  aria-invalid={req.errors.date ? true : undefined}
                  aria-describedby={describe("date", `${ids.date}-hint`)}
                  className={fieldInput}
                />
              </div>
              <p id={`${ids.date}-hint`} className={fieldHint}>
                From today up to {MAX_DAYS_AHEAD} days ahead.
              </p>
              {req.errors.date && (
                <p id={errorId("date")} className={fieldError}>
                  {req.errors.date}
                </p>
              )}
            </div>

            <fieldset disabled={fieldsDisabled}>
              <legend className={fieldLabel}>Delivery window (optional)</legend>
              <p id={`${ids.start}-hint`} className={fieldHint}>
                Leave both empty for any time that day. Times are{" "}
                {zoneLongName() || "local time"}.
              </p>
              <div className="mt-1.5 grid grid-cols-2 gap-3">
                <div>
                  <label
                    htmlFor={ids.start}
                    className="text-sm text-text-muted"
                  >
                    From
                  </label>
                  <input
                    id={ids.start}
                    type="time"
                    value={req.values.startTime}
                    onChange={(e) => req.set("startTime", e.target.value)}
                    aria-invalid={req.errors.time ? true : undefined}
                    aria-describedby={describe("time", `${ids.start}-hint`)}
                    className={`${fieldInput} mt-1`}
                  />
                </div>
                <div>
                  <label htmlFor={ids.end} className="text-sm text-text-muted">
                    To
                  </label>
                  <input
                    id={ids.end}
                    type="time"
                    value={req.values.endTime}
                    onChange={(e) => req.set("endTime", e.target.value)}
                    aria-invalid={req.errors.time ? true : undefined}
                    aria-describedby={describe("time", `${ids.start}-hint`)}
                    className={`${fieldInput} mt-1`}
                  />
                </div>
              </div>
              {req.errors.time && (
                <p id={errorId("time")} className={fieldError}>
                  {req.errors.time}
                </p>
              )}
            </fieldset>

            <div>
              <label htmlFor={ids.po} className={fieldLabel}>
                PO number (optional)
              </label>
              <input
                id={ids.po}
                type="text"
                maxLength={PO_MAX}
                value={req.values.poNumber}
                onChange={(e) => req.set("poNumber", e.target.value)}
                disabled={fieldsDisabled}
                aria-invalid={req.errors.po_number ? true : undefined}
                aria-describedby={describe("po_number")}
                className={`${fieldInput} mt-1`}
              />
              {req.errors.po_number && (
                <p id={errorId("po_number")} className={fieldError}>
                  {req.errors.po_number}
                </p>
              )}
            </div>

            <div>
              <label htmlFor={ids.notes} className={fieldLabel}>
                Notes (optional)
              </label>
              <textarea
                id={ids.notes}
                rows={3}
                maxLength={NOTES_MAX}
                value={req.values.notes}
                onChange={(e) => req.set("notes", e.target.value)}
                disabled={fieldsDisabled}
                aria-invalid={req.errors.notes ? true : undefined}
                aria-describedby={describe("notes", `${ids.notes}-hint`)}
                className={`${fieldInput} mt-1 h-auto min-h-24 py-2`}
              />
              <p id={`${ids.notes}-hint`} className={fieldHint}>
                {req.values.notes.length} of {NOTES_MAX} characters.
              </p>
              {req.errors.notes && (
                <p id={errorId("notes")} className={fieldError}>
                  {req.errors.notes}
                </p>
              )}
            </div>
          </form>
        )}
      </div>
    </Modal>
  );
}
