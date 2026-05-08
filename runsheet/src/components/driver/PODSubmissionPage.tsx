"use client";

/**
 * Driver Proof-of-Delivery submission page.
 *
 * Implements task 11.4 of the Fuel Ops Hardening spec: drivers collect
 * the recipient name, signature, delivery photos, optional meter-ticket
 * image, geotag, and optional OTP, upload every artifact via presigned
 * S3 URLs returned by `POST /api/driver/pod/uploads/presign`, and then
 * submit the POD to `POST /api/driver/jobs/{job_id}/pod`.
 *
 * The flow purposefully keeps the three responsibilities separate so
 * failures surface in the right place:
 *
 *  1. **Presign + upload** — each selected file is paired with a
 *     per-file status (idle → presigning → uploading → uploaded / failed)
 *     so the driver can see exactly which photo is slow or broken and
 *     can retry that one file without re-selecting the others.
 *  2. **Submit POD** — only runs once every required artifact has a
 *     `file_ref`. Disabled until pre-conditions hold.
 *  3. **OCR confirmation** — the backend response includes either an
 *     OCR-populated `delivered_gallons` (`delivered_gallons_source="ocr"`)
 *     or flags the POD for manual review. The modal supports both
 *     shapes: if the server returns OCR metadata inline we honor it
 *     directly; otherwise we conservatively prompt whenever a
 *     meter-ticket was uploaded without a driver-entered gallon count
 *     (Req 4.2.5). The driver can accept the extracted value or
 *     override it; the override is re-submitted with
 *     `delivered_gallons` set so the server persists it with
 *     `delivered_gallons_source="manual"`.
 *
 * Styling follows the ops pages under `runsheet/src/components/ops/`
 * (Tailwind utility classes, `bg-black/30` modal overlays, compact
 * toasts) so the driver UI looks at home in the same shell.
 *
 * Validates: Requirements 4.1.3, 4.2.5.
 */

import {
  AlertTriangle,
  Camera,
  Check,
  FileText,
  Loader2,
  MapPin,
  PenLine,
  Receipt,
  RefreshCw,
  Send,
  Upload,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  ApiError,
  ApiTimeoutError,
} from "../../services/api";
import {
  presignPodUpload,
  putPresignedFile,
  submitPOD,
  type DriverPODRequest,
  type DriverPODResult,
  type PodUploadCategory,
  type PodUploadContentType,
} from "../../services/driverApi";

// ─── Constants ───────────────────────────────────────────────────────────────

/** Map MIME types to the canonical set accepted by the backend. */
const ALLOWED_CONTENT_TYPES = new Set<string>([
  "image/jpeg",
  "image/png",
  "image/heic",
  "application/pdf",
]);

/** Safe fallback for `File.type === ""` (some mobile browsers omit it). */
const DEFAULT_IMAGE_CONTENT_TYPE: PodUploadContentType = "image/jpeg";

/** Default ceiling the UI displays while the server resolves the real one. */
const DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024;

// ─── Types ───────────────────────────────────────────────────────────────────

type UploadStatus = "idle" | "presigning" | "uploading" | "uploaded" | "failed";

/**
 * Per-file upload tracking record. One entry exists per selected file;
 * `fileRef` is populated once the upload succeeds and is what we send to
 * the POD endpoint.
 */
interface UploadSlot {
  /** Stable UI id — NOT persisted server-side. */
  clientId: string;
  /** Category sent to `/pod/uploads/presign`. */
  category: PodUploadCategory;
  /** The underlying File the driver selected. */
  file: File;
  /** Resolved content type sent on the presign + PUT. */
  contentType: PodUploadContentType;
  /** Current stage of the upload lifecycle. */
  status: UploadStatus;
  /** `file_ref` returned by the presign endpoint after a successful PUT. */
  fileRef: string | null;
  /** Short human error message for the failed state. */
  error: string | null;
}

/** Snapshot of OCR metadata captured from the POD submit response. */
interface OcrSnapshot {
  extractedGallons: number | null;
  confidence: number | null;
  requiresManualReview: boolean;
  source: "manual" | "ocr";
  error: string | null;
  podId: string;
}

export interface PODSubmissionPageProps {
  jobId: string;
  /** Optional hook called after a successful POD submission (for navigation). */
  onSubmitted?: (result: DriverPODResult) => void;
}

// ─── Toast system (mirrors CustomerTankPage.tsx) ─────────────────────────────

interface Toast {
  id: number;
  message: string;
  type: "success" | "error" | "info";
}

let toastIdCounter = 0;

function ToastContainer({
  toasts,
  onDismiss,
}: {
  toasts: Toast[];
  onDismiss: (id: number) => void;
}) {
  if (toasts.length === 0) return null;
  return (
    <div className="fixed top-4 right-4 z-[100] space-y-2">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`flex items-center gap-2 px-4 py-3 rounded-lg shadow-lg text-sm font-medium ${
            toast.type === "success"
              ? "bg-green-600 text-white"
              : toast.type === "error"
                ? "bg-red-600 text-white"
                : "bg-slate-700 text-white"
          }`}
        >
          {toast.type === "success" ? (
            <Check className="w-4 h-4" />
          ) : toast.type === "error" ? (
            <AlertTriangle className="w-4 h-4" />
          ) : (
            <RefreshCw className="w-4 h-4" />
          )}
          <span>{toast.message}</span>
          <button
            type="button"
            onClick={() => onDismiss(toast.id)}
            className="ml-2 p-0.5 hover:bg-white/20 rounded"
            aria-label="Dismiss notification"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
      ))}
    </div>
  );
}

function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const addToast = useCallback(
    (message: string, type: "success" | "error" | "info" = "info") => {
      const id = ++toastIdCounter;
      setToasts((prev) => [...prev, { id, message, type }]);
      setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== id));
      }, 4500);
    },
    [],
  );

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return { toasts, addToast, dismissToast };
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function resolveContentType(file: File): PodUploadContentType | null {
  const raw = (file.type || "").toLowerCase();
  if (ALLOWED_CONTENT_TYPES.has(raw)) {
    return raw as PodUploadContentType;
  }
  // Some mobile browsers report an empty `File.type`; fall back based on
  // extension so the upload does not silently fail.
  const name = file.name.toLowerCase();
  if (name.endsWith(".heic")) return "image/heic";
  if (name.endsWith(".png")) return "image/png";
  if (name.endsWith(".pdf")) return "application/pdf";
  if (name.endsWith(".jpg") || name.endsWith(".jpeg")) return "image/jpeg";
  if (!file.type) return DEFAULT_IMAGE_CONTENT_TYPE;
  return null;
}

function humaniseError(error: unknown): string {
  if (error instanceof ApiTimeoutError) return "Request timed out";
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return "Cross-tenant file rejected. Please re-capture the file.";
    }
    if (error.status === 400) {
      return error.message || "Invalid upload";
    }
    return error.message || `Request failed (HTTP ${error.status})`;
  }
  if (error instanceof Error) return error.message;
  return "Unexpected error";
}

function nowIso(): string {
  return new Date().toISOString();
}

function nextClientId(prefix: string): string {
  // `crypto.randomUUID` is widely available on modern browsers; fall
  // back to a timestamp + counter for Jest/JSDOM shims where it isn't.
  if (
    typeof globalThis !== "undefined" &&
    typeof globalThis.crypto !== "undefined" &&
    typeof globalThis.crypto.randomUUID === "function"
  ) {
    return `${prefix}-${globalThis.crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// ─── OCR confirmation modal ──────────────────────────────────────────────────

interface OcrConfirmationModalProps {
  snapshot: OcrSnapshot;
  onAccept: (extractedGallons: number) => void;
  onOverride: (driverGallons: number) => void;
  onCancel: () => void;
  isSubmitting: boolean;
}

function OcrConfirmationModal({
  snapshot,
  onAccept,
  onOverride,
  onCancel,
  isSubmitting,
}: OcrConfirmationModalProps) {
  const [override, setOverride] = useState<string>(
    snapshot.extractedGallons != null
      ? snapshot.extractedGallons.toFixed(2)
      : "",
  );
  const overrideNum = Number.parseFloat(override);
  const overrideValid = !Number.isNaN(overrideNum) && overrideNum >= 0;
  const canAccept =
    snapshot.extractedGallons != null && !snapshot.requiresManualReview;
  const confidencePct =
    snapshot.confidence != null ? Math.round(snapshot.confidence * 100) : null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="ocr-confirm-title"
    >
      <div className="w-full max-w-md rounded-2xl bg-white p-5 shadow-xl">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2
              id="ocr-confirm-title"
              className="text-lg font-semibold text-[#232323]"
            >
              Confirm gallon count
            </h2>
            <p className="mt-1 text-xs text-gray-600">
              The meter ticket was processed
              {snapshot.source === "ocr"
                ? " and a value was extracted."
                : " but could not be read with enough confidence."}
            </p>
          </div>
          <button
            type="button"
            onClick={onCancel}
            className="rounded p-1 text-gray-400 hover:bg-gray-100"
            aria-label="Close"
            disabled={isSubmitting}
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="mt-4 space-y-3">
          <div className="rounded-lg border border-gray-200 bg-gray-50 p-3">
            <div className="flex items-center justify-between text-sm">
              <span className="text-gray-600">Extracted gallons</span>
              <span className="font-semibold text-[#232323]">
                {snapshot.extractedGallons != null
                  ? snapshot.extractedGallons.toFixed(2)
                  : "—"}
              </span>
            </div>
            <div className="mt-1 flex items-center justify-between text-xs">
              <span className="text-gray-500">Confidence</span>
              <span className="text-gray-700">
                {confidencePct != null ? `${confidencePct}%` : "n/a"}
              </span>
            </div>
            {snapshot.requiresManualReview && (
              <div className="mt-2 flex items-center gap-2 text-xs text-yellow-700">
                <AlertTriangle className="h-3.5 w-3.5" />
                <span>Manual review required</span>
              </div>
            )}
            {snapshot.error && (
              <div className="mt-2 flex items-center gap-2 text-xs text-red-700">
                <AlertTriangle className="h-3.5 w-3.5" />
                <span>{snapshot.error}</span>
              </div>
            )}
          </div>

          <div>
            <label
              htmlFor="ocr-override"
              className="block text-xs font-medium text-gray-600"
            >
              Driver-entered gallons
            </label>
            <input
              id="ocr-override"
              type="number"
              inputMode="decimal"
              min={0}
              step="0.01"
              value={override}
              onChange={(event) => setOverride(event.target.value)}
              disabled={isSubmitting}
              className="mt-1 block w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-[#0D9373] focus:outline-none focus:ring-1 focus:ring-[#0D9373]"
            />
            <p className="mt-1 text-[11px] text-gray-500">
              Use this if the ticket is unreadable or the extracted value is
              wrong. The server will record the manual value as
              authoritative.
            </p>
          </div>
        </div>

        <div className="mt-5 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={isSubmitting}
            className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => {
              if (snapshot.extractedGallons != null) {
                onAccept(snapshot.extractedGallons);
              }
            }}
            disabled={!canAccept || isSubmitting}
            className="rounded-lg bg-slate-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          >
            Accept OCR value
          </button>
          <button
            type="button"
            onClick={() => {
              if (overrideValid) onOverride(overrideNum);
            }}
            disabled={!overrideValid || isSubmitting}
            className="inline-flex items-center gap-1 rounded-lg bg-[#0D9373] px-3 py-1.5 text-xs font-medium text-white hover:bg-[#0B7F63] disabled:opacity-50"
          >
            {isSubmitting ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Check className="h-3.5 w-3.5" />
            )}
            Submit override
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Main component ──────────────────────────────────────────────────────────

export default function PODSubmissionPage({
  jobId,
  onSubmitted,
}: PODSubmissionPageProps) {
  const { toasts, addToast, dismissToast } = useToasts();

  // Form fields
  const [recipientName, setRecipientName] = useState("");
  const [otp, setOtp] = useState("");
  const [driverGallons, setDriverGallons] = useState<string>("");

  // Geotag (Geolocation API with manual override)
  const [geotag, setGeotag] = useState<{ lat: string; lng: string }>({
    lat: "",
    lng: "",
  });
  const [geoStatus, setGeoStatus] = useState<
    "idle" | "detecting" | "detected" | "error"
  >("idle");
  const [geoError, setGeoError] = useState<string | null>(null);

  // Upload slots
  const [signatureSlot, setSignatureSlot] = useState<UploadSlot | null>(null);
  const [photoSlots, setPhotoSlots] = useState<UploadSlot[]>([]);
  const [meterTicketSlot, setMeterTicketSlot] = useState<UploadSlot | null>(
    null,
  );
  const [maxFileBytes, setMaxFileBytes] = useState<number>(
    DEFAULT_MAX_FILE_BYTES,
  );

  // Submission state
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [lastResult, setLastResult] = useState<DriverPODResult | null>(null);
  const [ocrSnapshot, setOcrSnapshot] = useState<OcrSnapshot | null>(null);
  const [idempotencyKey, setIdempotencyKey] = useState<string>(() =>
    nextClientId("pod"),
  );

  // ─── Geolocation ───────────────────────────────────────────────────────────

  const detectLocation = useCallback(() => {
    if (typeof navigator === "undefined" || !navigator.geolocation) {
      setGeoStatus("error");
      setGeoError("Geolocation not supported on this device");
      return;
    }
    setGeoStatus("detecting");
    setGeoError(null);
    navigator.geolocation.getCurrentPosition(
      (position) => {
        setGeotag({
          lat: position.coords.latitude.toFixed(6),
          lng: position.coords.longitude.toFixed(6),
        });
        setGeoStatus("detected");
      },
      (error) => {
        setGeoStatus("error");
        setGeoError(error.message || "Unable to determine location");
      },
      { enableHighAccuracy: true, timeout: 15_000, maximumAge: 60_000 },
    );
  }, []);

  useEffect(() => {
    // Best-effort auto-detect on mount; the driver can override manually.
    detectLocation();
  }, [detectLocation]);

  // ─── Upload helpers ────────────────────────────────────────────────────────

  /**
   * Run presign → PUT for a single slot and patch the slot state at each
   * stage so the UI can reflect progress. Errors are caught and stored
   * on the slot so one failure does not abort the rest.
   */
  const runUpload = useCallback(
    async (
      slot: UploadSlot,
      patch: (next: Partial<UploadSlot>) => void,
    ): Promise<void> => {
      patch({ status: "presigning", error: null });
      let presigned;
      try {
        presigned = await presignPodUpload(slot.category, slot.contentType);
      } catch (error) {
        patch({ status: "failed", error: humaniseError(error) });
        return;
      }

      if (slot.file.size > presigned.max_file_bytes) {
        patch({
          status: "failed",
          error: `File exceeds ${formatBytes(presigned.max_file_bytes)} limit`,
        });
        return;
      }

      setMaxFileBytes(presigned.max_file_bytes);
      patch({ status: "uploading" });
      try {
        await putPresignedFile(
          presigned.upload_url,
          slot.file,
          slot.contentType,
        );
      } catch (error) {
        patch({ status: "failed", error: humaniseError(error) });
        return;
      }
      patch({ status: "uploaded", fileRef: presigned.file_ref });
    },
    [],
  );

  // ─── Signature handling ────────────────────────────────────────────────────

  const handleSignatureSelect = useCallback(
    (file: File | null) => {
      if (!file) {
        setSignatureSlot(null);
        return;
      }
      const contentType = resolveContentType(file);
      if (!contentType) {
        addToast(
          "Signature must be JPEG, PNG, HEIC, or PDF",
          "error",
        );
        return;
      }
      const slot: UploadSlot = {
        clientId: nextClientId("sig"),
        category: "signature",
        file,
        contentType,
        status: "idle",
        fileRef: null,
        error: null,
      };
      setSignatureSlot(slot);
      runUpload(slot, (next) =>
        setSignatureSlot((prev) =>
          prev && prev.clientId === slot.clientId ? { ...prev, ...next } : prev,
        ),
      );
    },
    [addToast, runUpload],
  );

  const retrySignature = useCallback(() => {
    if (!signatureSlot) return;
    runUpload(signatureSlot, (next) =>
      setSignatureSlot((prev) =>
        prev && prev.clientId === signatureSlot.clientId
          ? { ...prev, ...next }
          : prev,
      ),
    );
  }, [signatureSlot, runUpload]);

  // ─── Photo handling ────────────────────────────────────────────────────────

  const handlePhotosSelect = useCallback(
    (files: FileList | null) => {
      if (!files || files.length === 0) return;
      const accepted: UploadSlot[] = [];
      for (const file of Array.from(files)) {
        const contentType = resolveContentType(file);
        if (!contentType) {
          addToast(`${file.name}: unsupported file type`, "error");
          continue;
        }
        accepted.push({
          clientId: nextClientId("photo"),
          category: "photo",
          file,
          contentType,
          status: "idle",
          fileRef: null,
          error: null,
        });
      }
      if (accepted.length === 0) return;
      setPhotoSlots((prev) => [...prev, ...accepted]);
      for (const slot of accepted) {
        runUpload(slot, (next) =>
          setPhotoSlots((prev) =>
            prev.map((entry) =>
              entry.clientId === slot.clientId ? { ...entry, ...next } : entry,
            ),
          ),
        );
      }
    },
    [addToast, runUpload],
  );

  const retryPhoto = useCallback(
    (clientId: string) => {
      const slot = photoSlots.find((entry) => entry.clientId === clientId);
      if (!slot) return;
      runUpload(slot, (next) =>
        setPhotoSlots((prev) =>
          prev.map((entry) =>
            entry.clientId === clientId ? { ...entry, ...next } : entry,
          ),
        ),
      );
    },
    [photoSlots, runUpload],
  );

  const removePhoto = useCallback((clientId: string) => {
    setPhotoSlots((prev) => prev.filter((entry) => entry.clientId !== clientId));
  }, []);

  // ─── Meter ticket handling ─────────────────────────────────────────────────

  const handleMeterSelect = useCallback(
    (file: File | null) => {
      if (!file) {
        setMeterTicketSlot(null);
        return;
      }
      const contentType = resolveContentType(file);
      if (!contentType) {
        addToast(
          "Meter ticket must be JPEG, PNG, HEIC, or PDF",
          "error",
        );
        return;
      }
      const slot: UploadSlot = {
        clientId: nextClientId("meter"),
        category: "meter_ticket",
        file,
        contentType,
        status: "idle",
        fileRef: null,
        error: null,
      };
      setMeterTicketSlot(slot);
      runUpload(slot, (next) =>
        setMeterTicketSlot((prev) =>
          prev && prev.clientId === slot.clientId ? { ...prev, ...next } : prev,
        ),
      );
    },
    [addToast, runUpload],
  );

  const retryMeter = useCallback(() => {
    if (!meterTicketSlot) return;
    runUpload(meterTicketSlot, (next) =>
      setMeterTicketSlot((prev) =>
        prev && prev.clientId === meterTicketSlot.clientId
          ? { ...prev, ...next }
          : prev,
      ),
    );
  }, [meterTicketSlot, runUpload]);

  // ─── Submit ────────────────────────────────────────────────────────────────

  const signatureReady =
    signatureSlot?.status === "uploaded" && signatureSlot.fileRef;
  const uploadedPhotoRefs = useMemo(
    () =>
      photoSlots
        .filter(
          (slot): slot is UploadSlot & { fileRef: string } =>
            slot.status === "uploaded" && slot.fileRef != null,
        )
        .map((slot) => slot.fileRef),
    [photoSlots],
  );
  const anyUploadPending = useMemo(
    () =>
      [signatureSlot, meterTicketSlot, ...photoSlots].some(
        (slot): slot is UploadSlot =>
          slot != null &&
          (slot.status === "presigning" || slot.status === "uploading"),
      ),
    [signatureSlot, meterTicketSlot, photoSlots],
  );

  const meterTicketReady =
    meterTicketSlot == null ||
    (meterTicketSlot.status === "uploaded" && meterTicketSlot.fileRef);

  const geotagValid = useMemo(() => {
    const lat = Number.parseFloat(geotag.lat);
    const lng = Number.parseFloat(geotag.lng);
    return (
      !Number.isNaN(lat) &&
      !Number.isNaN(lng) &&
      lat >= -90 &&
      lat <= 90 &&
      lng >= -180 &&
      lng <= 180
    );
  }, [geotag]);

  const canSubmit =
    !isSubmitting &&
    !anyUploadPending &&
    recipientName.trim().length > 0 &&
    Boolean(signatureReady) &&
    uploadedPhotoRefs.length > 0 &&
    Boolean(meterTicketReady) &&
    geotagValid;

  /**
   * Derive an OCR snapshot from the submit response.
   *
   * Two shapes are supported so the UI keeps working whether or not the
   * backend has been upgraded to surface OCR metadata inline:
   *
   *  • If `delivered_gallons_source` / `ocr_*` fields are present, use
   *    them directly — this is the documented upgraded response.
   *  • Otherwise, fall back to a conservative default: if the driver
   *    supplied a meter ticket but did not type a gallon count, prompt
   *    for confirmation so the driver can override a value they cannot
   *    see.
   */
  const buildOcrSnapshot = useCallback(
    (
      result: DriverPODResult,
      hadMeterTicket: boolean,
      hadDriverGallons: boolean,
    ): OcrSnapshot | null => {
      const hasOcrFields =
        "delivered_gallons_source" in result &&
        result.delivered_gallons_source != null;
      if (hasOcrFields) {
        const requiresReview =
          Boolean(result.ocr_requires_manual_review) ||
          result.delivered_gallons_source === "manual";
        // Only prompt when the driver genuinely needs to act: either OCR
        // says so, or the server fell back to manual AND the driver did
        // not type a value (so `delivered_gallons` is still null).
        const driverMustAct =
          requiresReview &&
          !hadDriverGallons &&
          (result.delivered_gallons == null ||
            result.delivered_gallons_source === "manual");
        if (!driverMustAct) {
          return null;
        }
        return {
          extractedGallons: result.delivered_gallons,
          confidence: result.ocr_confidence ?? null,
          requiresManualReview: Boolean(result.ocr_requires_manual_review),
          source: result.delivered_gallons_source,
          error: result.ocr_error ?? null,
          podId: result.pod_id,
        };
      }
      // Fallback path: no OCR metadata in the response. Only prompt when
      // the driver uploaded a meter ticket without typing a value, so we
      // can capture authoritative gallon data for reconciliation.
      if (!hadMeterTicket || hadDriverGallons) return null;
      return {
        extractedGallons: null,
        confidence: null,
        requiresManualReview: true,
        source: "manual",
        error: null,
        podId: result.pod_id,
      };
    },
    [],
  );

  const handleSubmit = useCallback(async () => {
    if (!canSubmit || !signatureSlot?.fileRef) return;
    const driverGallonsNum = driverGallons.trim().length
      ? Number.parseFloat(driverGallons)
      : undefined;
    if (
      driverGallonsNum !== undefined &&
      (Number.isNaN(driverGallonsNum) || driverGallonsNum < 0)
    ) {
      addToast("Delivered gallons must be a non-negative number", "error");
      return;
    }

    const payload: DriverPODRequest = {
      recipient_name: recipientName.trim(),
      signature_ref: signatureSlot.fileRef,
      photo_refs: uploadedPhotoRefs,
      meter_ticket_ref: meterTicketSlot?.fileRef ?? undefined,
      delivered_gallons: driverGallonsNum,
      geotag: {
        lat: Number.parseFloat(geotag.lat),
        lng: Number.parseFloat(geotag.lng),
      },
      timestamp: nowIso(),
      otp: otp.trim() || undefined,
    };

    setIsSubmitting(true);
    try {
      const response = await submitPOD(jobId, payload, idempotencyKey);
      setLastResult(response.data);
      const snapshot = buildOcrSnapshot(
        response.data,
        Boolean(payload.meter_ticket_ref),
        driverGallonsNum !== undefined,
      );
      if (snapshot) {
        setOcrSnapshot(snapshot);
        addToast(
          "POD received — please confirm the gallon count",
          "info",
        );
      } else {
        addToast("Proof of delivery submitted", "success");
        onSubmitted?.(response.data);
      }
    } catch (error) {
      addToast(humaniseError(error), "error");
    } finally {
      setIsSubmitting(false);
    }
  }, [
    addToast,
    buildOcrSnapshot,
    canSubmit,
    driverGallons,
    geotag,
    idempotencyKey,
    jobId,
    meterTicketSlot,
    onSubmitted,
    otp,
    recipientName,
    signatureSlot,
    uploadedPhotoRefs,
  ]);

  /**
   * Resubmit the POD with an authoritative gallon count.
   *
   * Uses a fresh idempotency key so the server persists a new POD
   * document; the old key is retained on the result so audit callers
   * can correlate if needed. This matches the spec's direction to
   * "submit a follow-up ... or re-submit depending on the API shape" —
   * the current backend API does not expose a PATCH, so we re-submit.
   */
  const handleOcrResubmit = useCallback(
    async (gallons: number) => {
      if (!signatureSlot?.fileRef) return;
      const freshKey = nextClientId("pod");
      setIdempotencyKey(freshKey);
      const payload: DriverPODRequest = {
        recipient_name: recipientName.trim(),
        signature_ref: signatureSlot.fileRef,
        photo_refs: uploadedPhotoRefs,
        meter_ticket_ref: meterTicketSlot?.fileRef ?? undefined,
        delivered_gallons: gallons,
        geotag: {
          lat: Number.parseFloat(geotag.lat),
          lng: Number.parseFloat(geotag.lng),
        },
        timestamp: nowIso(),
        otp: otp.trim() || undefined,
      };
      setIsSubmitting(true);
      try {
        const response = await submitPOD(jobId, payload, freshKey);
        setLastResult(response.data);
        setOcrSnapshot(null);
        addToast("Gallon count confirmed and submitted", "success");
        onSubmitted?.(response.data);
      } catch (error) {
        addToast(humaniseError(error), "error");
      } finally {
        setIsSubmitting(false);
      }
    },
    [
      addToast,
      geotag,
      jobId,
      meterTicketSlot,
      onSubmitted,
      otp,
      recipientName,
      signatureSlot,
      uploadedPhotoRefs,
    ],
  );

  // ─── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="mx-auto max-w-3xl px-4 py-6">
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />

      <header className="mb-6">
        <h1 className="text-xl font-semibold text-[#232323]">
          Proof of Delivery
        </h1>
        <p className="mt-1 text-sm text-gray-600">
          Job{" "}
          <span className="font-mono text-[#232323]">{jobId}</span>. Upload
          every artifact, then submit.
        </p>
      </header>

      <section className="mb-6 rounded-2xl border border-gray-200 bg-white p-5 shadow-sm">
        <h2 className="text-sm font-semibold text-[#232323]">
          Delivery details
        </h2>
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="block">
            <span className="text-xs font-medium text-gray-600">
              Recipient name
              <span className="text-red-500"> *</span>
            </span>
            <input
              type="text"
              value={recipientName}
              onChange={(event) => setRecipientName(event.target.value)}
              className="mt-1 block w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-[#0D9373] focus:outline-none focus:ring-1 focus:ring-[#0D9373]"
              placeholder="Jane Doe"
              disabled={isSubmitting}
            />
          </label>
          <label className="block">
            <span className="text-xs font-medium text-gray-600">OTP</span>
            <input
              type="text"
              value={otp}
              onChange={(event) => setOtp(event.target.value)}
              className="mt-1 block w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-[#0D9373] focus:outline-none focus:ring-1 focus:ring-[#0D9373]"
              placeholder="Optional"
              disabled={isSubmitting}
            />
          </label>
          <label className="block">
            <span className="text-xs font-medium text-gray-600">
              Delivered gallons
            </span>
            <input
              type="number"
              inputMode="decimal"
              min={0}
              step="0.01"
              value={driverGallons}
              onChange={(event) => setDriverGallons(event.target.value)}
              className="mt-1 block w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-[#0D9373] focus:outline-none focus:ring-1 focus:ring-[#0D9373]"
              placeholder="Leave blank to let OCR fill in"
              disabled={isSubmitting}
            />
          </label>
        </div>
      </section>

      <section className="mb-6 rounded-2xl border border-gray-200 bg-white p-5 shadow-sm">
        <div className="flex items-center justify-between">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-[#232323]">
            <MapPin className="h-4 w-4 text-[#0D9373]" /> Geotag
          </h2>
          <button
            type="button"
            onClick={detectLocation}
            disabled={geoStatus === "detecting" || isSubmitting}
            className="inline-flex items-center gap-1 rounded-lg border border-gray-300 bg-white px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          >
            {geoStatus === "detecting" ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <RefreshCw className="h-3 w-3" />
            )}
            Auto-detect
          </button>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-3">
          <label className="block">
            <span className="text-xs font-medium text-gray-600">Latitude</span>
            <input
              type="text"
              value={geotag.lat}
              onChange={(event) =>
                setGeotag((prev) => ({ ...prev, lat: event.target.value }))
              }
              className="mt-1 block w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-[#0D9373] focus:outline-none focus:ring-1 focus:ring-[#0D9373]"
              placeholder="40.712800"
              disabled={isSubmitting}
            />
          </label>
          <label className="block">
            <span className="text-xs font-medium text-gray-600">Longitude</span>
            <input
              type="text"
              value={geotag.lng}
              onChange={(event) =>
                setGeotag((prev) => ({ ...prev, lng: event.target.value }))
              }
              className="mt-1 block w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-[#0D9373] focus:outline-none focus:ring-1 focus:ring-[#0D9373]"
              placeholder="-74.006000"
              disabled={isSubmitting}
            />
          </label>
        </div>
        {geoError && (
          <p className="mt-2 text-xs text-red-600">{geoError}</p>
        )}
        {geoStatus === "detected" && !geoError && (
          <p className="mt-2 text-xs text-gray-500">
            Location captured. Adjust manually if incorrect.
          </p>
        )}
      </section>

      <section className="mb-6 rounded-2xl border border-gray-200 bg-white p-5 shadow-sm">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-[#232323]">
          <PenLine className="h-4 w-4 text-[#0D9373]" /> Signature
          <span className="text-red-500">*</span>
        </h2>
        <SlotFileInput
          accept="image/jpeg,image/png,image/heic,application/pdf"
          onChange={(files) => handleSignatureSelect(files?.[0] ?? null)}
          disabled={isSubmitting}
          maxFileBytes={maxFileBytes}
          buttonLabel={signatureSlot ? "Replace" : "Capture"}
        />
        {signatureSlot && (
          <div className="mt-3">
            <UploadRow
              slot={signatureSlot}
              onRetry={retrySignature}
              onRemove={() => setSignatureSlot(null)}
              disabled={isSubmitting}
            />
          </div>
        )}
      </section>

      <section className="mb-6 rounded-2xl border border-gray-200 bg-white p-5 shadow-sm">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-[#232323]">
          <Camera className="h-4 w-4 text-[#0D9373]" /> Photos
          <span className="text-red-500">*</span>
        </h2>
        <SlotFileInput
          accept="image/jpeg,image/png,image/heic,application/pdf"
          onChange={handlePhotosSelect}
          multiple
          disabled={isSubmitting}
          maxFileBytes={maxFileBytes}
          buttonLabel="Add photos"
        />
        {photoSlots.length > 0 && (
          <ul className="mt-3 space-y-2">
            {photoSlots.map((slot) => (
              <li key={slot.clientId}>
                <UploadRow
                  slot={slot}
                  onRetry={() => retryPhoto(slot.clientId)}
                  onRemove={() => removePhoto(slot.clientId)}
                  disabled={isSubmitting}
                />
              </li>
            ))}
          </ul>
        )}
        <p className="mt-2 text-[11px] text-gray-500">
          At least one uploaded photo is required.
        </p>
      </section>

      <section className="mb-6 rounded-2xl border border-gray-200 bg-white p-5 shadow-sm">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-[#232323]">
          <Receipt className="h-4 w-4 text-[#0D9373]" /> Meter ticket
        </h2>
        <SlotFileInput
          accept="image/jpeg,image/png,image/heic,application/pdf"
          onChange={(files) => handleMeterSelect(files?.[0] ?? null)}
          disabled={isSubmitting}
          maxFileBytes={maxFileBytes}
          buttonLabel={meterTicketSlot ? "Replace" : "Capture"}
        />
        {meterTicketSlot && (
          <div className="mt-3">
            <UploadRow
              slot={meterTicketSlot}
              onRetry={retryMeter}
              onRemove={() => setMeterTicketSlot(null)}
              disabled={isSubmitting}
            />
          </div>
        )}
        <p className="mt-2 text-[11px] text-gray-500">
          Optional. When provided without a driver-entered gallon count, the
          server will run OCR and may request confirmation.
        </p>
      </section>

      <div className="mb-10 flex items-center justify-between gap-4">
        <p className="text-xs text-gray-500">
          {anyUploadPending
            ? "Waiting for uploads to finish..."
            : canSubmit
              ? "All set — ready to submit."
              : "Complete every required field and upload to enable submit."}
        </p>
        <button
          type="button"
          onClick={handleSubmit}
          disabled={!canSubmit}
          className="inline-flex items-center gap-2 rounded-lg bg-[#0D9373] px-4 py-2 text-sm font-medium text-white hover:bg-[#0B7F63] disabled:opacity-50"
        >
          {isSubmitting ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Send className="h-4 w-4" />
          )}
          Submit POD
        </button>
      </div>

      {lastResult && !ocrSnapshot && (
        <section className="mb-10 rounded-2xl border border-green-200 bg-green-50 p-5 text-sm text-green-800">
          <div className="flex items-center gap-2">
            <Check className="h-4 w-4" />
            <span className="font-semibold">POD submitted</span>
          </div>
          <dl className="mt-3 grid grid-cols-1 gap-1 text-xs sm:grid-cols-2">
            <Row label="POD id" value={lastResult.pod_id} />
            <Row
              label="Delivered gallons"
              value={
                lastResult.delivered_gallons != null
                  ? `${lastResult.delivered_gallons.toFixed(2)} (${lastResult.delivered_gallons_source})`
                  : "—"
              }
            />
            <Row label="Status" value={lastResult.status} />
            <Row
              label="OTP verified"
              value={lastResult.otp_verified ? "Yes" : "No"}
            />
          </dl>
        </section>
      )}

      {ocrSnapshot && (
        <OcrConfirmationModal
          snapshot={ocrSnapshot}
          isSubmitting={isSubmitting}
          onAccept={(gallons) => handleOcrResubmit(gallons)}
          onOverride={(gallons) => handleOcrResubmit(gallons)}
          onCancel={() => setOcrSnapshot(null)}
        />
      )}
    </div>
  );
}

// ─── Local presentation helpers ──────────────────────────────────────────────

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between border-b border-green-100 py-1 last:border-b-0">
      <dt className="text-green-700">{label}</dt>
      <dd className="font-medium text-green-900">{value}</dd>
    </div>
  );
}

interface SlotFileInputProps {
  accept: string;
  multiple?: boolean;
  disabled?: boolean;
  buttonLabel: string;
  maxFileBytes: number;
  onChange: (files: FileList | null) => void;
}

/**
 * Presentational wrapper around `<input type="file">`.
 *
 * We intentionally keep the file input visible (rather than hiding it
 * behind a custom button) so drivers on mobile keyboards can pick from
 * the camera / gallery without additional clicks — a paved path that
 * matches how native Android/iOS capture flows work.
 */
const SlotFileInput = ({
  accept,
  multiple = false,
  disabled = false,
  buttonLabel,
  maxFileBytes,
  onChange,
}: SlotFileInputProps) => {
  return (
    <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center">
      <label className="inline-flex items-center gap-2 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-700 hover:bg-gray-50 cursor-pointer">
        <Upload className="h-4 w-4" />
        <span>{buttonLabel}</span>
        <input
          type="file"
          accept={accept}
          multiple={multiple}
          disabled={disabled}
          className="sr-only"
          onChange={(event) => {
            onChange(event.target.files);
            // Allow the same file to be re-selected after a retry.
            event.target.value = "";
          }}
        />
      </label>
      <span className="text-[11px] text-gray-500">
        Up to {formatBytes(maxFileBytes)} per file.
      </span>
    </div>
  );
};

interface UploadRowProps {
  slot: UploadSlot;
  onRetry: () => void;
  onRemove: () => void;
  disabled?: boolean;
}

function UploadRow({ slot, onRetry, onRemove, disabled }: UploadRowProps) {
  const isBusy = slot.status === "presigning" || slot.status === "uploading";
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-xs">
      <div className="flex min-w-0 items-center gap-2">
        <FileText className="h-4 w-4 text-gray-500" />
        <div className="min-w-0">
          <p className="truncate font-medium text-[#232323]">{slot.file.name}</p>
          <p className="text-[11px] text-gray-500">
            {formatBytes(slot.file.size)} · {slot.contentType}
          </p>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <StatusPill slot={slot} />
        {slot.status === "failed" && (
          <button
            type="button"
            onClick={onRetry}
            disabled={disabled}
            className="inline-flex items-center gap-1 rounded border border-gray-300 bg-white px-2 py-1 text-[11px] text-gray-700 hover:bg-gray-100 disabled:opacity-50"
          >
            <RefreshCw className="h-3 w-3" />
            Retry
          </button>
        )}
        <button
          type="button"
          onClick={onRemove}
          disabled={disabled || isBusy}
          className="rounded p-1 text-gray-400 hover:bg-gray-200 disabled:opacity-50"
          aria-label="Remove file"
        >
          <X className="h-3 w-3" />
        </button>
      </div>
    </div>
  );
}

function StatusPill({ slot }: { slot: UploadSlot }) {
  const config: Record<UploadStatus, { label: string; className: string }> = {
    idle: { label: "Queued", className: "bg-gray-100 text-gray-700" },
    presigning: {
      label: "Requesting URL",
      className: "bg-slate-100 text-slate-700",
    },
    uploading: {
      label: "Uploading",
      className: "bg-blue-100 text-blue-700",
    },
    uploaded: {
      label: "Uploaded",
      className: "bg-green-100 text-green-700",
    },
    failed: { label: slot.error ?? "Failed", className: "bg-red-100 text-red-700" },
  };
  const entry = config[slot.status];
  const showSpinner = slot.status === "presigning" || slot.status === "uploading";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium ${entry.className}`}
      title={slot.error ?? undefined}
    >
      {showSpinner && <Loader2 className="h-3 w-3 animate-spin" />}
      <span className="max-w-[12rem] truncate">{entry.label}</span>
    </span>
  );
}
