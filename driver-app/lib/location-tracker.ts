/**
 * Copied from azumi-rider/lib/location-tracker.ts
 * Copied: 2026-07-29
 * Donor: azumi-rider (Expo SDK 53). Retains the interval-based location poller
 * — the immediate first sample, the periodic timer, start/stop, and the
 * one-shot manual trigger — and the donor's capture of speed, heading, and
 * horizontal accuracy (Requirements 16.2, 16.3, 16.4, 10.1).
 *
 * Phase 2 adaptation, applied here (Requirements 10.9 – 10.12):
 *   - The interval is **duty-status bound**. Sampling runs at most 60 s apart
 *     while the driver holds `active` or `on_break`, and stops outright while
 *     `off_duty` or `inactive` (R10.9, R10.10). The duty vocabulary and the
 *     stored copy come from `lib/duty-api.ts`; nothing here invents a status.
 *   - Samples land in a 5000-entry ring buffer on the device, oldest discarded
 *     past that (R10.11), and drain in ascending `sample_timestamp` batches of
 *     at most 200 when connectivity returns (R10.12).
 *   - `expo-task-manager` carries the sampling while the app is backgrounded:
 *     one task feeds the same ring buffer the foreground timer feeds, so a
 *     shift spent with the screen off produces the same track.
 *
 * Changed from the donor: the donor sent each sample to `PATCH /rider/location`
 * through `azumi-rider/lib/api-client.ts`. That is a `/rider/*` endpoint client
 * (Requirement 16.12) reached through a module that logs credentials, and
 * neither is carried. Samples go to `POST /api/driver/telemetry/breadcrumbs`
 * through `lib/api-client.ts`, in a body that carries no driver identifier —
 * the server derives it from `TenantContext` (R10.2). Timer handles are typed
 * `ReturnType<typeof setInterval>` rather than `NodeJS.Timeout`, and the donor's
 * `simpleLocationPoller` singleton is exported as `locationTracker`.
 *
 * Speed is transmitted in **miles per hour** (R16.18, R16.19). The device
 * reports metres per second; the conversion happens once, in `lib/units.ts`,
 * and nowhere else.
 *
 * Requirements: 10.9, 10.10, 10.11, 10.12, 16.4
 */

import NetInfo from '@react-native-community/netinfo';
import * as Location from 'expo-location';
import * as TaskManager from 'expo-task-manager';
import { MMKV } from 'react-native-mmkv';

import { apiSend, type ApiRequestOptions, type ApiSendResult } from './api-client';
import { storedDutyStatus, type DutyStatus } from './duty-api';
import { classifyResponse } from './offline-queue';
import { milesPerHourFromMetersPerSecond } from './units';

/** R10.9 — the sampling interval's upper bound while on duty. */
export const MAX_SAMPLE_INTERVAL_MS = 60_000;

/** A floor, so a caller cannot ask the GPS for a fix every millisecond. */
export const MIN_SAMPLE_INTERVAL_MS = 5_000;

/** R10.11 — how many samples the device holds before the oldest is discarded. */
export const BREADCRUMB_BUFFER_LIMIT = 5000;

/** R10.12 — the largest batch the breadcrumb endpoint accepts. */
export const BREADCRUMB_BATCH_LIMIT = 200;

/** The batch endpoint. The body carries `samples` and no driver identifier (R10.2). */
export const BREADCRUMB_PATH = '/api/driver/telemetry/breadcrumbs';

/** The `expo-task-manager` task that samples while the app is backgrounded. */
export const BREADCRUMB_TASK_NAME = 'runsheet-driver-breadcrumbs';

/** The duty statuses that sample (R10.9). Every other value stops (R10.10). */
export const SAMPLING_DUTY_STATUSES: readonly DutyStatus[] = ['active', 'on_break'];

/**
 * One breadcrumb, in the exact shape `POST /api/driver/telemetry/breadcrumbs`
 * reads: speed already in miles per hour, accuracy in metres, heading in
 * degrees, and a client ISO 8601 `sample_timestamp` (R10.1).
 */
export interface BreadcrumbSample {
  latitude: number;
  longitude: number;
  /** ISO-8601 capture time. The server discards samples older than 24 hours (R10.7). */
  sample_timestamp: string;
  accuracy_meters: number | null;
  speed_mph: number | null;
  heading_degrees: number | null;
}

/**
 * The donor's sample shape, kept so the poller still reads as the donor's
 * poller: device units, nullable, straight off `expo-location`.
 */
export interface LocationSample {
  latitude: number;
  longitude: number;
  accuracy?: number | null;
  /** Metres per second, as the device reports it. Never transmitted in this unit. */
  speed?: number | null;
  heading?: number | null;
  sample_timestamp: string;
}

/** Where sampled positions go. Defaults to the ring buffer. */
export type LocationSink = (sample: LocationSample) => void | Promise<void>;

/** The transport the drain sends through. Defaults to {@link apiSend}. */
export type BreadcrumbTransport = (options: ApiRequestOptions) => Promise<ApiSendResult>;

/** `true` while the driver's duty status calls for sampling (R10.9, R10.10). */
export function shouldSampleLocation(status: DutyStatus | null | undefined): boolean {
  return status ? SAMPLING_DUTY_STATUSES.includes(status) : false;
}

function isUsable(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

/**
 * Device reading → wire sample.
 *
 * The platforms report `-1` for a speed, heading, or accuracy they do not have.
 * A negative reading is therefore absent rather than negative, and travels as
 * `null` instead of as a fabricated number. Metres per second become miles per
 * hour here, through the one conversion in `lib/units.ts` (R16.18).
 */
export function toBreadcrumbSample(sample: LocationSample): BreadcrumbSample {
  return {
    latitude: sample.latitude,
    longitude: sample.longitude,
    sample_timestamp: sample.sample_timestamp,
    accuracy_meters:
      isUsable(sample.accuracy) && sample.accuracy >= 0 ? sample.accuracy : null,
    speed_mph: milesPerHourFromMetersPerSecond(sample.speed),
    heading_degrees:
      isUsable(sample.heading) && sample.heading >= 0 ? sample.heading : null,
  };
}

/** A position reading from `expo-location`, as the donor read it. */
export function toLocationSample(position: Location.LocationObject): LocationSample {
  return {
    latitude: position.coords.latitude,
    longitude: position.coords.longitude,
    accuracy: position.coords.accuracy,
    speed: position.coords.speed,
    heading: position.coords.heading,
    sample_timestamp: new Date(position.timestamp).toISOString(),
  };
}

// ---------------------------------------------------------------------------
// The ring buffer (R10.11)
// ---------------------------------------------------------------------------

const BUFFER_STORAGE_ID = 'runsheet-breadcrumbs';
const BUFFER_STORAGE_KEY = 'buffer';

/** The slice of key-value storage the buffer needs. Injectable for tests. */
export interface BreadcrumbBufferStore {
  getString(key: string): string | undefined;
  set(key: string, value: string): void;
  delete(key: string): void;
}

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

function resolveDefaultStore(): BreadcrumbBufferStore {
  try {
    return new MMKV({ id: BUFFER_STORAGE_ID });
  } catch {
    // No native MMKV here (Jest, web preview). The buffer then lives for the
    // life of the process, which is all a preview build needs.
    return createMemoryStore();
  }
}

/**
 * A bounded, device-resident hold for samples that have not been accepted yet.
 *
 * Bounded is the point: a driver in a dead zone for a week must not fill the
 * device. Past {@link BREADCRUMB_BUFFER_LIMIT} the **oldest** sample is
 * discarded, because the newest positions are the ones dispatch can still act
 * on (R10.11).
 */
export class BreadcrumbBuffer {
  private samples: BreadcrumbSample[] | null = null;

  constructor(
    private readonly capacity: number = BREADCRUMB_BUFFER_LIMIT,
    private store: BreadcrumbBufferStore = resolveDefaultStore(),
  ) {}

  /** Swap the backing store. Tests only. */
  public setStore(store: BreadcrumbBufferStore): void {
    this.store = store;
    this.samples = null;
  }

  private load(): BreadcrumbSample[] {
    if (this.samples) {
      return this.samples;
    }
    let restored: BreadcrumbSample[] = [];
    const raw = this.store.getString(BUFFER_STORAGE_KEY);
    if (raw) {
      try {
        const parsed = JSON.parse(raw) as unknown;
        if (Array.isArray(parsed)) {
          restored = parsed.filter(
            (entry): entry is BreadcrumbSample =>
              !!entry &&
              typeof entry === 'object' &&
              typeof (entry as BreadcrumbSample).latitude === 'number' &&
              typeof (entry as BreadcrumbSample).longitude === 'number' &&
              typeof (entry as BreadcrumbSample).sample_timestamp === 'string',
          );
        }
      } catch {
        // A corrupt buffer is not worth a crash; the track resumes empty.
      }
    }
    this.samples = restored.slice(-this.capacity);
    return this.samples;
  }

  private persist(): void {
    const samples = this.load();
    if (samples.length === 0) {
      this.store.delete(BUFFER_STORAGE_KEY);
      return;
    }
    this.store.set(BUFFER_STORAGE_KEY, JSON.stringify(samples));
  }

  /**
   * Append one sample.
   *
   * @returns how many oldest samples were discarded to make room (R10.11).
   */
  public push(sample: BreadcrumbSample): number {
    const samples = this.load();
    samples.push(sample);
    let discarded = 0;
    while (samples.length > this.capacity) {
      samples.shift();
      discarded += 1;
    }
    this.persist();
    return discarded;
  }

  public size(): number {
    return this.load().length;
  }

  /** Every held sample in ascending `sample_timestamp` order. */
  public snapshot(): BreadcrumbSample[] {
    return this.sorted().slice();
  }

  /**
   * The next submission batch: the earliest samples first, at most `limit` of
   * them (R10.12). Ordering is by `sample_timestamp` rather than by arrival, so
   * a device clock that stepped backwards still submits in timestamp order.
   */
  public nextBatch(limit: number = BREADCRUMB_BATCH_LIMIT): BreadcrumbSample[] {
    return this.sorted().slice(0, Math.max(0, limit));
  }

  /** Drop the `count` earliest samples — the batch the server accepted. */
  public dropEarliest(count: number): void {
    if (count <= 0) {
      return;
    }
    const samples = this.sorted();
    samples.splice(0, count);
    this.persist();
  }

  public clear(): void {
    this.samples = [];
    this.persist();
  }

  /** Sort in place, so `nextBatch` and `dropEarliest` address the same order. */
  private sorted(): BreadcrumbSample[] {
    const samples = this.load();
    samples.sort((left, right) =>
      left.sample_timestamp < right.sample_timestamp
        ? -1
        : left.sample_timestamp > right.sample_timestamp
          ? 1
          : 0,
    );
    return samples;
  }
}

/** The app's buffer. One per device. */
export const breadcrumbBuffer = new BreadcrumbBuffer();

// ---------------------------------------------------------------------------
// The drain (R10.12)
// ---------------------------------------------------------------------------

let transport: BreadcrumbTransport = apiSend;

/** Override the transport and the buffer store. Tests only. */
export function configureLocationTracker(next: {
  transport?: BreadcrumbTransport | null;
  bufferStore?: BreadcrumbBufferStore | null;
}): void {
  if (next.transport !== undefined) {
    transport = next.transport ?? apiSend;
  }
  if (next.bufferStore !== undefined) {
    breadcrumbBuffer.setStore(next.bufferStore ?? createMemoryStore());
  }
}

/** Restore module defaults. Tests only. */
export function resetLocationTracker(): void {
  transport = apiSend;
  draining = null;
}

/** Why a drain pass stopped. */
export type BreadcrumbDrainStop = 'complete' | 'offline' | 'retry_later';

export interface BreadcrumbDrainSummary {
  /** Samples the server accepted. */
  submitted: number;
  /** Batches sent, accepted or not. */
  batches: number;
  /**
   * Samples dropped because the server will never accept them — a 4xx that is
   * not a transient conflict. Holding them would stall every later sample
   * behind a batch that cannot succeed.
   */
  rejected: number;
  stopped: BreadcrumbDrainStop;
}

let draining: Promise<BreadcrumbDrainSummary> | null = null;

/**
 * Submit the buffer in ascending `sample_timestamp` batches of at most 200
 * (R10.12).
 *
 * The disposition of each batch is `lib/offline-queue.ts`'s
 * {@link classifyResponse} — the same matrix every other driver write is judged
 * by, so a 429 or a 5xx waits for the next connectivity event instead of
 * spinning here. Breadcrumbs are not one of R11.8's seven queued mutation
 * kinds, so they are not enqueued; the ring buffer is their durability.
 *
 * Concurrent callers — the NetInfo listener and the background task both fire
 * on reconnection — receive the in-flight pass rather than a second one, which
 * is what keeps a batch from being sent twice.
 */
export function drainBreadcrumbs(): Promise<BreadcrumbDrainSummary> {
  if (draining) {
    return draining;
  }
  draining = runDrainPass().finally(() => {
    draining = null;
  });
  return draining;
}

async function runDrainPass(): Promise<BreadcrumbDrainSummary> {
  const summary: BreadcrumbDrainSummary = {
    submitted: 0,
    batches: 0,
    rejected: 0,
    stopped: 'complete',
  };

  while (breadcrumbBuffer.size() > 0) {
    const batch = breadcrumbBuffer.nextBatch(BREADCRUMB_BATCH_LIMIT);
    if (batch.length === 0) {
      break;
    }

    let result: ApiSendResult;
    try {
      result = await transport({
        method: 'POST',
        path: BREADCRUMB_PATH,
        body: { samples: batch },
      });
    } catch {
      // A transport that throws for a reason other than "no response" — an
      // unset base URL, say — must not spin the buffer.
      result = { kind: 'no_response' };
    }

    summary.batches += 1;
    const disposition = classifyResponse(result);
    switch (disposition.kind) {
      case 'success':
        breadcrumbBuffer.dropEarliest(batch.length);
        summary.submitted += batch.length;
        break;
      case 'conflict':
      case 'terminal':
        breadcrumbBuffer.dropEarliest(batch.length);
        summary.rejected += batch.length;
        break;
      case 'retry':
        summary.stopped = 'retry_later';
        return summary;
      case 'offline':
        summary.stopped = 'offline';
        return summary;
    }
  }

  return summary;
}

// ---------------------------------------------------------------------------
// Background sampling (`expo-task-manager`)
// ---------------------------------------------------------------------------

interface BackgroundLocationPayload {
  locations?: Location.LocationObject[];
}

/**
 * Defined at module scope, as `expo-task-manager` requires: the OS may spin the
 * JavaScript bundle up in the background with no view mounted, and the task has
 * to already exist when it does.
 */
function defineBreadcrumbTask(): void {
  try {
    if (TaskManager.isTaskDefined(BREADCRUMB_TASK_NAME)) {
      return;
    }
    TaskManager.defineTask<BackgroundLocationPayload>(
      BREADCRUMB_TASK_NAME,
      async ({ data, error }) => {
        if (error) {
          return;
        }
        for (const position of data?.locations ?? []) {
          breadcrumbBuffer.push(toBreadcrumbSample(toLocationSample(position)));
        }
        await drainBreadcrumbs().catch(() => undefined);
      },
    );
  } catch {
    // No task manager on this platform (web preview). The foreground timer is
    // then the only sampler, which is a reduced track, not a broken app.
  }
}

defineBreadcrumbTask();

async function startBackgroundSampling(intervalMs: number): Promise<boolean> {
  try {
    const permission = await Location.getBackgroundPermissionsAsync();
    if (!permission.granted) {
      // Background location was never granted. Foreground sampling continues.
      return false;
    }
    if (await Location.hasStartedLocationUpdatesAsync(BREADCRUMB_TASK_NAME)) {
      return true;
    }
    await Location.startLocationUpdatesAsync(BREADCRUMB_TASK_NAME, {
      accuracy: Location.Accuracy.High,
      timeInterval: intervalMs,
      distanceInterval: 0,
      pausesUpdatesAutomatically: false,
      foregroundService: {
        notificationTitle: 'Runsheet is recording your route',
        notificationBody: 'Location sharing stops when you go off duty.',
      },
    });
    return true;
  } catch {
    return false;
  }
}

async function stopBackgroundSampling(): Promise<void> {
  try {
    if (await Location.hasStartedLocationUpdatesAsync(BREADCRUMB_TASK_NAME)) {
      await Location.stopLocationUpdatesAsync(BREADCRUMB_TASK_NAME);
    }
  } catch {
    // Nothing was running, or the platform has no background updates.
  }
}

// ---------------------------------------------------------------------------
// The poller
// ---------------------------------------------------------------------------

function debug(message: string): void {
  if (!__DEV__) {
    return;
  }
  // eslint-disable-next-line no-console
  console.log(`[breadcrumbs] ${message}`);
}

/**
 * The donor's interval poller, bound to duty status and feeding the ring buffer.
 */
class LocationTracker {
  private timerId: ReturnType<typeof setInterval> | null = null;
  private isPolling = false;
  private pollingInterval = MAX_SAMPLE_INTERVAL_MS; // R10.9 upper bound
  private sink: LocationSink | null = null;
  private dutyStatus: DutyStatus | null = null;
  private netInfoUnsubscribe: (() => void) | null = null;

  /**
   * Install a destination for sampled positions. The default destination is the
   * ring buffer; a sink replaces it, which is what lets a test observe the
   * capture without a buffer.
   */
  public setSink(sink: LocationSink | null) {
    this.sink = sink;
  }

  /**
   * Adopt the stored duty status and start draining on reconnection (R10.12).
   *
   * Called once from the app bootstrap. Importing this module subscribes to
   * nothing.
   */
  public async initialize(): Promise<void> {
    if (!this.netInfoUnsubscribe) {
      this.netInfoUnsubscribe = NetInfo.addEventListener((state) => {
        if (state.isConnected) {
          void drainBreadcrumbs().catch(() => undefined);
        }
      });
    }
    await this.applyDutyStatus(storedDutyStatus());
  }

  /** Release the connectivity subscription and stop sampling. */
  public async shutdown(): Promise<void> {
    this.netInfoUnsubscribe?.();
    this.netInfoUnsubscribe = null;
    await this.applyDutyStatus('off_duty');
  }

  /**
   * Bind sampling to duty status (R10.9, R10.10).
   *
   * `active` and `on_break` sample; `off_duty` and `inactive` — and an unknown
   * status, because sampling a driver whose status this device cannot name is
   * not something to guess at — stop.
   */
  public async applyDutyStatus(status: DutyStatus | null): Promise<void> {
    this.dutyStatus = status;
    if (shouldSampleLocation(status)) {
      this.startPolling(this.pollingInterval);
      await startBackgroundSampling(this.pollingInterval);
      return;
    }
    this.stopPolling();
    await stopBackgroundSampling();
  }

  /** The duty status sampling is currently bound to. */
  public currentDutyStatus(): DutyStatus | null {
    return this.dutyStatus;
  }

  /**
   * Start polling for location updates.
   *
   * The interval is clamped to {@link MAX_SAMPLE_INTERVAL_MS}, so no caller can
   * widen the gap past the 60 seconds R10.9 allows.
   */
  public startPolling(intervalMs: number = MAX_SAMPLE_INTERVAL_MS) {
    const interval = Math.min(
      MAX_SAMPLE_INTERVAL_MS,
      Math.max(MIN_SAMPLE_INTERVAL_MS, intervalMs),
    );

    if (this.timerId) {
      if (interval === this.pollingInterval) {
        return;
      }
      // A new interval means a new timer; the old one is not left running.
      this.stopPolling();
    }

    this.pollingInterval = interval;
    this.isPolling = true;

    // Send one immediate update
    void this._sendLocationUpdate();

    // Set up interval for periodic updates
    this.timerId = setInterval(() => {
      void this._sendLocationUpdate();
    }, this.pollingInterval);

    debug(`sampling started (interval: ${this.pollingInterval / 1000}s)`);
  }

  /**
   * Stop polling for location updates.
   * Call this when the driver goes off duty or signs out.
   */
  public stopPolling() {
    if (this.timerId) {
      clearInterval(this.timerId);
      this.timerId = null;
      this.isPolling = false;
      debug('sampling stopped');
    }
  }

  /** Whether the foreground timer is running. */
  public isActive() {
    return this.isPolling;
  }

  /** The current sampling interval in milliseconds. */
  public intervalMs() {
    return this.pollingInterval;
  }

  /**
   * Fetch the GPS position and hand it to the sink, or to the ring buffer when
   * no sink is installed.
   */
  private async _sendLocationUpdate() {
    try {
      // 1. Get current GPS location from the device
      const position = await Location.getCurrentPositionAsync({
        accuracy: Location.Accuracy.High,
      });

      const sample = toLocationSample(position);

      // 2. Hold it on the device. No endpoint is called from the capture path:
      //    submission is the drain's job, in batches, on connectivity (R10.12).
      if (this.sink) {
        await this.sink(sample);
        return;
      }
      breadcrumbBuffer.push(toBreadcrumbSample(sample));
    } catch (error) {
      // A denied permission or a cold radio is not a failure worth surfacing —
      // R10.15 keeps the rest of the app working with location sharing off.
      if (error instanceof Error) {
        debug(`no fix: ${error.message}`);
      }
    }
  }

  /** Manually trigger one sample (useful for testing or one-off updates). */
  public async sendLocationNow() {
    await this._sendLocationUpdate();
  }
}

/** The app's tracker. The donor called this singleton `simpleLocationPoller`. */
export const locationTracker = new LocationTracker();
