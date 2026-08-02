/**
 * Proof of delivery — artifact upload, on-device draft retention, and the queued
 * submission.
 *
 * The upload contract is R5.4 exactly: ask
 * `POST /api/driver/pod/uploads/presign` for a URL, PUT the bytes to the returned
 * `upload_url` using the returned `content_type`, and keep the returned
 * `file_ref` for the submission. Two failure modes are handled here rather than
 * shown to the driver:
 *
 *  - **Too large** (R5.5). An artifact bigger than the returned `max_file_bytes`
 *    is re-encoded smaller through `expo-image-manipulator` and the upload is
 *    retried **once**, against a fresh presigned URL because the re-encode can
 *    change the content type. A second overflow is surfaced.
 *  - **Expired URL** (R5.6). A presigned URL whose `expires_at` has already
 *    passed is replaced before the PUT rather than after it fails, and a PUT the
 *    object store rejects with 403 — the shape an expired signature takes — gets
 *    one replacement URL and one retry.
 *
 * Evidence is durable before the network is touched: {@link queuePodCapture}
 * writes the captured bytes into SQLite, so the driver may walk away from the
 * screen and a dead radio cannot lose a POD (R5.18, R11.16). Upload and
 * enqueueing happen on the next connected pass.
 *
 * No artifact byte and no `file_ref` is ever passed to a log statement.
 *
 * Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.14, 5.18, 5.19, 11.6, 11.8, 11.16
 */

// SDK 54 (expo-file-system 19) moved the URI-based API to
// `expo-file-system/legacy`. `uploadAsync` in particular has no equivalent in
// the new File/Directory API, and the deprecated stub the package root still
// exports for it throws at runtime, so this must resolve to the legacy entry.
import * as FileSystem from 'expo-file-system/legacy';
import { ImageManipulator, SaveFormat } from 'expo-image-manipulator';
import NetInfo from '@react-native-community/netinfo';
import * as SQLite from 'expo-sqlite';

import { ApiError, apiRequest, assertTls } from './api-client';
import { putArtifact } from './artifact-store';
import {
  drainQueue,
  enqueueMutation,
  generateIdempotencyKey,
  type EnqueueResult,
} from './offline-queue';
import { registerSessionPurgeHandler } from './session';

/** The three categories the driver surface uploads (R5.1, R5.2, R5.3). */
export type PodArtifactCategory = 'signature' | 'photo' | 'meter_ticket';

/** The image MIME types the presign service accepts. */
export type PodContentType = 'image/jpeg' | 'image/png' | 'image/heic';

/** `driver/models.py:42-54` `DeliveryRefusalReason`, verbatim. */
export type RefusalReasonCode =
  | 'customer_refused'
  | 'customer_unavailable'
  | 'access_denied'
  | 'unsafe_site'
  | 'wrong_product'
  | 'insufficient_capacity'
  | 'payment_hold'
  | 'other';

export interface RefusalReasonOption {
  value: RefusalReasonCode;
  label: string;
}

/** The eight reason codes, in the order the refusal path lists them (R5.14). */
export const REFUSAL_REASONS: RefusalReasonOption[] = [
  { value: 'customer_refused', label: 'Customer refused the delivery' },
  { value: 'customer_unavailable', label: 'Nobody available on site' },
  { value: 'access_denied', label: 'Access to the site was denied' },
  { value: 'unsafe_site', label: 'Site was unsafe to unload' },
  { value: 'wrong_product', label: 'Wrong product for this tank' },
  { value: 'insufficient_capacity', label: 'Tank had insufficient capacity' },
  { value: 'payment_hold', label: 'Payment hold' },
  { value: 'other', label: 'Other' },
];

interface PresignResponseBody {
  data: {
    file_ref: string;
    upload_url: string;
    expires_at: string;
    content_type: string;
    max_file_bytes: number;
  };
}

/** One presigned upload grant. */
export interface PresignedUpload {
  fileRef: string;
  uploadUrl: string;
  expiresAt: string;
  /** The content type the PUT must send — the server's value, not ours (R5.4). */
  contentType: string;
  maxFileBytes: number;
}

/** An artifact captured on the device, before it has a `file_ref`. */
export interface CapturedArtifact {
  category: PodArtifactCategory;
  /** The image bytes, base64-encoded with no data-URL prefix. */
  base64: string;
  contentType: PodContentType;
}

export interface UploadedPodArtifact {
  fileRef: string;
  localUri: string;
  contentType: string;
  category: PodArtifactCategory;
}

/** `PODRequest` (`driver/models.py:135-182`), the fields this app populates. */
export interface PodSubmission {
  recipient_name: string;
  customer_id?: string;
  signature_ref?: string;
  photo_refs?: string[];
  meter_ticket_ref?: string;
  /** US gallons. Never litres, and never converted (R5.19, R16.18). */
  delivered_gallons?: number;
  geotag: { lat: number; lng: number };
  timestamp: string;
  otp?: string;
  refused_delivery?: boolean;
  refusal_reason_code?: RefusalReasonCode;
  refusal_note?: string;
}

/** A POD before its artifacts have `file_ref` values. */
export type PodCapture = Omit<
  PodSubmission,
  'signature_ref' | 'photo_refs' | 'meter_ticket_ref'
>;

function categoryLabel(category: PodArtifactCategory): string {
  return category.replace('_', ' ');
}

/**
 * Byte length of the payload a base64 string encodes.
 *
 * The presign service's `max_file_bytes` bounds the *decoded* object, so the
 * comparison has to be against this rather than the string length.
 */
export function approximateBase64Bytes(base64: string): number {
  const compact = base64.replace(/[\r\n=]/g, '');
  return Math.floor((compact.length * 3) / 4);
}

/** Whether a presigned URL has already aged out (R5.6). */
export function isPresignExpired(
  expiresAt: string | null | undefined,
  now: number = Date.now(),
): boolean {
  if (!expiresAt) {
    return false;
  }
  const parsed = Date.parse(expiresAt);
  return Number.isFinite(parsed) && parsed <= now;
}

async function requestPresignedUpload(
  category: PodArtifactCategory,
  contentType: PodContentType,
): Promise<PresignedUpload> {
  const response = await apiRequest<PresignResponseBody>({
    method: 'POST',
    path: '/api/driver/pod/uploads/presign',
    body: { category, content_type: contentType },
  });
  const grant = response.data;
  return {
    fileRef: grant.file_ref,
    uploadUrl: grant.upload_url,
    expiresAt: grant.expires_at,
    contentType: grant.content_type || contentType,
    maxFileBytes: grant.max_file_bytes,
  };
}

/** Extension for the temporary file the re-encoder reads. */
function extensionFor(contentType: PodContentType): string {
  switch (contentType) {
    case 'image/png':
      return 'png';
    case 'image/heic':
      return 'heic';
    default:
      return 'jpg';
  }
}

/**
 * Re-encode an oversized artifact (R5.5).
 *
 * The bytes are written to a cache file first because the manipulator reads a
 * URI, then resized and JPEG-compressed. JPEG is the output for every input,
 * including a PNG signature: a lossless PNG that already exceeded the tenant's
 * limit will not come back under it by being re-saved as PNG. The caller
 * re-presigns afterwards, so the changed content type travels with the retry.
 *
 * @returns the smaller artifact, or `null` when the platform cannot re-encode.
 */
async function reencodeArtifact(
  artifact: CapturedArtifact,
  maxBytes: number,
): Promise<CapturedArtifact | null> {
  const directory = FileSystem.cacheDirectory;
  if (!directory) {
    return null;
  }
  const sourceUri = `${directory}runsheet-pod-reencode-${generateIdempotencyKey()}.${extensionFor(
    artifact.contentType,
  )}`;
  try {
    await FileSystem.writeAsStringAsync(sourceUri, artifact.base64, {
      encoding: 'base64',
    });
    const bytes = approximateBase64Bytes(artifact.base64);
    // Scale the long edge by the square root of the overflow ratio: area, and so
    // roughly encoded size, falls with the square of the linear dimension.
    const overflow = bytes > 0 ? maxBytes / bytes : 1;
    const scale = Math.min(1, Math.max(0.25, Math.sqrt(overflow)));
    const rendered = await ImageManipulator.manipulate(sourceUri)
      .resize({ width: Math.max(640, Math.round(2400 * scale)) })
      .renderAsync();
    const saved = await rendered.saveAsync({
      base64: true,
      compress: 0.6,
      format: SaveFormat.JPEG,
    });
    if (!saved.base64) {
      return null;
    }
    return {
      category: artifact.category,
      base64: saved.base64,
      contentType: 'image/jpeg',
    };
  } catch {
    return null;
  } finally {
    await FileSystem.deleteAsync(sourceUri, { idempotent: true }).catch(
      () => undefined,
    );
  }
}

async function putArtifactBytes(
  upload: PresignedUpload,
  localUri: string,
  contentType: string,
): Promise<number> {
  const response = await FileSystem.uploadAsync(
    assertTls(upload.uploadUrl),
    localUri,
    {
      httpMethod: 'PUT',
      uploadType: FileSystem.FileSystemUploadType.BINARY_CONTENT,
      headers: { 'Content-Type': contentType },
    },
  );
  return response.status;
}

/**
 * Upload one artifact and return the `file_ref` the POD submission carries.
 *
 * The bytes are written to the artifact store under the `file_ref` before the
 * PUT, so a submission queued behind a dead radio still has its evidence on the
 * device (R11.16).
 */
export async function uploadPodArtifact(
  artifact: CapturedArtifact,
): Promise<UploadedPodArtifact> {
  let current = artifact;
  let upload = await requestPresignedUpload(
    current.category,
    current.contentType,
  );

  // R5.5 — one re-encode, then one retry against a fresh grant.
  if (approximateBase64Bytes(current.base64) > upload.maxFileBytes) {
    const smaller = await reencodeArtifact(current, upload.maxFileBytes);
    if (
      !smaller ||
      approximateBase64Bytes(smaller.base64) > upload.maxFileBytes
    ) {
      throw new Error(
        `The ${categoryLabel(current.category)} is too large to upload, even ` +
          'after being re-encoded. Capture it again at a lower resolution.',
      );
    }
    current = smaller;
    upload = await requestPresignedUpload(
      current.category,
      current.contentType,
    );
  }

  // R5.6 — the grant may have aged out while the driver finished the wizard.
  if (isPresignExpired(upload.expiresAt)) {
    upload = await requestPresignedUpload(
      current.category,
      current.contentType,
    );
  }

  let localUri = await putArtifact({
    fileRef: upload.fileRef,
    base64: current.base64,
    contentType: current.contentType,
  });
  let status = await putArtifactBytes(upload, localUri, upload.contentType);

  // An object store answers an expired signature with 403, so one replacement
  // grant and one retry is the rest of R5.6.
  if (status === 403) {
    upload = await requestPresignedUpload(
      current.category,
      current.contentType,
    );
    localUri = await putArtifact({
      fileRef: upload.fileRef,
      base64: current.base64,
      contentType: current.contentType,
    });
    status = await putArtifactBytes(upload, localUri, upload.contentType);
  }

  if (status < 200 || status >= 300) {
    throw new Error(
      `The ${categoryLabel(current.category)} upload failed (${status}).`,
    );
  }

  return {
    fileRef: upload.fileRef,
    localUri,
    contentType: current.contentType,
    category: current.category,
  };
}

// ---------------------------------------------------------------------------
// The queued submission
// ---------------------------------------------------------------------------

/**
 * Enqueue the POD itself.
 *
 * Every artifact `file_ref` travels on the row, which is what keeps the bytes on
 * the device until the submission leaves the queue (R11.16).
 */
export async function queuePodSubmission(args: {
  orderId: string;
  pod: PodSubmission;
  idempotencyKey?: string;
}): Promise<EnqueueResult> {
  const artifactRefs = [
    ...(args.pod.signature_ref ? [args.pod.signature_ref] : []),
    ...(args.pod.photo_refs ?? []),
    ...(args.pod.meter_ticket_ref ? [args.pod.meter_ticket_ref] : []),
  ];
  const queued = await enqueueMutation({
    kind: 'pod',
    method: 'POST',
    path: `/api/driver/orders/${encodeURIComponent(args.orderId)}/pod`,
    body: args.pod,
    orderId: args.orderId,
    eventTimestamp: args.pod.timestamp,
    idempotencyKey: args.idempotencyKey ?? generateIdempotencyKey(),
    artifactRefs,
  });

  // Best effort: the row is already durable, so a dead radio cannot lose it.
  void drainQueue();
  return queued;
}

// ---------------------------------------------------------------------------
// On-device drafts
// ---------------------------------------------------------------------------

const POD_DRAFT_DATABASE = 'runsheet-pod-drafts.db';

/**
 * The pre-task-18.8 draft row: one signature and exactly one photo, in dedicated
 * NOT NULL columns. Rows written by an earlier build are still drained, so an
 * upgrade cannot strand a captured POD.
 */
interface LegacyPodDraft {
  id: string;
  order_id: string;
  pod_json: string;
  signature_base64: string;
  signature_content_type: string;
  photo_base64: string;
  photo_content_type: string;
  idempotency_key: string;
}

/** The current draft row: any number of artifacts, any subset of categories. */
interface StoredPodCapture {
  id: string;
  order_id: string;
  pod_json: string;
  artifacts_json: string;
  idempotency_key: string;
}

let podDraftDb: Promise<SQLite.SQLiteDatabase> | null = null;
let syncingDrafts: Promise<number> | null = null;

async function openPodDraftDatabase(): Promise<SQLite.SQLiteDatabase> {
  if (!podDraftDb) {
    podDraftDb = (async () => {
      const db = await SQLite.openDatabaseAsync(POD_DRAFT_DATABASE);
      await db.execAsync(`
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS pod_drafts (
          id TEXT PRIMARY KEY,
          order_id TEXT NOT NULL,
          pod_json TEXT NOT NULL,
          signature_base64 TEXT NOT NULL,
          signature_content_type TEXT NOT NULL,
          photo_base64 TEXT NOT NULL,
          photo_content_type TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_pod_drafts_created
          ON pod_drafts (created_at);
        CREATE TABLE IF NOT EXISTS pod_captures (
          id TEXT PRIMARY KEY,
          order_id TEXT NOT NULL,
          pod_json TEXT NOT NULL,
          artifacts_json TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_pod_captures_created
          ON pod_captures (created_at);
      `);
      return db;
    })();
  }
  return podDraftDb;
}

async function clearPodDrafts(): Promise<void> {
  const db = await openPodDraftDatabase();
  await db.runAsync('DELETE FROM pod_drafts');
  await db.runAsync('DELETE FROM pod_captures');
}

registerSessionPurgeHandler('pod-drafts', clearPodDrafts);

function parseArtifacts(raw: string): CapturedArtifact[] {
  try {
    const parsed = JSON.parse(raw) as CapturedArtifact[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

/**
 * Compose the submission from the uploaded artifacts.
 *
 * A refusal legitimately carries no signature and no photo (R5.14), so absent
 * refs are omitted rather than sent empty.
 */
function submissionFrom(
  pod: PodCapture,
  uploaded: UploadedPodArtifact[],
): PodSubmission {
  const signature = uploaded.find((item) => item.category === 'signature');
  const meterTicket = uploaded.find((item) => item.category === 'meter_ticket');
  const photos = uploaded
    .filter((item) => item.category === 'photo')
    .map((item) => item.fileRef);
  return {
    ...pod,
    ...(signature ? { signature_ref: signature.fileRef } : {}),
    ...(photos.length > 0 ? { photo_refs: photos } : {}),
    ...(meterTicket ? { meter_ticket_ref: meterTicket.fileRef } : {}),
  };
}

async function syncCaptureRow(row: StoredPodCapture): Promise<void> {
  const pod = JSON.parse(row.pod_json) as PodCapture;
  const artifacts = parseArtifacts(row.artifacts_json);
  const uploaded: UploadedPodArtifact[] = [];
  for (const artifact of artifacts) {
    uploaded.push(await uploadPodArtifact(artifact));
  }
  await queuePodSubmission({
    orderId: row.order_id,
    idempotencyKey: row.idempotency_key,
    pod: submissionFrom(pod, uploaded),
  });
  const db = await openPodDraftDatabase();
  await db.runAsync('DELETE FROM pod_captures WHERE id = ?', row.id);
}

async function syncLegacyDraftRow(row: LegacyPodDraft): Promise<void> {
  const pod = JSON.parse(row.pod_json) as PodCapture;
  const uploaded: UploadedPodArtifact[] = [];
  for (const artifact of [
    {
      category: 'signature' as const,
      base64: row.signature_base64,
      contentType: row.signature_content_type as PodContentType,
    },
    {
      category: 'photo' as const,
      base64: row.photo_base64,
      contentType: row.photo_content_type as PodContentType,
    },
  ]) {
    uploaded.push(await uploadPodArtifact(artifact));
  }
  await queuePodSubmission({
    orderId: row.order_id,
    idempotencyKey: row.idempotency_key,
    pod: submissionFrom(pod, uploaded),
  });
  const db = await openPodDraftDatabase();
  await db.runAsync('DELETE FROM pod_drafts WHERE id = ?', row.id);
}

/**
 * Upload and enqueue every locally captured POD once a usable connection returns.
 *
 * Oldest first, and the loop stops at the first failure: the row that failed
 * keeps its evidence and its idempotency key, so the next foreground or
 * reconnect pass retries the same submission rather than a second one.
 */
export async function syncPendingPodCaptures(): Promise<number> {
  if (syncingDrafts) {
    return syncingDrafts;
  }
  syncingDrafts = (async () => {
    const network = await NetInfo.fetch();
    if (!network.isConnected) {
      return 0;
    }
    const db = await openPodDraftDatabase();
    let synced = 0;

    const legacyRows = await db.getAllAsync<LegacyPodDraft>(
      'SELECT * FROM pod_drafts ORDER BY created_at ASC',
    );
    for (const row of legacyRows) {
      try {
        await syncLegacyDraftRow(row);
        synced += 1;
      } catch {
        return synced;
      }
    }

    const rows = await db.getAllAsync<StoredPodCapture>(
      'SELECT * FROM pod_captures ORDER BY created_at ASC',
    );
    for (const row of rows) {
      try {
        await syncCaptureRow(row);
        synced += 1;
      } catch {
        break;
      }
    }
    return synced;
  })();
  try {
    return await syncingDrafts;
  } finally {
    syncingDrafts = null;
  }
}

export interface PodCaptureResult {
  /** `true` when the artifacts uploaded and the POD reached the queue. */
  synced: boolean;
  draftId: string;
  /** Set when the first upload attempt failed; the draft is retained regardless. */
  error?: Error;
}

/**
 * Persist a captured POD, then try to upload and enqueue it.
 *
 * The write comes first and the network second: after this resolves the driver
 * may leave the screen whatever the connection did, because the evidence is on
 * disk with its idempotency key already fixed (R5.18, R11.6, R11.16).
 */
export async function queuePodCapture(args: {
  orderId: string;
  pod: PodCapture;
  artifacts: CapturedArtifact[];
}): Promise<PodCaptureResult> {
  const db = await openPodDraftDatabase();
  const draftId = generateIdempotencyKey();
  const idempotencyKey = generateIdempotencyKey();
  await db.runAsync(
    `INSERT INTO pod_captures (
       id, order_id, pod_json, artifacts_json, idempotency_key, created_at
     ) VALUES (?, ?, ?, ?, ?, ?)`,
    draftId,
    args.orderId,
    JSON.stringify(args.pod),
    JSON.stringify(args.artifacts),
    idempotencyKey,
    new Date().toISOString(),
  );

  const network = await NetInfo.fetch();
  if (!network.isConnected) {
    return { synced: false, draftId };
  }
  try {
    const row = await db.getFirstAsync<StoredPodCapture>(
      'SELECT * FROM pod_captures WHERE id = ?',
      draftId,
    );
    if (row) {
      await syncCaptureRow(row);
      return { synced: true, draftId };
    }
  } catch (error) {
    // The durable draft remains for the next reconnect pass. The reason is
    // surfaced so the screen can say something specific — an `ApiError` carries
    // the server's own code, never a credential.
    return {
      synced: false,
      draftId,
      error:
        error instanceof ApiError
          ? new Error(error.message)
          : error instanceof Error
            ? error
            : new Error('The POD upload did not complete.'),
    };
  }
  return { synced: false, draftId };
}
