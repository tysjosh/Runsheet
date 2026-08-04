/**
 * Duty status — the three controls the driver sees and the one value the server
 * keeps.
 *
 * `fuel/order_models.py:63` declares `DriverStatus = active | inactive |
 * on_break | off_duty`, which carries **no** `on_duty` value. The driver surface
 * therefore maps its controls rather than inventing a vocabulary (R13.4, R13.5):
 *
 *   on-duty control  → `active`
 *   break control    → `on_break`   (a control of its own, not a modifier)
 *   off-duty control → `off_duty`
 *
 * `inactive` has no control at all: R13.2 makes a driver-submitted `inactive` a
 * 403, because it is an administrator-set value.
 *
 * The server is authoritative. {@link adoptServerDutyStatus} is called with the
 * value `GET /api/driver/me` returned on launch and replaces the stored value
 * whenever the two differ (R13.10). The stored copy exists only so the duty
 * control can render before the identity query resolves; it is never sent back
 * as a fact.
 *
 * Duty status is deliberately **not** an offline-queue mutation: R11.8 lists the
 * seven queued kinds and duty status is not among them, so a transition is a
 * live request carrying its own idempotency key.
 *
 * Requirements: 13.4, 13.5, 13.10, 1.11
 */

import { MMKV } from 'react-native-mmkv';

import { apiRequest } from './api-client';
import { generateIdempotencyKey } from './offline-queue';

/** `fuel/order_models.py:63` `DriverStatus`, verbatim. */
export type DutyStatus = 'active' | 'inactive' | 'on_break' | 'off_duty';

/** The three values a driver may transition to (R13.1). */
export type DriverSettableDutyStatus = Extract<
  DutyStatus,
  'active' | 'on_break' | 'off_duty'
>;

/** The controls the profile screen renders. One per settable status. */
export type DutyControl = 'on_duty' | 'on_break' | 'off_duty';

/** Control → wire value (R13.4, R13.5). */
export const DUTY_CONTROL_STATUS: Record<DutyControl, DriverSettableDutyStatus> =
  {
    on_duty: 'active',
    on_break: 'on_break',
    off_duty: 'off_duty',
  };

export interface DutyControlDescriptor {
  control: DutyControl;
  status: DriverSettableDutyStatus;
  label: string;
  description: string;
}

/**
 * The three controls, in the order the screen shows them. `on_break` sits
 * between the two so it reads as the distinct third state it is (R13.5).
 */
export const DUTY_CONTROLS: DutyControlDescriptor[] = [
  {
    control: 'on_duty',
    status: 'active',
    label: 'On duty',
    description: 'Available for dispatch. Assignment alerts are delivered.',
  },
  {
    control: 'on_break',
    status: 'on_break',
    label: 'On break',
    description: 'Still on the clock, not available for a new assignment.',
  },
  {
    control: 'off_duty',
    status: 'off_duty',
    label: 'Off duty',
    description: 'Assignment alerts are suppressed until you go back on duty.',
  },
];

/** Label for a duty status, including the administrator-set `inactive`. */
export function dutyStatusLabel(status: string | null | undefined): string {
  switch (status) {
    case 'active':
      return 'On duty';
    case 'on_break':
      return 'On break';
    case 'off_duty':
      return 'Off duty';
    case 'inactive':
      return 'Inactive (set by dispatch)';
    default:
      return 'Unknown';
  }
}

/** Which control, if any, a server value corresponds to. */
export function controlForStatus(
  status: string | null | undefined,
): DutyControl | null {
  const match = DUTY_CONTROLS.find((entry) => entry.status === status);
  return match ? match.control : null;
}

/** Wire shape of `GET /api/driver/me` (R1.11). */
export interface DriverIdentity {
  driver_id: string;
  driver_name?: string | null;
  assigned_truck_id?: string | null;
  duty_status?: string | null;
  duty_status_updated_at?: string | null;
}

interface DriverIdentityResponse {
  data: DriverIdentity;
}

/** Read the authenticated identity and the server's duty status (R1.11). */
export async function loadDriverIdentity(): Promise<DriverIdentity> {
  const response = await apiRequest<DriverIdentityResponse>({
    method: 'GET',
    path: '/api/driver/me',
  });
  return response.data;
}

// ---------------------------------------------------------------------------
// The stored copy
// ---------------------------------------------------------------------------

const STORAGE_ID = 'runsheet-duty-status';
const STORAGE_KEY = 'duty_status';

/** The slice of key-value storage this module needs. Injectable for tests. */
export interface DutyStatusStore {
  getString(key: string): string | undefined;
  set(key: string, value: string): void;
  delete(key: string): void;
}

function createMemoryStore(): DutyStatusStore {
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

let store: DutyStatusStore | null = null;

function resolveStore(): DutyStatusStore {
  if (!store) {
    try {
      store = new MMKV({ id: STORAGE_ID });
    } catch {
      // No native MMKV here (Jest, web preview). Losing durability only means
      // the screen waits for `GET /api/driver/me`, which is authoritative anyway.
      store = createMemoryStore();
    }
  }
  return store;
}

/** Override the store. Tests only. */
export function configureDutyStatusStore(next: {
  store?: DutyStatusStore | null;
}): void {
  if (next.store !== undefined) {
    store = next.store;
  }
}

function isDutyStatus(value: string | null | undefined): value is DutyStatus {
  return (
    value === 'active' ||
    value === 'inactive' ||
    value === 'on_break' ||
    value === 'off_duty'
  );
}

/** The last value this device saw, or `null` when it has seen none. */
export function storedDutyStatus(): DutyStatus | null {
  const raw = resolveStore().getString(STORAGE_KEY);
  return isDutyStatus(raw) ? raw : null;
}

/** Overwrite the stored copy. */
export function storeDutyStatus(status: DutyStatus): void {
  resolveStore().set(STORAGE_KEY, status);
}

/** Forget the stored copy — used when a session ends. */
export function forgetDutyStatus(): void {
  resolveStore().delete(STORAGE_KEY);
}

export interface DutyStatusAdoption {
  /** The value now in force — always the server's when it supplied one. */
  status: DutyStatus | null;
  /** `true` when the stored value differed and was replaced (R13.10). */
  adopted: boolean;
  /** What this device held before the adoption. */
  previous: DutyStatus | null;
}

/**
 * Adopt the server's duty status on launch (R13.10).
 *
 * An unrecognised or absent server value changes nothing: the stored copy is a
 * cache of a server fact, and a response that carries no fact is not a reason to
 * discard the last one.
 */
export function adoptServerDutyStatus(
  serverStatus: string | null | undefined,
): DutyStatusAdoption {
  const previous = storedDutyStatus();
  if (!isDutyStatus(serverStatus)) {
    return { status: previous, adopted: false, previous };
  }
  if (previous === serverStatus) {
    return { status: serverStatus, adopted: false, previous };
  }
  storeDutyStatus(serverStatus);
  return { status: serverStatus, adopted: true, previous };
}

// ---------------------------------------------------------------------------
// The transition
// ---------------------------------------------------------------------------

/** What `POST /api/driver/duty-status` answers with. */
export interface DutyStatusTransitionResult {
  data?: {
    new_status?: string;
    previous_status?: string;
    event_timestamp?: string;
  };
}

/**
 * Submit one duty-status transition.
 *
 * The control is mapped to its wire value here, so no screen writes a duty
 * status literal (R13.4, R13.5). The idempotency key is minted once, at the
 * moment the driver taps, and travels on the request (R11.6).
 */
export async function submitDutyStatus(
  control: DutyControl,
  options: { reason?: string } = {},
): Promise<{ status: DriverSettableDutyStatus }> {
  const status = DUTY_CONTROL_STATUS[control];
  await apiRequest<DutyStatusTransitionResult>({
    method: 'POST',
    path: '/api/driver/duty-status',
    idempotencyKey: generateIdempotencyKey(),
    body: {
      status,
      event_timestamp: new Date().toISOString(),
      ...(options.reason ? { reason: options.reason } : {}),
    },
  });
  storeDutyStatus(status);
  return { status };
}
