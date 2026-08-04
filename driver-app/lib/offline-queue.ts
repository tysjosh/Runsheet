/**
 * The durable on-device mutation queue (R11.6 – R11.16).
 *
 * Every driver-initiated write — POD submissions, stop check-ins, order status
 * transitions, exception reports, terminal wait reports, compartment cleaning
 * events, inspection reports (R11.8) — is enqueued here first and sent from
 * here, so an action taken in a dead zone is never lost and never duplicated.
 *
 * Four properties carry the design:
 *
 *  1. **Durability (R11.7).** Rows live in `expo-sqlite`, on disk. A mutation
 *     enqueued and then killed by the OS is present on next launch. Any row left
 *     `in_flight` by a termination is returned to `pending` at
 *     {@link initializeQueue}, because nothing else can own it.
 *  2. **Exactly-once (R11.6).** The idempotency key is generated at the moment
 *     the driver performs the action and reused on every retry. The column is
 *     `UNIQUE`, so a double-tap that produces two enqueues of the same action
 *     collapses at insert time rather than at the server.
 *  3. **Order (R11.11).** The drain reads `pending` rows in ascending
 *     `event_timestamp` and holds at most one in-flight mutation per
 *     `order_id`. A `POST /status {in_transit}` recorded at 09:00 therefore
 *     lands before a `POST /pod` recorded at 11:00 for the same order, without
 *     which the POD's `in_transit → delivered` transition would be illegal.
 *     Rows with a `NULL` `order_id` are not order-serialized. Different orders
 *     proceed concurrently, capped at {@link DRAIN_CONCURRENCY}.
 *  4. **Convergence (R11.12 – R11.15).** {@link classifyResponse} is the whole
 *     disposition matrix, as a pure function of the transport result, so every
 *     status code has exactly one outcome and no row can loop forever.
 *
 * A single async loop owns the queue — there is no second consumer — so
 * `in_flight` needs no lock. 401 never reaches the matrix as a live case: the
 * API client intercepts it, refreshes once, and replays under the same
 * idempotency key; only a 401 that survives a refresh arrives here, and it is
 * terminal.
 *
 * Requirements: 11.6, 11.7, 11.8, 11.9, 11.11, 11.12, 11.13, 11.14, 11.15, 11.16, 5.18
 */

import * as SQLite from 'expo-sqlite';

import { apiSend, type ApiRequestOptions, type ApiSendResult, type HttpMethod } from './api-client';
import {
  acknowledgeArtifacts,
  retainArtifactsIndefinitely,
  sweepArtifacts,
} from './artifact-store';
import { registerSessionPurgeHandler } from './session';

/** Every mutation the queue carries (R11.8). */
export type MutationKind =
  | 'pod'
  | 'checkin'
  | 'order_status'
  | 'exception'
  | 'wait_report'
  | 'cleaning_event'
  | 'inspection';

/** Lifecycle of a queued row. `pending` and `failed` are what the driver sees. */
export type QueueStatus = 'pending' | 'in_flight' | 'failed' | 'conflict';

/** The error code that makes a 409 terminal rather than transient (R11.13). */
export const INVALID_STATUS_TRANSITION = 'INVALID_STATUS_TRANSITION';

/** How many mutations for *different* orders may be in flight at once. */
export const DRAIN_CONCURRENCY = 4;

/** First retry delay (R11.15). */
export const RETRY_BASE_MS = 2_000;

/** Retry ceiling (R11.15). */
export const RETRY_CAP_MS = 300_000;

/** Jitter fraction applied to the backoff so a reconnecting fleet desynchronizes. */
export const RETRY_JITTER = 0.2;

const DATABASE_NAME = 'runsheet-driver-queue.db';

const COLUMNS =
  'id, idempotency_key, kind, method, path, body, order_id, event_timestamp, ' +
  'enqueued_at, status, attempts, next_attempt_at, last_error_code, last_status_code, artifact_refs';

// ---------------------------------------------------------------------------
// Storage seam
// ---------------------------------------------------------------------------

/** The result `runAsync` reports. Matches `SQLiteRunResult`. */
export interface QueueRunResult {
  changes: number;
  lastInsertRowId: number;
}

/**
 * The slice of `expo-sqlite` the queue needs. Injectable so the drain loop and
 * the disposition matrix can be driven against an in-memory database.
 */
export interface QueueDatabase {
  execAsync(source: string): Promise<void>;
  runAsync(source: string, params: (string | number | null)[]): Promise<QueueRunResult>;
  getAllAsync<T>(source: string, params: (string | number | null)[]): Promise<T[]>;
  getFirstAsync<T>(source: string, params: (string | number | null)[]): Promise<T | null>;
}

/** The transport the queue sends through. Defaults to {@link apiSend}. */
export type QueueTransport = (options: ApiRequestOptions) => Promise<ApiSendResult>;

/** Raw column shape, exactly as stored. */
interface QueueRowRecord {
  id: string;
  idempotency_key: string;
  kind: string;
  method: string;
  path: string;
  body: string;
  order_id: string | null;
  event_timestamp: string;
  enqueued_at: string;
  status: string;
  attempts: number;
  next_attempt_at: string | null;
  last_error_code: string | null;
  last_status_code: number | null;
  artifact_refs: string | null;
}

/** A queued mutation, as the UI and the drain loop read it. */
export interface QueuedMutation {
  id: string;
  idempotencyKey: string;
  kind: MutationKind;
  method: HttpMethod;
  path: string;
  body: unknown;
  orderId: string | null;
  /** Client-asserted ISO 8601 instant at which the driver acted (R11.9). */
  eventTimestamp: string;
  enqueuedAt: string;
  status: QueueStatus;
  attempts: number;
  nextAttemptAt: string | null;
  lastErrorCode: string | null;
  lastStatusCode: number | null;
  artifactRefs: string[];
}

/** What {@link enqueueMutation} accepts. */
export interface EnqueueInput {
  kind: MutationKind;
  method: HttpMethod;
  /** Fully resolved path — no interpolation happens at send time. */
  path: string;
  body?: unknown;
  /** Concurrency key (R11.11). `null` or omitted means the row is unserialized. */
  orderId?: string | null;
  /**
   * The instant the driver performed the action (R11.9). ISO 8601, epoch ms, or
   * a `Date`. Defaults to now, which is only correct when the caller enqueues
   * at action time — which is the contract.
   */
  eventTimestamp?: string | number | Date;
  /**
   * Generated once, at action time, and reused on every retry (R11.6). Supply it
   * from the screen so a re-render cannot mint a second key for one action.
   */
  idempotencyKey?: string;
  /** POD artifact `file_ref`s whose bytes must outlive this row (R11.16). */
  artifactRefs?: string[];
}

export interface EnqueueResult {
  mutation: QueuedMutation;
  /**
   * `false` when the idempotency key was already queued — the double-tap case.
   * The existing row is returned untouched.
   */
  inserted: boolean;
}

/** Counts the header chip renders (R11.10). */
export interface QueueDepth {
  pending: number;
  inFlight: number;
  failed: number;
  conflict: number;
  /** `pending + failed` — what R11.10 puts in front of the driver. */
  outstanding: number;
}

/** Outcome of one disposition decision. */
export type Disposition =
  | { kind: 'success'; status: number }
  | { kind: 'conflict'; status: number; errorCode: string }
  | { kind: 'retry'; status: number; errorCode: string | null }
  | { kind: 'terminal'; status: number; errorCode: string | null }
  | { kind: 'offline' };

export interface DrainSummary {
  attempted: number;
  succeeded: number;
  conflicted: number;
  failed: number;
  retryScheduled: number;
  /** True when the transport reported no response, so the pass stopped early. */
  offline: boolean;
}

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------

let database: QueueDatabase | null = null;
let openingDatabase: Promise<QueueDatabase> | null = null;
let transport: QueueTransport = apiSend;
let clock: () => number = () => Date.now();
let random: () => number = Math.random;

let draining: Promise<DrainSummary> | null = null;
let rerunRequested = false;

const listeners = new Set<(depth: QueueDepth) => void>();

/** Override the storage, the transport, the clock, and the jitter source. Tests only. */
export function configureOfflineQueue(next: {
  database?: QueueDatabase | null;
  transport?: QueueTransport | null;
  now?: (() => number) | null;
  random?: (() => number) | null;
}): void {
  if (next.database !== undefined) {
    database = next.database;
    openingDatabase = null;
  }
  if (next.transport !== undefined) {
    transport = next.transport ?? apiSend;
  }
  if (next.now !== undefined) {
    clock = next.now ?? (() => Date.now());
  }
  if (next.random !== undefined) {
    random = next.random ?? Math.random;
  }
}

/** Drop the in-memory handles and subscribers. Tests only. */
export function resetOfflineQueue(): void {
  database = null;
  openingDatabase = null;
  transport = apiSend;
  clock = () => Date.now();
  random = Math.random;
  draining = null;
  rerunRequested = false;
  listeners.clear();
}

/** Subscribe to depth changes. Returns the unsubscribe function. */
export function subscribeToQueue(listener: (depth: QueueDepth) => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

async function publishDepth(): Promise<void> {
  if (listeners.size === 0) {
    return;
  }
  const depth = await queueDepth();
  listeners.forEach((listener) => listener(depth));
}

// ---------------------------------------------------------------------------
// Schema
// ---------------------------------------------------------------------------

const SCHEMA = `
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS mutation_queue (
  id                 TEXT PRIMARY KEY,
  idempotency_key    TEXT NOT NULL UNIQUE,
  kind               TEXT NOT NULL,
  method             TEXT NOT NULL,
  path               TEXT NOT NULL,
  body               TEXT NOT NULL,
  order_id           TEXT,
  event_timestamp    TEXT NOT NULL,
  enqueued_at        TEXT NOT NULL,
  status             TEXT NOT NULL,
  attempts           INTEGER NOT NULL DEFAULT 0,
  next_attempt_at    TEXT,
  last_error_code    TEXT,
  last_status_code   INTEGER,
  artifact_refs      TEXT
);
CREATE INDEX IF NOT EXISTS ix_queue_drain ON mutation_queue (status, event_timestamp);
CREATE INDEX IF NOT EXISTS ix_queue_order ON mutation_queue (order_id, status);
`;

async function openDatabase(): Promise<QueueDatabase> {
  if (database) {
    return database;
  }
  if (!openingDatabase) {
    openingDatabase = (async () => {
      const opened = (await SQLite.openDatabaseAsync(DATABASE_NAME)) as unknown as QueueDatabase;
      await opened.execAsync(SCHEMA);
      // Nothing else can own an `in_flight` row: a previous process died mid
      // send. The idempotency key makes re-sending safe (R11.6, R11.7).
      await opened.runAsync(
        `UPDATE mutation_queue SET status = 'pending' WHERE status = 'in_flight'`,
        [],
      );
      database = opened;
      return opened;
    })();
  }
  return openingDatabase;
}

/**
 * Open the queue, create the schema, and recover rows left `in_flight` by a
 * termination. Safe to call more than once.
 */
export async function initializeQueue(): Promise<void> {
  const db = await openDatabase();
  await db.execAsync(SCHEMA);
  await db.runAsync(`UPDATE mutation_queue SET status = 'pending' WHERE status = 'in_flight'`, []);
  await publishDepth();
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const HEX = '0123456789abcdef';

function randomHexByte(): string {
  const value = Math.floor(random() * 256);
  return `${HEX[(value >> 4) & 0xf]}${HEX[value & 0xf]}`;
}

/**
 * A v4 UUID for the idempotency key.
 *
 * Prefers the platform CSPRNG and falls back to the module's `random` source,
 * which keeps the key generatable in a test harness with a seeded generator.
 */
export function generateIdempotencyKey(): string {
  const cryptoApi = (globalThis as { crypto?: Crypto }).crypto;
  if (cryptoApi?.randomUUID) {
    return cryptoApi.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (cryptoApi?.getRandomValues) {
    cryptoApi.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => `${HEX[(byte >> 4) & 0xf]}${HEX[byte & 0xf]}`).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** Monotonic within the process, which is what makes {@link generateRowId} collision-free. */
let rowSequence = 0;

/**
 * The row key.
 *
 * Timestamp plus an in-process counter plus random bytes. The counter is what
 * carries the guarantee: two rows enqueued in the same millisecond from the same
 * process cannot collide even when the random source is a seeded generator, and
 * `id` is the `PRIMARY KEY` every state transition addresses — a collision would
 * silently route one row's disposition onto another row and leave the loser
 * eligible forever.
 */
function generateRowId(): string {
  rowSequence += 1;
  return `${clock().toString(36)}-${rowSequence.toString(36)}-${randomHexByte()}${randomHexByte()}`;
}

function isoOf(value: string | number | Date | undefined, fallbackMs: number): string {
  if (typeof value === 'string') {
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? new Date(fallbackMs).toISOString() : new Date(parsed).toISOString();
  }
  if (typeof value === 'number') {
    return new Date(value).toISOString();
  }
  if (value instanceof Date) {
    return value.toISOString();
  }
  return new Date(fallbackMs).toISOString();
}

function parseRefs(raw: string | null): string[] {
  if (!raw) {
    return [];
  }
  try {
    const parsed = JSON.parse(raw) as unknown;
    return Array.isArray(parsed) ? parsed.filter((ref): ref is string => typeof ref === 'string') : [];
  } catch {
    return [];
  }
}

function asStatus(value: string): QueueStatus {
  return value === 'in_flight' || value === 'failed' || value === 'conflict' ? value : 'pending';
}

function toMutation(row: QueueRowRecord): QueuedMutation {
  let body: unknown = null;
  try {
    body = JSON.parse(row.body);
  } catch {
    body = row.body;
  }
  return {
    id: row.id,
    idempotencyKey: row.idempotency_key,
    kind: row.kind as MutationKind,
    method: row.method as HttpMethod,
    path: row.path,
    body,
    orderId: row.order_id,
    eventTimestamp: row.event_timestamp,
    enqueuedAt: row.enqueued_at,
    status: asStatus(row.status),
    attempts: row.attempts,
    nextAttemptAt: row.next_attempt_at,
    lastErrorCode: row.last_error_code,
    lastStatusCode: row.last_status_code,
    artifactRefs: parseRefs(row.artifact_refs),
  };
}

/**
 * Retry delay for attempt `attempts` (0-based): `min(2s · 2^attempts, 300s)`
 * with ±20 % jitter, clamped to `[2s, 300s]` so R11.15's floor and ceiling both
 * hold literally after jittering.
 */
export function retryDelayMs(attempts: number): number {
  const exponent = Math.max(0, Math.min(attempts, 30));
  const base = Math.min(RETRY_BASE_MS * 2 ** exponent, RETRY_CAP_MS);
  const jittered = base * (1 - RETRY_JITTER + 2 * RETRY_JITTER * random());
  return Math.min(RETRY_CAP_MS, Math.max(RETRY_BASE_MS, Math.round(jittered)));
}

// ---------------------------------------------------------------------------
// Enqueue and read
// ---------------------------------------------------------------------------

/**
 * Persist a mutation for later submission (R11.7, R11.8).
 *
 * The idempotency key is written once and reused on every retry (R11.6). Because
 * the column is `UNIQUE`, enqueuing the same key twice is a no-op that returns
 * the row already queued, which is what makes a double-tap harmless.
 */
export async function enqueueMutation(input: EnqueueInput): Promise<EnqueueResult> {
  const handle = await openDatabase();
  const now = clock();
  const idempotencyKey = input.idempotencyKey ?? generateIdempotencyKey();
  const row: QueueRowRecord = {
    id: generateRowId(),
    idempotency_key: idempotencyKey,
    kind: input.kind,
    method: input.method,
    path: input.path,
    body: JSON.stringify(input.body ?? {}),
    order_id: input.orderId ?? null,
    event_timestamp: isoOf(input.eventTimestamp, now),
    enqueued_at: new Date(now).toISOString(),
    status: 'pending',
    attempts: 0,
    next_attempt_at: null,
    last_error_code: null,
    last_status_code: null,
    artifact_refs: input.artifactRefs?.length ? JSON.stringify(input.artifactRefs) : null,
  };

  // `INSERT OR IGNORE` collapses a double tap on one idempotency key (R11.6). It
  // would also swallow a `PRIMARY KEY` collision, so a dropped insert that is not
  // explained by an existing row for this key is re-attempted under a fresh row
  // id rather than reported as enqueued.
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const result = await handle.runAsync(
      `INSERT OR IGNORE INTO mutation_queue (${COLUMNS})
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, ?)`,
      [
        row.id,
        row.idempotency_key,
        row.kind,
        row.method,
        row.path,
        row.body,
        row.order_id,
        row.event_timestamp,
        row.enqueued_at,
        row.artifact_refs,
      ],
    );

    if (result.changes > 0) {
      await publishDepth();
      return { mutation: toMutation(row), inserted: true };
    }

    const existing = await handle.getFirstAsync<QueueRowRecord>(
      `SELECT ${COLUMNS} FROM mutation_queue WHERE idempotency_key = ?`,
      [idempotencyKey],
    );
    if (existing) {
      return { mutation: toMutation(existing), inserted: false };
    }
    row.id = generateRowId();
  }

  throw new Error('Could not enqueue mutation: the queue rejected the insert.');
}

/** Every row in the queue, whatever its state, newest event last. */
export async function listQueue(): Promise<QueuedMutation[]> {
  const handle = await openDatabase();
  const rows = await handle.getAllAsync<QueueRowRecord>(
    `SELECT ${COLUMNS} FROM mutation_queue ORDER BY event_timestamp ASC, enqueued_at ASC`,
    [],
  );
  return rows.map(toMutation);
}

async function listByStatus(status: QueueStatus): Promise<QueuedMutation[]> {
  const handle = await openDatabase();
  const rows = await handle.getAllAsync<QueueRowRecord>(
    `SELECT ${COLUMNS} FROM mutation_queue WHERE status = ? ORDER BY event_timestamp ASC`,
    [status],
  );
  return rows.map(toMutation);
}

/**
 * Rows that will never be retried and carry a server error code the driver has
 * to see (R11.14).
 */
export function listFailedMutations(): Promise<QueuedMutation[]> {
  return listByStatus('failed');
}

/**
 * The driver-visible conflict entries (R11.13) — a status transition the server
 * had already moved past. They are out of the submission set and are never
 * retried; {@link dismissQueueEntry} removes one once the driver has read it.
 */
export function listConflicts(): Promise<QueuedMutation[]> {
  return listByStatus('conflict');
}

/** Counts for the header chip (R11.10). */
export async function queueDepth(): Promise<QueueDepth> {
  const handle = await openDatabase();
  const rows = await handle.getAllAsync<{ status: string; total: number }>(
    `SELECT status, COUNT(*) AS total FROM mutation_queue GROUP BY status`,
    [],
  );
  const depth: QueueDepth = {
    pending: 0,
    inFlight: 0,
    failed: 0,
    conflict: 0,
    outstanding: 0,
  };
  for (const row of rows) {
    switch (asStatus(row.status)) {
      case 'pending':
        depth.pending = row.total;
        break;
      case 'in_flight':
        depth.inFlight = row.total;
        break;
      case 'failed':
        depth.failed = row.total;
        break;
      case 'conflict':
        depth.conflict = row.total;
        break;
    }
  }
  depth.outstanding = depth.pending + depth.failed;
  return depth;
}

/** Every `file_ref` any queued row still depends on (R11.16). */
export async function referencedArtifactRefs(): Promise<string[]> {
  const handle = await openDatabase();
  const rows = await handle.getAllAsync<{ artifact_refs: string | null }>(
    `SELECT artifact_refs FROM mutation_queue WHERE artifact_refs IS NOT NULL`,
    [],
  );
  const refs = new Set<string>();
  for (const row of rows) {
    parseRefs(row.artifact_refs).forEach((ref) => refs.add(ref));
  }
  return Array.from(refs);
}

/**
 * Foreground sweep: delete artifact bytes whose 24-hour post-acknowledgement
 * timer has elapsed, plus orphans no queue row references (R5.18, R11.16).
 *
 * @returns the number of artifacts deleted.
 */
export async function sweepQueueArtifacts(): Promise<number> {
  return sweepArtifacts({ referencedRefs: await referencedArtifactRefs() });
}

/**
 * Return a `failed` row to `pending` so the driver can retry after a dispatcher
 * fixes the server-side cause. The idempotency key is unchanged (R11.6).
 *
 * Only a `failed` row is retryable: a `conflict` row is never re-submitted
 * (R11.13), because the server has already moved past that transition.
 */
export async function retryMutation(id: string): Promise<void> {
  const handle = await openDatabase();
  await handle.runAsync(
    `UPDATE mutation_queue
        SET status = 'pending', attempts = 0, next_attempt_at = NULL
      WHERE id = ? AND status = 'failed'`,
    [id],
  );
  await publishDepth();
}

/**
 * Delete a `failed` or `conflict` row the driver has acknowledged, and schedule
 * its artifact bytes for deletion 24 hours later (R5.18).
 */
export async function dismissQueueEntry(id: string): Promise<void> {
  const handle = await openDatabase();
  const row = await handle.getFirstAsync<QueueRowRecord>(
    `SELECT ${COLUMNS} FROM mutation_queue WHERE id = ?`,
    [id],
  );
  if (!row) {
    return;
  }
  await handle.runAsync(
    `DELETE FROM mutation_queue WHERE id = ? AND status IN ('failed', 'conflict')`,
    [id],
  );
  await acknowledgeArtifacts(parseRefs(row.artifact_refs));
  await publishDepth();
}

/** Delete every queued row. Registered as the sign-out eraser (R15.5). */
export async function purgeQueue(): Promise<void> {
  const handle = await openDatabase();
  await handle.runAsync(`DELETE FROM mutation_queue`, []);
  await publishDepth();
}

// ---------------------------------------------------------------------------
// Disposition matrix
// ---------------------------------------------------------------------------

/**
 * The whole matrix, as a pure function of the transport result.
 *
 * | Response | Outcome | Requirement |
 * |---|---|---|
 * | 2xx — including `202 DUTY_STATUS_PROJECTION_PENDING` | `success` — dequeue | R11.12 |
 * | 409 `INVALID_STATUS_TRANSITION` | `conflict` — dequeue, driver-visible entry | R11.13 |
 * | 409 any other code | `retry` — a transient conflict such as `POD_GALLONS_CONFIRMATION_REQUIRED` or a lock timeout is not terminal | R11.15 |
 * | 408, 429, 5xx | `retry` with backoff | R11.14 exclusion, R11.15 |
 * | other 4xx — 400, 401 surviving refresh, 403, 404, 422 | `terminal` — `failed`, surface the server `error_code` | R11.14 |
 * | no response | `offline` — stay `pending` | — |
 *
 * A 202 is a 2xx, so `DUTY_STATUS_PROJECTION_PENDING` dequeues: the write was
 * accepted and the projection catches up on the server's own schedule. Anything
 * outside 2xx/4xx/5xx (a 1xx or a redirect the transport surfaced) is retried
 * rather than failed, because it is not a statement that the request was wrong.
 */
export function classifyResponse(result: ApiSendResult): Disposition {
  if (result.kind === 'no_response') {
    return { kind: 'offline' };
  }
  const { status, errorCode } = result;
  if (status >= 200 && status < 300) {
    return { kind: 'success', status };
  }
  if (status === 409) {
    return errorCode === INVALID_STATUS_TRANSITION
      ? { kind: 'conflict', status, errorCode: INVALID_STATUS_TRANSITION }
      : { kind: 'retry', status, errorCode };
  }
  if (status === 408 || status === 429 || status >= 500) {
    return { kind: 'retry', status, errorCode };
  }
  if (status >= 400) {
    return { kind: 'terminal', status, errorCode };
  }
  return { kind: 'retry', status, errorCode };
}

// ---------------------------------------------------------------------------
// Drain loop
// ---------------------------------------------------------------------------

async function eligibleRows(nowIso: string): Promise<QueueRowRecord[]> {
  const handle = await openDatabase();
  return handle.getAllAsync<QueueRowRecord>(
    `SELECT ${COLUMNS} FROM mutation_queue
      WHERE status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
      ORDER BY event_timestamp ASC, enqueued_at ASC`,
    [nowIso],
  );
}

async function markInFlight(id: string): Promise<void> {
  const handle = await openDatabase();
  await handle.runAsync(`UPDATE mutation_queue SET status = 'in_flight' WHERE id = ?`, [id]);
}

async function dequeue(row: QueueRowRecord): Promise<void> {
  const handle = await openDatabase();
  await handle.runAsync(`DELETE FROM mutation_queue WHERE id = ?`, [row.id]);
  // R11.16 / R5.18 — the row has left the queue with a 2xx, so the bytes may be
  // deleted once the 24-hour timer has also elapsed. Not before.
  await acknowledgeArtifacts(parseRefs(row.artifact_refs));
}

async function markConflict(row: QueueRowRecord, errorCode: string): Promise<void> {
  const handle = await openDatabase();
  // R11.13 — out of the submission set and never retried, retained only as the
  // driver-visible conflict entry until it is dismissed.
  await handle.runAsync(
    `UPDATE mutation_queue
        SET status = 'conflict', attempts = attempts + 1, next_attempt_at = NULL,
            last_error_code = ?, last_status_code = 409
      WHERE id = ?`,
    [errorCode, row.id],
  );
  // The evidence outlives the conflict: a dispatcher may need it to reconcile.
  await retainArtifactsIndefinitely(parseRefs(row.artifact_refs));
}

async function markFailed(
  row: QueueRowRecord,
  status: number,
  errorCode: string | null,
): Promise<void> {
  const handle = await openDatabase();
  await handle.runAsync(
    `UPDATE mutation_queue
        SET status = 'failed', attempts = attempts + 1, next_attempt_at = NULL,
            last_error_code = ?, last_status_code = ?
      WHERE id = ?`,
    [errorCode, status, row.id],
  );
  // R11.16 — a failed POD keeps its bytes indefinitely so the driver can retry
  // once the server-side cause is fixed.
  await retainArtifactsIndefinitely(parseRefs(row.artifact_refs));
}

async function scheduleRetry(
  row: QueueRowRecord,
  status: number | null,
  errorCode: string | null,
): Promise<void> {
  const handle = await openDatabase();
  const delay = retryDelayMs(row.attempts);
  await handle.runAsync(
    `UPDATE mutation_queue
        SET status = 'pending', attempts = attempts + 1, next_attempt_at = ?,
            last_error_code = ?, last_status_code = ?
      WHERE id = ?`,
    [new Date(clock() + delay).toISOString(), errorCode, status, row.id],
  );
}

async function returnToPending(row: QueueRowRecord): Promise<void> {
  const handle = await openDatabase();
  // No response: the attempt never reached the server, so it does not count
  // against the backoff schedule. The row waits for the next connectivity event.
  await handle.runAsync(
    `UPDATE mutation_queue SET status = 'pending', next_attempt_at = NULL WHERE id = ?`,
    [row.id],
  );
}

async function sendRow(row: QueueRowRecord): Promise<ApiSendResult> {
  let body: unknown;
  try {
    body = JSON.parse(row.body);
  } catch {
    body = row.body;
  }
  try {
    return await transport({
      method: row.method as HttpMethod,
      path: row.path,
      body,
      idempotencyKey: row.idempotency_key,
    });
  } catch {
    // A transport that throws for a reason other than "no response" — a missing
    // base URL, say — must not spin the row. Treat it as offline: the row stays
    // pending and the pass stops.
    return { kind: 'no_response' };
  }
}

async function applyDisposition(
  row: QueueRowRecord,
  disposition: Disposition,
  summary: DrainSummary,
): Promise<void> {
  switch (disposition.kind) {
    case 'success':
      await dequeue(row);
      summary.succeeded += 1;
      break;
    case 'conflict':
      await markConflict(row, disposition.errorCode);
      summary.conflicted += 1;
      break;
    case 'terminal':
      await markFailed(row, disposition.status, disposition.errorCode);
      summary.failed += 1;
      break;
    case 'retry':
      await scheduleRetry(row, disposition.status, disposition.errorCode);
      summary.retryScheduled += 1;
      break;
    case 'offline':
      await returnToPending(row);
      summary.offline = true;
      break;
  }
}

async function runDrainPass(): Promise<DrainSummary> {
  const summary: DrainSummary = {
    attempted: 0,
    succeeded: 0,
    conflicted: 0,
    failed: 0,
    retryScheduled: 0,
    offline: false,
  };

  /** Order ids with a mutation in flight — R11.11's serialization key. */
  const blockedOrders = new Set<string>();
  const active = new Map<string, Promise<void>>();
  /**
   * Rows this pass has already sent. One attempt per row per pass, so the pass
   * terminates by construction: a row that came back retryable waits for the
   * next pass rather than being re-sent the moment its backoff elapses.
   */
  const attempted = new Set<string>();

  for (;;) {
    if (!summary.offline) {
      const rows = await eligibleRows(new Date(clock()).toISOString());
      for (const row of rows) {
        if (active.size >= DRAIN_CONCURRENCY) {
          break;
        }
        if (active.has(row.id) || attempted.has(row.id)) {
          continue;
        }
        if (row.order_id !== null && blockedOrders.has(row.order_id)) {
          // A later mutation for the same order waits for the earlier one, so
          // the server sees them in event-timestamp order (R11.11).
          continue;
        }
        if (row.order_id !== null) {
          blockedOrders.add(row.order_id);
        }
        attempted.add(row.id);
        await markInFlight(row.id);
        summary.attempted += 1;
        const attempt = (async () => {
          const result = await sendRow(row);
          await applyDisposition(row, classifyResponse(result), summary);
        })()
          .catch(() => undefined)
          .finally(() => {
            active.delete(row.id);
            if (row.order_id !== null) {
              blockedOrders.delete(row.order_id);
            }
          });
        active.set(row.id, attempt);
      }
    }

    if (active.size === 0) {
      break;
    }
    // Re-evaluate as soon as one slot frees: a completed mutation may unblock
    // the next mutation for the same order.
    await Promise.race(Array.from(active.values()));
  }

  await publishDepth();
  return summary;
}

function mergeSummaries(first: DrainSummary, second: DrainSummary): DrainSummary {
  return {
    attempted: first.attempted + second.attempted,
    succeeded: first.succeeded + second.succeeded,
    conflicted: first.conflicted + second.conflicted,
    failed: first.failed + second.failed,
    retryScheduled: first.retryScheduled + second.retryScheduled,
    offline: first.offline || second.offline,
  };
}

/**
 * Submit every eligible queued mutation (R11.11).
 *
 * Call it on connectivity restoration, on app foreground, and after enqueuing an
 * action while online. Concurrent callers receive the one in-flight pass — the
 * queue has a single writer, which is what makes `in_flight` safe without a
 * lock — and a call arriving mid-pass schedules exactly one follow-up pass so a
 * mutation enqueued during a drain is not left waiting.
 */
export function drainQueue(): Promise<DrainSummary> {
  if (draining) {
    rerunRequested = true;
    return draining;
  }
  const pass = (async () => {
    let summary = await runDrainPass();
    while (rerunRequested) {
      rerunRequested = false;
      summary = mergeSummaries(summary, await runDrainPass());
    }
    return summary;
  })().finally(() => {
    draining = null;
    rerunRequested = false;
  });
  draining = pass;
  return pass;
}

registerSessionPurgeHandler('mutation-queue', purgeQueue);
