/**
 * Copied from azumi-rider/lib/notification-manager.ts
 * Copied: 2026-07-29
 * Adapted: 2026-07-29 (task 18.7)
 * Donor: azumi-rider (Expo SDK 53). Requirement 16.4 names what is retained:
 * the notification permission and registration state machine, the retry on
 * network reconnection, and the 24-hour periodic refresh. All three are here,
 * with the donor's trigger list intact (Requirements 16.2, 16.3, 16.4).
 *
 * Core principle, retained from the donor: always register, never check first.
 * `PUT /api/driver/devices/{device_id}` is an upsert on the composite document
 * id `{tenant_id}:{driver_id}:{device_id}`, so a re-registration replaces the
 * record and refreshes `last_seen_at` instead of creating a second row (R9.2).
 *
 * Trigger points (donor list, one changed):
 *   1. App startup — `start()` from the root layout, not the constructor. The
 *      donor built its singleton at module import, which fired permission reads
 *      and a network write as a side effect of `import`. Construction is inert
 *      here and every subscription is installed by `start()`.
 *   2. After sign-in — `retry()`.
 *   3. Every 24 hours — the periodic refresh.
 *   4. On network reconnection — the NetInfo listener.
 *   5. On permission grant — `requestPermission()`.
 *   6. On foreground — permission is re-read, because the driver may have
 *      changed it in the operating-system settings while the app was away.
 *
 * What task 18.7 changed from the copy:
 *   - The token write is `PUT /api/driver/devices/{device_id}` carrying the
 *     Bearer session, through this app's own `apiSend`. The donor's
 *     `authenticatedFetch` from `azumi-rider/lib/api-client.ts` is not one of
 *     the six copied artifacts — it logs the `Authorization` header and both
 *     bodies on every call (R15.1, R15.2).
 *   - The EAS `projectId` is read from the Runsheet `app.config.ts` `extra.eas`
 *     block, which comes from `RUNSHEET_EAS_PROJECT_ID`. No donor project id
 *     exists in this tree.
 *   - A push received while the app is running invalidates the cached
 *     assigned-work list and nothing else — an invalidation, never a state
 *     write, so the detail is always re-fetched over an authenticated request
 *     (R9.11, R9.8).
 *   - Denied permission is exposed as `alertsDisabled` on the snapshot, with
 *     `openNotificationSettings()` alongside it, so `components/PermissionBanner`
 *     (task 18.9) can render the persistent indicator and its settings link
 *     without knowing anything about Expo (R9.12).
 *   - Sign-out calls `DELETE /api/driver/devices/{device_id}` (R9.3).
 *   - Every log statement is gated on `__DEV__` and carries no push token, no
 *     session token, and no `Authorization` value (R15.1, R15.2).
 *
 * The device id keeps living in `react-native-mmkv`, which holds no credential;
 * the session tokens are in `expo-secure-store` and are reached only through
 * `apiSend` (R15.3).
 *
 * Requirements: 9.1, 9.2, 9.3, 9.11, 9.12, 16.2, 16.3, 16.4
 */

import NetInfo from '@react-native-community/netinfo';
import type { QueryClient } from '@tanstack/react-query';
import Constants from 'expo-constants';
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';
import { AppState, Linking, Platform, type AppStateStatus } from 'react-native';
import { MMKV } from 'react-native-mmkv';

import { apiSend } from '@/lib/api-client';
import { WORK_SCOPE } from '@/lib/query-keys';
import { currentSessionIdentity } from '@/lib/session';

const storage = new MMKV();

/**
 * Registration state machine, retained from the donor.
 *
 * `needs_registration` is the state the donor declared but never entered; here
 * it means "permission is granted but there is no session to register against",
 * which is exactly what `retry()` after sign-in resolves.
 */
export type NotificationState =
  | 'checking'
  | 'ready'
  | 'needs_permission'
  | 'needs_registration'
  | 'error';

/** Device permission as reported by the operating system. */
export type NotificationPermission = 'granted' | 'denied' | 'undetermined';

/** Everything the UI needs, with no Expo type in it (R9.12). */
export interface NotificationSnapshot {
  state: NotificationState;
  permission: NotificationPermission;
  /** `false` once the driver has denied at the operating-system level. */
  canAskAgain: boolean;
  /**
   * Requirement 9.12 — render the persistent in-app indicator while this is
   * true. True when permission is denied, or when it is merely undetermined and
   * the operating system will no longer show a prompt.
   */
  alertsDisabled: boolean;
}

type SnapshotListener = (snapshot: NotificationSnapshot) => void;

const DEVICE_ID_KEY = 'device_id';
const TWENTY_FOUR_HOURS = 24 * 60 * 60 * 1000;
/** `PUT /api/driver/devices/{device_id}` bounds the path segment at 128. */
const MAX_DEVICE_ID_LENGTH = 48;

/** `driver_devices.platform` is Runsheet's own two-value enumeration. */
type DevicePlatform = 'ios' | 'android';

function devicePlatform(): DevicePlatform | null {
  if (Platform.OS === 'ios' || Platform.OS === 'android') {
    return Platform.OS;
  }
  return null;
}

/** The Runsheet EAS project id from `app.config.ts`. Never a donor value. */
function easProjectId(): string | null {
  const extra = Constants.expoConfig?.extra as
    | { eas?: { projectId?: unknown } }
    | undefined;
  const projectId = extra?.eas?.projectId;
  return typeof projectId === 'string' && projectId.trim().length > 0
    ? projectId.trim()
    : null;
}

/**
 * The only logging in this module: a short label, never a token value, never a
 * request or response body, and silent outside a development build
 * (Requirements 15.1, 15.2).
 */
function debug(message: string): void {
  if (!__DEV__) {
    return;
  }
  // eslint-disable-next-line no-console
  console.log(`[notifications] ${message}`);
}

/**
 * The stable per-installation device identifier.
 *
 * Also the value `signIn({ deviceId })` sends, so the session and the device
 * registration name the same device.
 */
export function driverDeviceId(): string {
  let deviceId = storage.getString(DEVICE_ID_KEY);

  if (!deviceId) {
    const timestamp = Date.now();
    const random = Math.random().toString(36).substring(2, 15);
    const model = Device.modelId || Device.modelName || 'unknown';
    deviceId = `driver-${model}-${timestamp}-${random}`
      .replace(/\s+/g, '-')
      .substring(0, MAX_DEVICE_ID_LENGTH);
    storage.set(DEVICE_ID_KEY, deviceId);
  }

  return deviceId;
}

class NotificationManager {
  private static instance: NotificationManager;

  private state: NotificationState = 'checking';
  private permission: NotificationPermission = 'undetermined';
  private canAskAgain = true;
  private snapshot: NotificationSnapshot = {
    state: 'checking',
    permission: 'undetermined',
    canAskAgain: true,
    alertsDisabled: false,
  };

  private listeners = new Set<SnapshotListener>();
  private queryClient: QueryClient | null = null;

  private periodicTimer: ReturnType<typeof setInterval> | null = null;
  private netInfoUnsubscribe: (() => void) | null = null;
  private appStateSubscription: { remove: () => void } | null = null;
  private receivedSubscription: { remove: () => void } | null = null;
  private responseSubscription: { remove: () => void } | null = null;

  private isRegistering = false;
  private started = false;

  /** Construction is inert. Every side effect belongs to `start()`. */
  private constructor() {}

  static getInstance(): NotificationManager {
    if (!NotificationManager.instance) {
      NotificationManager.instance = new NotificationManager();
    }
    return NotificationManager.instance;
  }

  // --- Public API ---

  /**
   * Install the listeners and kick off the first registration attempt.
   *
   * Called once from the root layout with the app's `QueryClient`. Idempotent —
   * a second call only re-points the query client.
   */
  start(options: { queryClient?: QueryClient } = {}): void {
    if (options.queryClient) {
      this.queryClient = options.queryClient;
    }
    if (this.started) {
      return;
    }
    this.started = true;

    this.startPushListeners();
    this.startPeriodicRefresh();
    this.startNetworkListener();
    this.startAppStateListener();

    // Non-blocking: a permission read and a network write must not delay the
    // first frame.
    void this.initialize();
  }

  /** Point the manager at the app's query client (R9.11). */
  setQueryClient(queryClient: QueryClient | null): void {
    this.queryClient = queryClient;
  }

  getState(): NotificationState {
    return this.state;
  }

  /** Stable snapshot reference — safe for `useSyncExternalStore`. */
  getSnapshot(): NotificationSnapshot {
    return this.snapshot;
  }

  /** Requirement 9.12 — the condition the persistent indicator renders on. */
  isPermissionDenied(): boolean {
    return this.snapshot.alertsDisabled;
  }

  /** Subscribe to snapshot changes. The current snapshot is delivered at once. */
  subscribe(listener: SnapshotListener): () => void {
    this.listeners.add(listener);
    listener(this.snapshot);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /**
   * Called after sign-in, or whenever registration has to be forced.
   * Fire-and-forget — does not block.
   */
  retry(): void {
    debug('retry triggered');
    this.checkAndRegister().catch(() => {
      debug('retry failed');
    });
  }

  /**
   * Ask the operating system for permission. On a grant, register immediately.
   */
  async requestPermission(): Promise<boolean> {
    try {
      const result = await Notifications.requestPermissionsAsync();
      this.adoptPermission(result.status, result.canAskAgain);
      if (result.status === 'granted') {
        this.setState('checking');
        await this.registerToken();
        return true;
      }
      this.setState('needs_permission');
      return false;
    } catch {
      debug('requestPermission failed');
      this.setState('error');
      return false;
    }
  }

  /**
   * Requirement 9.12 — the settings link beside the persistent indicator.
   *
   * `Linking.openSettings()` reaches this app's settings page, which is where
   * the notification switch lives, on both iOS and Android. The donor's
   * `Linking.openURL('app-settings:')` is iOS-only.
   */
  async openNotificationSettings(): Promise<boolean> {
    try {
      await Linking.openSettings();
      return true;
    } catch {
      debug('could not open the operating-system settings');
      return false;
    }
  }

  /**
   * Re-read the operating-system permission without registering.
   *
   * The driver can flip the notification switch in the settings app while this
   * app is backgrounded, so the indicator would otherwise be stale.
   */
  async refreshPermission(): Promise<NotificationPermission> {
    try {
      const result = await Notifications.getPermissionsAsync();
      this.adoptPermission(result.status, result.canAskAgain);
      return this.permission;
    } catch {
      debug('permission read failed');
      return this.permission;
    }
  }

  /**
   * Requirement 9.3 — sign-out deletes this device's registration.
   *
   * Must be called **before** `signOut()` from `lib/session.ts`, because the
   * `DELETE` carries the Bearer session that sign-out is about to revoke. The
   * profile screen's sign-out control (task 18.8) is the one caller.
   *
   * @returns `true` when the server confirmed the delete. `false` — offline, or
   *   no session — is not an error worth blocking sign-out on: the record is
   *   also pruned when the provider reports the token dead (R9.4), and the next
   *   registration for this `device_id` replaces it (R9.2).
   */
  async deregisterDevice(): Promise<boolean> {
    if (!currentSessionIdentity()) {
      return false;
    }
    try {
      const result = await apiSend({
        method: 'DELETE',
        path: `/api/driver/devices/${encodeURIComponent(driverDeviceId())}`,
      });
      const deleted = result.kind === 'response' && result.ok;
      debug(deleted ? 'device registration deleted' : 'device deregistration rejected');
      if (deleted) {
        this.setState('needs_registration');
      }
      return deleted;
    } catch {
      debug('device deregistration failed');
      return false;
    }
  }

  /** Clear the badge count. */
  async clearBadge(): Promise<void> {
    try {
      await Notifications.setBadgeCountAsync(0);
    } catch {
      // A platform that does not support badges is not an error worth surfacing.
    }
  }

  destroy(): void {
    if (this.periodicTimer) clearInterval(this.periodicTimer);
    this.periodicTimer = null;
    this.netInfoUnsubscribe?.();
    this.netInfoUnsubscribe = null;
    this.appStateSubscription?.remove();
    this.appStateSubscription = null;
    this.receivedSubscription?.remove();
    this.receivedSubscription = null;
    this.responseSubscription?.remove();
    this.responseSubscription = null;
    this.listeners.clear();
    this.started = false;
  }

  // --- Private ---

  private publish(): void {
    const alertsDisabled =
      this.permission === 'denied' ||
      (this.permission === 'undetermined' && !this.canAskAgain);

    const next: NotificationSnapshot = {
      state: this.state,
      permission: this.permission,
      canAskAgain: this.canAskAgain,
      alertsDisabled,
    };

    if (
      next.state === this.snapshot.state &&
      next.permission === this.snapshot.permission &&
      next.canAskAgain === this.snapshot.canAskAgain &&
      next.alertsDisabled === this.snapshot.alertsDisabled
    ) {
      return;
    }

    this.snapshot = next;
    this.listeners.forEach((listener) => listener(next));
  }

  private setState(newState: NotificationState): void {
    if (this.state === newState) return;
    this.state = newState;
    debug(`state -> ${newState}`);
    this.publish();
  }

  private adoptPermission(status: string, canAskAgain: boolean): void {
    this.permission =
      status === 'granted' ? 'granted' : status === 'denied' ? 'denied' : 'undetermined';
    this.canAskAgain = canAskAgain;
    this.publish();
  }

  private async initialize(): Promise<void> {
    // Push tokens are not issued to a simulator, and `platform` has no value
    // outside iOS and Android.
    if (!Device.isDevice || !devicePlatform()) {
      debug('no push-capable device, skipping registration');
      this.setState('ready');
      return;
    }

    await this.checkAndRegister();
  }

  private async checkAndRegister(): Promise<void> {
    try {
      const { status, canAskAgain } = await Notifications.getPermissionsAsync();
      this.adoptPermission(status, canAskAgain);

      if (status !== 'granted') {
        this.setState('needs_permission');
        return;
      }

      // Always register — the backend upserts (R9.2).
      await this.registerToken();
    } catch {
      debug('checkAndRegister failed');
      this.setState('error');
    }
  }

  private async registerToken(): Promise<void> {
    // Prevent concurrent registrations.
    if (this.isRegistering) return;
    this.isRegistering = true;

    try {
      const platform = devicePlatform();
      if (!platform) {
        this.setState('ready');
        return;
      }

      // The `PUT` carries the Bearer session, so a registration before sign-in
      // would only earn a 401. `retry()` runs it again once the session exists.
      if (!currentSessionIdentity()) {
        debug('no session yet, deferring registration');
        this.setState('needs_registration');
        return;
      }

      const projectId = easProjectId();
      if (!projectId) {
        debug('no EAS project id configured');
        this.setState('error');
        return;
      }

      const token = await Notifications.getExpoPushTokenAsync({ projectId });

      const result = await apiSend({
        method: 'PUT',
        path: `/api/driver/devices/${encodeURIComponent(driverDeviceId())}`,
        body: {
          // Opaque to this app and to the registry (R9.18). Never logged.
          push_token: token.data,
          platform,
          app_version: Constants.expoConfig?.version ?? undefined,
        },
      });

      if (result.kind === 'response' && result.ok) {
        debug('device registration upserted');
        this.setState('ready');
      } else if (result.kind === 'no_response') {
        // Offline. The NetInfo listener retries on reconnection (R16.4).
        this.setState('needs_registration');
      } else {
        debug('device registration rejected');
        this.setState('error');
      }
    } catch {
      debug('registerToken failed');
      this.setState('error');
    } finally {
      this.isRegistering = false;
    }
  }

  /**
   * Requirement 9.11 — a push received while the app runs invalidates the
   * cached assigned-work list.
   *
   * Invalidation only: nothing from the payload is written into the cache, so
   * the next render fetches order detail over an authenticated request. The
   * `'work'` scope covers both `['work', filters]` and `['work', orderId]`.
   */
  private invalidateAssignedWork(): void {
    if (!this.queryClient) {
      return;
    }
    void this.queryClient.invalidateQueries({ queryKey: [WORK_SCOPE] });
  }

  private startPushListeners(): void {
    try {
      this.receivedSubscription = Notifications.addNotificationReceivedListener(() => {
        debug('push received, invalidating the assigned-work list');
        this.invalidateAssignedWork();
      });
      // A tap that resumes the app takes the same path: invalidate, then let
      // the screen fetch. The payload carries identifiers only (R9.8).
      this.responseSubscription = Notifications.addNotificationResponseReceivedListener(
        () => {
          this.invalidateAssignedWork();
        },
      );
    } catch {
      debug('push listeners unavailable on this platform');
    }
  }

  private startPeriodicRefresh(): void {
    // Re-register every 24 hours to keep the token fresh and `last_seen_at`
    // current. Retained from the donor (R16.4).
    this.periodicTimer = setInterval(() => {
      debug('periodic refresh');
      this.checkAndRegister().catch(() => {});
    }, TWENTY_FOUR_HOURS);
  }

  private startNetworkListener(): void {
    // Retry on reconnection. Retained from the donor (R16.4).
    this.netInfoUnsubscribe = NetInfo.addEventListener((netState) => {
      if (
        netState.isConnected &&
        (this.state === 'error' || this.state === 'needs_registration')
      ) {
        debug('network reconnected, retrying registration');
        this.checkAndRegister().catch(() => {});
      }
    });
  }

  private startAppStateListener(): void {
    this.appStateSubscription = AppState.addEventListener(
      'change',
      (nextState: AppStateStatus) => {
        if (nextState !== 'active') {
          return;
        }
        void this.clearBadge();
        // The permission may have changed in the settings app while we were
        // away; re-read it so the indicator is accurate, and register if the
        // driver has just enabled alerts (R9.12).
        void this.refreshPermission().then((permission) => {
          if (permission === 'granted' && this.state !== 'ready') {
            this.checkAndRegister().catch(() => {});
          }
        });
      },
    );
  }
}

/** The one manager. Inert until `start()` is called from the root layout. */
export const notificationManager = NotificationManager.getInstance();
