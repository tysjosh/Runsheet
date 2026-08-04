/**
 * Unit coverage for the duty-bound breadcrumb sampler.
 *
 * The ring buffer, the drain ordering, and the metres-per-second → miles-per-hour
 * conversion are exercised against an in-memory store and a fake transport, so no
 * GPS and no network are involved.
 *
 * Requirements: 10.9, 10.10, 10.11, 10.12, 16.19
 */
// `expo-task-manager` resolves a native module at import time, which no Jest
// environment has. The task registry it provides is a JavaScript map, so the
// registry itself is all that has to stand in.
jest.mock('expo-task-manager', () => {
  const tasks = new Map<string, unknown>();
  return {
    isTaskDefined: (name: string) => tasks.has(name),
    defineTask: (name: string, executor: unknown) => tasks.set(name, executor),
  };
});

import type { ApiRequestOptions, ApiSendResult } from '@/lib/api-client';
import {
  BREADCRUMB_BATCH_LIMIT,
  BREADCRUMB_BUFFER_LIMIT,
  BREADCRUMB_PATH,
  BreadcrumbBuffer,
  MAX_SAMPLE_INTERVAL_MS,
  breadcrumbBuffer,
  configureLocationTracker,
  drainBreadcrumbs,
  resetLocationTracker,
  shouldSampleLocation,
  toBreadcrumbSample,
  type BreadcrumbBufferStore,
  type BreadcrumbSample,
} from '@/lib/location-tracker';
import { milesPerHourFromMetersPerSecond } from '@/lib/units';

function createMemoryStore(): BreadcrumbBufferStore {
  const map = new Map<string, string>();
  return {
    getString: (key) => map.get(key),
    set: (key, value) => {
      map.set(key, value);
    },
    delete: (key) => {
      map.delete(key);
    },
  };
}

function sampleAt(isoTimestamp: string): BreadcrumbSample {
  return {
    latitude: 32.7767,
    longitude: -96.797,
    sample_timestamp: isoTimestamp,
    accuracy_meters: 8,
    speed_mph: 55,
    heading_degrees: 180,
  };
}

/** `n` samples one second apart, starting at 2026-08-01T00:00:00Z. */
function sampleSeries(count: number): BreadcrumbSample[] {
  const start = Date.parse('2026-08-01T00:00:00.000Z');
  return Array.from({ length: count }, (_unused, index) =>
    sampleAt(new Date(start + index * 1000).toISOString()),
  );
}

describe('breadcrumb ring buffer (R10.11)', () => {
  it('holds samples in ascending sample_timestamp order', () => {
    const buffer = new BreadcrumbBuffer(10, createMemoryStore());
    buffer.push(sampleAt('2026-08-01T00:00:30.000Z'));
    buffer.push(sampleAt('2026-08-01T00:00:10.000Z'));
    buffer.push(sampleAt('2026-08-01T00:00:20.000Z'));

    expect(buffer.snapshot().map((entry) => entry.sample_timestamp)).toEqual([
      '2026-08-01T00:00:10.000Z',
      '2026-08-01T00:00:20.000Z',
      '2026-08-01T00:00:30.000Z',
    ]);
  });

  it('discards the oldest sample past the capacity and keeps the newest', () => {
    const buffer = new BreadcrumbBuffer(3, createMemoryStore());
    const series = sampleSeries(5);
    const discarded = series.map((entry) => buffer.push(entry));

    expect(discarded).toEqual([0, 0, 0, 1, 1]);
    expect(buffer.size()).toBe(3);
    expect(buffer.snapshot().map((entry) => entry.sample_timestamp)).toEqual([
      series[2].sample_timestamp,
      series[3].sample_timestamp,
      series[4].sample_timestamp,
    ]);
  });

  it('caps the device hold at 5000 samples', () => {
    expect(BREADCRUMB_BUFFER_LIMIT).toBe(5000);

    // Seeded to the brim through the store, so the assertion is about the
    // eviction at the real limit rather than about 5000 serializations.
    const store = createMemoryStore();
    const series = sampleSeries(BREADCRUMB_BUFFER_LIMIT + 5);
    store.set('buffer', JSON.stringify(series.slice(0, BREADCRUMB_BUFFER_LIMIT)));

    const buffer = new BreadcrumbBuffer();
    buffer.setStore(store);
    expect(buffer.size()).toBe(BREADCRUMB_BUFFER_LIMIT);

    series.slice(BREADCRUMB_BUFFER_LIMIT).forEach((entry) => {
      expect(buffer.push(entry)).toBe(1);
    });

    expect(buffer.size()).toBe(BREADCRUMB_BUFFER_LIMIT);
    // The five oldest are the ones gone; the five newest are held.
    expect(buffer.snapshot()[0].sample_timestamp).toBe(series[5].sample_timestamp);
    expect(buffer.snapshot().at(-1)?.sample_timestamp).toBe(
      series[series.length - 1].sample_timestamp,
    );
  });

  it('survives a reload from the device store', () => {
    const store = createMemoryStore();
    const first = new BreadcrumbBuffer(10, store);
    sampleSeries(3).forEach((entry) => first.push(entry));

    const reloaded = new BreadcrumbBuffer(10, store);
    expect(reloaded.size()).toBe(3);
  });
});

describe('breadcrumb drain (R10.12)', () => {
  beforeEach(() => {
    resetLocationTracker();
    configureLocationTracker({ bufferStore: createMemoryStore() });
    breadcrumbBuffer.clear();
  });

  afterEach(() => {
    resetLocationTracker();
    configureLocationTracker({ bufferStore: createMemoryStore() });
  });

  it('submits in ascending sample_timestamp batches of at most 200', async () => {
    const series = sampleSeries(450);
    // Enqueued newest-first, so ordering cannot come from arrival order.
    [...series].reverse().forEach((entry) => breadcrumbBuffer.push(entry));

    const sent: ApiRequestOptions[] = [];
    configureLocationTracker({
      transport: async (options) => {
        sent.push(options);
        return { kind: 'response', status: 202, ok: true, errorCode: null, data: {} };
      },
    });

    const summary = await drainBreadcrumbs();

    expect(summary).toMatchObject({ submitted: 450, batches: 3, stopped: 'complete' });
    expect(breadcrumbBuffer.size()).toBe(0);

    const batches = sent.map(
      (options) => (options.body as { samples: BreadcrumbSample[] }).samples,
    );
    expect(batches.map((batch) => batch.length)).toEqual([
      BREADCRUMB_BATCH_LIMIT,
      BREADCRUMB_BATCH_LIMIT,
      50,
    ]);
    expect(
      batches.flat().map((entry) => entry.sample_timestamp),
    ).toEqual(series.map((entry) => entry.sample_timestamp));
    expect(sent[0].path).toBe(BREADCRUMB_PATH);
    // R10.2 — the body carries samples and no driver identifier.
    expect(Object.keys(sent[0].body as object)).toEqual(['samples']);
  });

  it('keeps the buffer intact when no response arrives', async () => {
    sampleSeries(5).forEach((entry) => breadcrumbBuffer.push(entry));
    configureLocationTracker({
      transport: async (): Promise<ApiSendResult> => ({ kind: 'no_response' }),
    });

    const summary = await drainBreadcrumbs();

    expect(summary).toMatchObject({ submitted: 0, stopped: 'offline' });
    expect(breadcrumbBuffer.size()).toBe(5);
  });

  it('stops the pass on a retryable status without losing samples', async () => {
    sampleSeries(5).forEach((entry) => breadcrumbBuffer.push(entry));
    configureLocationTracker({
      transport: async () => ({
        kind: 'response' as const,
        status: 503,
        ok: false,
        errorCode: 'SERVICE_UNAVAILABLE',
        data: {},
      }),
    });

    const summary = await drainBreadcrumbs();

    expect(summary.stopped).toBe('retry_later');
    expect(breadcrumbBuffer.size()).toBe(5);
  });

  it('drops a batch the server will never accept so later samples can flow', async () => {
    sampleSeries(5).forEach((entry) => breadcrumbBuffer.push(entry));
    configureLocationTracker({
      transport: async () => ({
        kind: 'response' as const,
        status: 422,
        ok: false,
        errorCode: 'VALIDATION_ERROR',
        data: {},
      }),
    });

    const summary = await drainBreadcrumbs();

    expect(summary).toMatchObject({ submitted: 0, rejected: 5, stopped: 'complete' });
    expect(breadcrumbBuffer.size()).toBe(0);
  });

  it('gives concurrent callers the one in-flight pass', async () => {
    sampleSeries(10).forEach((entry) => breadcrumbBuffer.push(entry));
    let calls = 0;
    configureLocationTracker({
      transport: async () => {
        calls += 1;
        return { kind: 'response' as const, status: 202, ok: true, errorCode: null, data: {} };
      },
    });

    const [first, second] = await Promise.all([drainBreadcrumbs(), drainBreadcrumbs()]);

    expect(calls).toBe(1);
    expect(first).toBe(second);
  });
});

describe('duty-status binding (R10.9, R10.10)', () => {
  it('samples only while active or on_break', () => {
    expect(shouldSampleLocation('active')).toBe(true);
    expect(shouldSampleLocation('on_break')).toBe(true);
    expect(shouldSampleLocation('off_duty')).toBe(false);
    expect(shouldSampleLocation('inactive')).toBe(false);
    expect(shouldSampleLocation(null)).toBe(false);
  });

  it('bounds the sampling interval at 60 seconds', () => {
    expect(MAX_SAMPLE_INTERVAL_MS).toBe(60_000);
  });
});

describe('sample conversion (R16.19)', () => {
  it('reports speed in miles per hour', () => {
    // 26.8224 m/s is 60 mph exactly.
    expect(milesPerHourFromMetersPerSecond(26.8224)).toBeCloseTo(60, 2);
    expect(
      toBreadcrumbSample({
        latitude: 1,
        longitude: 2,
        accuracy: 12.5,
        speed: 26.8224,
        heading: 90,
        sample_timestamp: '2026-08-01T00:00:00.000Z',
      }),
    ).toEqual({
      latitude: 1,
      longitude: 2,
      sample_timestamp: '2026-08-01T00:00:00.000Z',
      accuracy_meters: 12.5,
      speed_mph: 60,
      heading_degrees: 90,
    });
  });

  it('carries an unavailable reading as null rather than as a negative number', () => {
    expect(
      toBreadcrumbSample({
        latitude: 1,
        longitude: 2,
        accuracy: -1,
        speed: -1,
        heading: -1,
        sample_timestamp: '2026-08-01T00:00:00.000Z',
      }),
    ).toMatchObject({ accuracy_meters: null, speed_mph: null, heading_degrees: null });
    expect(milesPerHourFromMetersPerSecond(null)).toBeNull();
    expect(milesPerHourFromMetersPerSecond(undefined)).toBeNull();
  });
});
