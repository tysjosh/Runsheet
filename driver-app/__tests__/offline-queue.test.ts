/**
 * Unit coverage for the offline mutation queue and the artifact store.
 *
 * The queue runs against an in-memory stand-in for its single table and a fake
 * transport, so the disposition matrix, the per-order ordering rule, and the
 * artifact retention window are exercised without a native module.
 *
 * Requirements: 11.6, 11.11, 11.12, 11.13, 11.14, 11.15, 11.16, 5.18
 */
import {
  ARTIFACT_RETENTION_MS,
  acknowledgeArtifacts,
  configureArtifactStore,
  hasArtifact,
  putArtifact,
  readArtifact,
  retainArtifactsIndefinitely,
  sweepArtifacts,
  type ArtifactFileSystem,
} from '@/lib/artifact-store';
import {
  classifyResponse,
  configureOfflineQueue,
  drainQueue,
  enqueueMutation,
  initializeQueue,
  listConflicts,
  listFailedMutations,
  listQueue,
  queueDepth,
  resetOfflineQueue,
  retryDelayMs,
  type QueueDatabase,
  type QueueRunResult,
} from '@/lib/offline-queue';
import type { ApiRequestOptions, ApiSendResult } from '@/lib/api-client';

// --- minimal in-memory stand-in for the single-table queue database ---------

interface Row {
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

function createFakeDatabase(): QueueDatabase & { rows: Row[] } {
  const rows: Row[] = [];
  const noResult: QueueRunResult = { changes: 0, lastInsertRowId: 0 };

  return {
    rows,
    async execAsync() {},
    async runAsync(source, params): Promise<QueueRunResult> {
      const sql = source.replace(/\s+/g, ' ').trim();
      if (sql.startsWith('UPDATE mutation_queue SET status = \'pending\' WHERE status = \'in_flight\'')) {
        let changes = 0;
        rows.forEach((row) => {
          if (row.status === 'in_flight') {
            row.status = 'pending';
            changes += 1;
          }
        });
        return { changes, lastInsertRowId: 0 };
      }
      if (sql.startsWith('INSERT OR IGNORE')) {
        const [
          id,
          idempotency_key,
          kind,
          method,
          path,
          body,
          order_id,
          event_timestamp,
          enqueued_at,
          artifact_refs,
        ] = params as (string | null)[];
        if (rows.some((row) => row.idempotency_key === idempotency_key)) {
          return noResult;
        }
        rows.push({
          id: id as string,
          idempotency_key: idempotency_key as string,
          kind: kind as string,
          method: method as string,
          path: path as string,
          body: body as string,
          order_id: order_id ?? null,
          event_timestamp: event_timestamp as string,
          enqueued_at: enqueued_at as string,
          status: 'pending',
          attempts: 0,
          next_attempt_at: null,
          last_error_code: null,
          last_status_code: null,
          artifact_refs: artifact_refs ?? null,
        });
        return { changes: 1, lastInsertRowId: rows.length };
      }
      if (sql.startsWith('UPDATE mutation_queue SET status = \'in_flight\'')) {
        const row = rows.find((candidate) => candidate.id === params[0]);
        if (row) {
          row.status = 'in_flight';
        }
        return { changes: row ? 1 : 0, lastInsertRowId: 0 };
      }
      if (sql.startsWith('DELETE FROM mutation_queue WHERE id = ? AND status IN')) {
        const index = rows.findIndex(
          (row) => row.id === params[0] && (row.status === 'failed' || row.status === 'conflict'),
        );
        if (index >= 0) {
          rows.splice(index, 1);
        }
        return { changes: index >= 0 ? 1 : 0, lastInsertRowId: 0 };
      }
      if (sql.startsWith('DELETE FROM mutation_queue WHERE id = ?')) {
        const index = rows.findIndex((row) => row.id === params[0]);
        if (index >= 0) {
          rows.splice(index, 1);
        }
        return { changes: index >= 0 ? 1 : 0, lastInsertRowId: 0 };
      }
      if (sql === 'DELETE FROM mutation_queue') {
        const changes = rows.length;
        rows.length = 0;
        return { changes, lastInsertRowId: 0 };
      }
      if (sql.includes("SET status = 'conflict'")) {
        const row = rows.find((candidate) => candidate.id === params[1]);
        if (row) {
          row.status = 'conflict';
          row.attempts += 1;
          row.next_attempt_at = null;
          row.last_error_code = params[0] as string;
          row.last_status_code = 409;
        }
        return { changes: row ? 1 : 0, lastInsertRowId: 0 };
      }
      if (sql.includes("SET status = 'failed'")) {
        const row = rows.find((candidate) => candidate.id === params[2]);
        if (row) {
          row.status = 'failed';
          row.attempts += 1;
          row.next_attempt_at = null;
          row.last_error_code = params[0] as string | null;
          row.last_status_code = params[1] as number | null;
        }
        return { changes: row ? 1 : 0, lastInsertRowId: 0 };
      }
      if (sql.includes("SET status = 'pending', attempts = attempts + 1")) {
        const row = rows.find((candidate) => candidate.id === params[3]);
        if (row) {
          row.status = 'pending';
          row.attempts += 1;
          row.next_attempt_at = params[0] as string;
          row.last_error_code = params[1] as string | null;
          row.last_status_code = params[2] as number | null;
        }
        return { changes: row ? 1 : 0, lastInsertRowId: 0 };
      }
      if (sql.includes("SET status = 'pending', attempts = 0")) {
        const row = rows.find((candidate) => candidate.id === params[0] && candidate.status === 'failed');
        if (row) {
          row.status = 'pending';
          row.attempts = 0;
          row.next_attempt_at = null;
        }
        return { changes: row ? 1 : 0, lastInsertRowId: 0 };
      }
      if (sql.startsWith("UPDATE mutation_queue SET status = 'pending', next_attempt_at = NULL")) {
        const row = rows.find((candidate) => candidate.id === params[0]);
        if (row) {
          row.status = 'pending';
          row.next_attempt_at = null;
        }
        return { changes: row ? 1 : 0, lastInsertRowId: 0 };
      }
      throw new Error(`unhandled runAsync: ${sql}`);
    },
    async getAllAsync<T>(source: string, params: (string | number | null)[]): Promise<T[]> {
      const sql = source.replace(/\s+/g, ' ').trim();
      const byEvent = (a: Row, b: Row) =>
        a.event_timestamp === b.event_timestamp
          ? a.enqueued_at.localeCompare(b.enqueued_at)
          : a.event_timestamp.localeCompare(b.event_timestamp);
      if (sql.includes('SELECT status, COUNT(*)')) {
        const grouped = new Map<string, number>();
        rows.forEach((row) => grouped.set(row.status, (grouped.get(row.status) ?? 0) + 1));
        return Array.from(grouped, ([status, total]) => ({ status, total })) as T[];
      }
      if (sql.startsWith('SELECT artifact_refs')) {
        return rows
          .filter((row) => row.artifact_refs !== null)
          .map((row) => ({ artifact_refs: row.artifact_refs })) as T[];
      }
      if (sql.includes("WHERE status = 'pending' AND (next_attempt_at IS NULL")) {
        const now = params[0] as string;
        return rows
          .filter(
            (row) =>
              row.status === 'pending' && (row.next_attempt_at === null || row.next_attempt_at <= now),
          )
          .sort(byEvent)
          .map((row) => ({ ...row })) as T[];
      }
      if (sql.includes('WHERE status = ?')) {
        return rows
          .filter((row) => row.status === params[0])
          .sort(byEvent)
          .map((row) => ({ ...row })) as T[];
      }
      return rows.slice().sort(byEvent).map((row) => ({ ...row })) as T[];
    },
    async getFirstAsync<T>(source: string, params: (string | number | null)[]): Promise<T | null> {
      const sql = source.replace(/\s+/g, ' ').trim();
      const row = sql.includes('idempotency_key = ?')
        ? rows.find((candidate) => candidate.idempotency_key === params[0])
        : rows.find((candidate) => candidate.id === params[0]);
      return (row ? ({ ...row } as T) : null);
    },
  };
}

function createMemoryFs(): ArtifactFileSystem {
  const files = new Map<string, string>();
  return {
    documentDirectory: 'memory://docs/',
    async getInfoAsync(uri) {
      const contents = files.get(uri);
      return contents === undefined ? { exists: false } : { exists: true, size: contents.length };
    },
    async makeDirectoryAsync() {},
    async readDirectoryAsync(uri) {
      return Array.from(files.keys())
        .filter((key) => key.startsWith(uri))
        .map((key) => key.slice(uri.length));
    },
    async readAsStringAsync(uri) {
      const contents = files.get(uri);
      if (contents === undefined) {
        throw new Error('missing');
      }
      return contents;
    },
    async writeAsStringAsync(uri, contents) {
      files.set(uri, contents);
    },
    async deleteAsync(uri) {
      files.delete(uri);
    },
  };
}

let now = Date.parse('2026-01-01T00:00:00.000Z');

beforeEach(() => {
  resetOfflineQueue();
  now = Date.parse('2026-01-01T00:00:00.000Z');
  configureArtifactStore({ fileSystem: createMemoryFs(), directory: 'memory://docs/pod/', now: () => now });
  configureOfflineQueue({ database: createFakeDatabase(), now: () => now, random: () => 0.5 });
});

describe('disposition matrix', () => {
  const response = (status: number, errorCode: string | null = null): ApiSendResult => ({
    kind: 'response',
    status,
    ok: status >= 200 && status < 300,
    errorCode,
    data: null,
  });

  it('classifies every bucket', () => {
    expect(classifyResponse(response(200)).kind).toBe('success');
    expect(classifyResponse(response(202, 'DUTY_STATUS_PROJECTION_PENDING')).kind).toBe('success');
    expect(classifyResponse(response(409, 'INVALID_STATUS_TRANSITION')).kind).toBe('conflict');
    expect(classifyResponse(response(409, 'POD_GALLONS_CONFIRMATION_REQUIRED')).kind).toBe('retry');
    expect(classifyResponse(response(408)).kind).toBe('retry');
    expect(classifyResponse(response(429)).kind).toBe('retry');
    expect(classifyResponse(response(503)).kind).toBe('retry');
    [400, 401, 403, 404, 422].forEach((status) =>
      expect(classifyResponse(response(status)).kind).toBe('terminal'),
    );
    expect(classifyResponse({ kind: 'no_response' }).kind).toBe('offline');
  });

  it('keeps the backoff inside 2s..300s', () => {
    expect(retryDelayMs(0)).toBe(2000);
    expect(retryDelayMs(1)).toBe(4000);
    expect(retryDelayMs(30)).toBe(300000);
  });
});

describe('drain loop', () => {
  it('preserves per-order event order and dequeues on 2xx', async () => {
    await initializeQueue();
    const sent: string[] = [];
    configureOfflineQueue({
      transport: async (options: ApiRequestOptions): Promise<ApiSendResult> => {
        sent.push(options.path ?? '');
        await new Promise((resolve) => setTimeout(resolve, 1));
        return { kind: 'response', status: 200, ok: true, errorCode: null, data: null };
      },
    });
    await enqueueMutation({
      kind: 'pod',
      method: 'POST',
      path: '/pod',
      orderId: 'o1',
      eventTimestamp: '2026-01-01T11:00:00.000Z',
      artifactRefs: ['t/sig.png'],
    });
    await enqueueMutation({
      kind: 'order_status',
      method: 'POST',
      path: '/status',
      orderId: 'o1',
      eventTimestamp: '2026-01-01T09:00:00.000Z',
    });
    await enqueueMutation({
      kind: 'wait_report',
      method: 'POST',
      path: '/wait',
      eventTimestamp: '2026-01-01T10:00:00.000Z',
    });

    const summary = await drainQueue();
    expect(summary.succeeded).toBe(3);
    expect(sent.indexOf('/status')).toBeLessThan(sent.indexOf('/pod'));
    expect(await listQueue()).toHaveLength(0);
  });

  it('routes conflicts, failures, retries, and offline correctly', async () => {
    await initializeQueue();
    const scripted: Record<string, ApiSendResult> = {
      '/conflict': { kind: 'response', status: 409, ok: false, errorCode: 'INVALID_STATUS_TRANSITION', data: null },
      '/failed': { kind: 'response', status: 422, ok: false, errorCode: 'DELIVERED_GALLONS_REQUIRED', data: null },
      '/retry': { kind: 'response', status: 503, ok: false, errorCode: 'UNEXPECTED_ERROR', data: null },
    };
    configureOfflineQueue({
      transport: async (options) => scripted[options.path ?? ''] ?? { kind: 'no_response' },
    });
    await enqueueMutation({ kind: 'order_status', method: 'POST', path: '/conflict', orderId: 'a', eventTimestamp: '2026-01-01T01:00:00.000Z' });
    await enqueueMutation({ kind: 'pod', method: 'POST', path: '/failed', orderId: 'b', eventTimestamp: '2026-01-01T02:00:00.000Z', artifactRefs: ['t/x.jpg'] });
    await enqueueMutation({ kind: 'checkin', method: 'POST', path: '/retry', orderId: 'c', eventTimestamp: '2026-01-01T03:00:00.000Z' });
    await enqueueMutation({ kind: 'exception', method: 'POST', path: '/nowhere', orderId: 'd', eventTimestamp: '2026-01-01T04:00:00.000Z' });

    const summary = await drainQueue();
    expect(summary.conflicted).toBe(1);
    expect(summary.failed).toBe(1);
    expect(summary.retryScheduled).toBe(1);
    expect(summary.offline).toBe(true);

    expect(await listConflicts()).toHaveLength(1);
    expect((await listFailedMutations())[0].lastErrorCode).toBe('DELIVERED_GALLONS_REQUIRED');
    const depth = await queueDepth();
    expect(depth.failed).toBe(1);
    expect(depth.conflict).toBe(1);
    expect(depth.pending).toBe(2);
    expect(depth.outstanding).toBe(3);
  });

  it('collapses a double tap on one idempotency key', async () => {
    await initializeQueue();
    const key = 'fixed-key';
    const first = await enqueueMutation({ kind: 'pod', method: 'POST', path: '/pod', idempotencyKey: key });
    const second = await enqueueMutation({ kind: 'pod', method: 'POST', path: '/pod', idempotencyKey: key });
    expect(first.inserted).toBe(true);
    expect(second.inserted).toBe(false);
    expect(await listQueue()).toHaveLength(1);
  });
});

describe('artifact retention', () => {
  it('keeps bytes until acknowledgement plus 24 hours', async () => {
    await putArtifact({ fileRef: 't/sig.png', base64: 'AAAA', contentType: 'image/png' });
    expect(await readArtifact('t/sig.png')).toBe('AAAA');

    now += 48 * 60 * 60 * 1000;
    expect(await sweepArtifacts()).toBe(0);
    expect(await hasArtifact('t/sig.png')).toBe(true);

    await acknowledgeArtifacts(['t/sig.png']);
    expect(await sweepArtifacts()).toBe(0);

    now += ARTIFACT_RETENTION_MS - 1;
    expect(await sweepArtifacts()).toBe(0);
    now += 1;
    expect(await sweepArtifacts()).toBe(1);
    expect(await hasArtifact('t/sig.png')).toBe(false);
  });

  it('keeps a failed row\'s bytes indefinitely', async () => {
    await putArtifact({ fileRef: 't/photo.jpg', base64: 'BBBB' });
    await acknowledgeArtifacts(['t/photo.jpg']);
    await retainArtifactsIndefinitely(['t/photo.jpg']);
    now += 10 * ARTIFACT_RETENTION_MS;
    expect(await sweepArtifacts()).toBe(0);
    expect(await hasArtifact('t/photo.jpg')).toBe(true);
  });

  it('sweeps orphans past the grace period only', async () => {
    await putArtifact({ fileRef: 't/orphan.jpg', base64: 'CCCC' });
    expect(await sweepArtifacts({ referencedRefs: [] })).toBe(0);
    now += ARTIFACT_RETENTION_MS;
    expect(await sweepArtifacts({ referencedRefs: [] })).toBe(1);
  });
});
