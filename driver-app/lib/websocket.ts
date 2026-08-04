/**
 * Adapted from azumi-rider/lib/websocket.ts
 * Copied: 2026-07-29
 * Adapted: 2026-07-29 (task 18.6)
 * Donor: azumi-rider (Expo SDK 53).
 *
 * **Retained from the donor** (Requirement 16.4): the network-aware
 * reconnection driven by a NetInfo subscription, the `AppState` foreground
 * check, the heartbeat send plus the 90-second client staleness detector
 * (donor `:33`), the connection timeout, the message-listener fan-out, and the
 * `maxReconnectAttempts = 25` ceiling (donor `:22`).
 *
 * **Changed for Runsheet:**
 *
 *  - Identity. The donor asserted identity through unauthenticated
 *    `?riderId=&userId=` query parameters (donor `:251`). This connects to
 *    `wss://{host}/ws/driver?token={access_token}` and places no `driver_id`
 *    and no `user_id` in the query string; the same value is also supplied in
 *    the React Native `headers` option as `Authorization: Bearer …`, which
 *    `bootstrap/websockets.py` prefers when the platform honours it. The server
 *    derives `driver_id` and `tenant_id` from the verified claims and closes
 *    `4001` when either is absent (R14.1, R14.2, R14.3).
 *  - The donor's dead `calculateReconnectDelay()` (donor `:389`) is deleted.
 *    Reconnection now runs the R14.5 ladder: `min(1s · 2^attempt, 30s)` with
 *    ±20 % jitter, at most 25 attempts. The donor's hard-coded 100/200/500 ms
 *    "immediate" retries, which made the attempt counter decorative, are gone.
 *  - Heartbeat send interval fixed at 30 s against the server's 120 s timeout
 *    (`driver/ws/driver_ws_manager.py:59`) — R14.4. Liveness is the donor's
 *    90 s threshold measured against the last `heartbeat_ack` / `pong`.
 *  - Event vocabulary. The donor's `["rider", …]` cache keys are replaced by
 *    the single registry in `lib/query-keys.ts`, and every event is an
 *    invalidation signal only: no payload is ever rendered as authoritative
 *    entity state, so order detail is always fetched over an authenticated
 *    request first (R14.9).
 *  - Nothing is ever sent except `heartbeat` and `location_update`. `ack`,
 *    `status_update`, and `exception` are REST operations (R14.11).
 *  - The NetInfo and `AppState` subscriptions move out of the constructor into
 *    `initialize()`, so importing this module subscribes to nothing and the
 *    seams below can be installed first.
 *  - Timer handles are typed `ReturnType<typeof setTimeout>` / `setInterval`
 *    instead of `number`, because this project has `@types/node` in scope.
 *  - No log statement carries the token, the URL query string, or an event
 *    payload (R15.1, R15.2).
 *
 * Requirements: 14.1, 14.4, 14.5, 14.6, 14.7, 14.8, 14.9, 14.11, 16.4
 */

import NetInfo from '@react-native-community/netinfo';
import { QueryClient } from '@tanstack/react-query';
import { AppState, type AppStateStatus } from 'react-native';

import { apiBaseUrl } from './api-client';
import { WORK_SCOPE, queryKeys } from './query-keys';
import { apiClientSessionBridge, subscribeToSession } from './session';

// ---------------------------------------------------------------------------
// Constants — every value below is a requirement, not a preference
// ---------------------------------------------------------------------------

/** Heartbeat send interval. The server times out at 120 s (R14.4). */
export const HEARTBEAT_INTERVAL_MS = 30_000;

/** Donor's client staleness threshold: no ack for this long → force reconnect. */
export const STALENESS_THRESHOLD_MS = 90_000;

/** How often the staleness detector runs. */
const STALENESS_CHECK_INTERVAL_MS = 15_000;

/** First rung of the reconnect ladder (R14.5). */
export const RECONNECT_BASE_MS = 1_000;

/** Ceiling of the reconnect ladder (R14.5). */
export const RECONNECT_CAP_MS = 30_000;

/** Attempt ceiling (R14.5, matches donor `:22`). */
export const MAX_RECONNECT_ATTEMPTS = 25;

/** Jitter fraction, so a fleet leaving a tunnel together does not resynchronize. */
export const RECONNECT_JITTER = 0.2;

/** Give up on a handshake that has not opened within this window. */
const CONNECTION_TIMEOUT_MS = 10_000;

/** Close code the server uses when the session carries no driver claims. */
export const CLOSE_UNAUTHORIZED = 4001;

/** Close code the server uses when the client stopped heartbeating. */
export const CLOSE_HEARTBEAT_TIMEOUT = 4002;

/** Close code this client uses for an intentional teardown. */
const CLOSE_NORMAL = 1000;

// ---------------------------------------------------------------------------
// Wire types
// ---------------------------------------------------------------------------

/**
 * Server-to-driver frame. `SERVER_TO_DRIVER_EVENTS` is
 * `{assignment, new_route, escalation, message, assignment_revoked}`
 * (`driver/ws/driver_ws_manager.py:26-33`), each carrying `{type, data,
 * timestamp}`.
 *
 * `data` is deliberately opaque: it is read for the identifiers that select a
 * cache key and for nothing else (R14.9).
 */
export interface WebSocketMessage {
  type?: string;
  data?: Record<string, unknown>;
  timestamp?: string;
}

export type DriverEventType =
  | 'assignment'
  | 'assignment_revoked'
  | 'new_route'
  | 'escalation'
  | 'message';

const DRIVER_EVENT_TYPES: readonly DriverEventType[] = [
  'assignment',
  'assignment_revoked',
  'new_route',
  'escalation',
  'message',
];

type MessageListener = (message: WebSocketMessage) => void;

export type ConnectionState =
  | 'idle'
  | 'connecting'
  | 'open'
  | 'waiting'
  | 'unauthorized'
  | 'exhausted';

export interface ConnectionStatus {
  state: ConnectionState;
  connected: boolean;
  attempts: number;
  maxAttempts: number;
  networkAvailable: boolean;
  /** Epoch ms of the last successful open, or 0. */
  lastConnected: number;
  /** Epoch ms of the last `heartbeat_ack` / `pong`, or 0. */
  lastAck: number;
}

/** The subset of `WebSocket` this module uses. Lets a test supply a fake. */
export interface WebSocketLike {
  readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((event?: unknown) => void) | null;
  onmessage: ((event: { data: string }) => void) | null;
  onerror: ((event?: unknown) => void) | null;
  onclose: ((event: { code: number; reason?: string }) => void) | null;
}

export interface WebSocketOptions {
  headers?: Record<string, string>;
}

export type WebSocketFactory = (
  url: string,
  protocols: string[] | undefined,
  options: WebSocketOptions,
) => WebSocketLike;

const OPEN = 1;
const CONNECTING = 0;

/**
 * React Native's `WebSocket` accepts a third `options` argument carrying
 * handshake `headers`; the DOM lib type does not declare it, hence the cast.
 * On a platform that ignores the option the `?token=` credential still
 * authenticates the handshake, which is why both are supplied.
 */
const defaultSocketFactory: WebSocketFactory = (url, protocols, options) =>
  new (WebSocket as unknown as new (
    url: string,
    protocols: string[] | undefined,
    options: WebSocketOptions,
  ) => WebSocketLike)(url, protocols, options);

// ---------------------------------------------------------------------------
// URL construction
// ---------------------------------------------------------------------------

/**
 * `wss://{host}/ws/driver?token={access_token}`.
 *
 * The token is the only query parameter. R14.1 forbids a driver identifier or a
 * user identifier here, and `assertTls` inside `apiBaseUrl()` guarantees the
 * origin is `https://`, so the handshake is always `wss://` (R15.4).
 */
export function buildDriverWsUrl(accessToken: string, baseUrl: string = apiBaseUrl()): string {
  const wsOrigin = baseUrl.replace(/^https:/i, 'wss:').replace(/\/+$/, '');
  return `${wsOrigin}/ws/driver?token=${encodeURIComponent(accessToken)}`;
}

/** The same URL with the credential removed, for logs (R15.1). */
function redactUrl(url: string): string {
  const queryStart = url.indexOf('?');
  return queryStart === -1 ? url : `${url.slice(0, queryStart)}?token=<redacted>`;
}

// ---------------------------------------------------------------------------
// The reconnect ladder (R14.5)
// ---------------------------------------------------------------------------

let random: () => number = Math.random;
let clock: () => number = () => Date.now();

/**
 * Delay before reconnect attempt `attempt` (0-based): `min(1s · 2^attempt,
 * 30s)` with ±20 % jitter, clamped to `[1s, 30s]` so R14.5's floor and ceiling
 * both hold literally after jittering.
 *
 * This replaces the donor's dead `calculateReconnectDelay()` and its
 * hard-coded 100/200/500 ms retries.
 */
export function reconnectDelayMs(attempt: number): number {
  const exponent = Math.max(0, Math.min(attempt, 30));
  const base = Math.min(RECONNECT_BASE_MS * 2 ** exponent, RECONNECT_CAP_MS);
  const jittered = base * (1 - RECONNECT_JITTER + 2 * RECONNECT_JITTER * random());
  return Math.min(RECONNECT_CAP_MS, Math.max(RECONNECT_BASE_MS, Math.round(jittered)));
}

// ---------------------------------------------------------------------------
// Identifier extraction — the only thing read out of a payload
// ---------------------------------------------------------------------------

function stringField(data: Record<string, unknown> | undefined, ...names: string[]): string | null {
  if (!data) {
    return null;
  }
  for (const name of names) {
    const value = data[name];
    if (typeof value === 'string' && value.length > 0) {
      return value;
    }
  }
  return null;
}

function orderIdOf(message: WebSocketMessage): string | null {
  return stringField(message.data, 'order_id', 'orderId');
}

function threadRefOf(message: WebSocketMessage): string | null {
  return stringField(message.data, 'work_ref', 'workRef', 'order_id', 'orderId');
}

// ---------------------------------------------------------------------------
// Injection seams
// ---------------------------------------------------------------------------

export interface NetworkMonitor {
  subscribe(listener: (online: boolean) => void): () => void;
}

export interface AppStateMonitor {
  subscribe(listener: (status: AppStateStatus) => void): () => void;
}

const netInfoMonitor: NetworkMonitor = {
  subscribe(listener) {
    const unsubscribe = NetInfo.addEventListener((state) => {
      listener(state.isConnected ?? false);
    });
    NetInfo.fetch()
      .then((state) => listener(state.isConnected ?? false))
      .catch(() => undefined);
    return unsubscribe;
  },
};

const appStateMonitor: AppStateMonitor = {
  subscribe(listener) {
    const subscription = AppState.addEventListener('change', listener);
    return () => subscription.remove();
  },
};

let socketFactory: WebSocketFactory = defaultSocketFactory;
let tokenProvider: () => Promise<string | null> = () =>
  apiClientSessionBridge().getAccessToken();
let sessionWatcher: (listener: () => void) => () => void = (listener) =>
  subscribeToSession(() => listener());
let network: NetworkMonitor = netInfoMonitor;
let appState: AppStateMonitor = appStateMonitor;
let baseUrlOverride: string | null = null;

/**
 * Override the socket factory, the token source, the monitors, the clock, and
 * the jitter source. Tests only.
 */
export function configureDriverWebSocket(next: {
  socketFactory?: WebSocketFactory | null;
  tokenProvider?: (() => Promise<string | null>) | null;
  sessionWatcher?: ((listener: () => void) => () => void) | null;
  network?: NetworkMonitor | null;
  appState?: AppStateMonitor | null;
  baseUrl?: string | null;
  now?: (() => number) | null;
  random?: (() => number) | null;
}): void {
  if (next.socketFactory !== undefined) {
    socketFactory = next.socketFactory ?? defaultSocketFactory;
  }
  if (next.tokenProvider !== undefined) {
    tokenProvider =
      next.tokenProvider ?? (() => apiClientSessionBridge().getAccessToken());
  }
  if (next.sessionWatcher !== undefined) {
    sessionWatcher =
      next.sessionWatcher ?? ((listener) => subscribeToSession(() => listener()));
  }
  if (next.network !== undefined) {
    network = next.network ?? netInfoMonitor;
  }
  if (next.appState !== undefined) {
    appState = next.appState ?? appStateMonitor;
  }
  if (next.baseUrl !== undefined) {
    baseUrlOverride = next.baseUrl;
  }
  if (next.now !== undefined) {
    clock = next.now ?? (() => Date.now());
  }
  if (next.random !== undefined) {
    random = next.random ?? Math.random;
  }
}

/** Restore module defaults. Tests only. */
export function resetDriverWebSocketConfig(): void {
  socketFactory = defaultSocketFactory;
  tokenProvider = () => apiClientSessionBridge().getAccessToken();
  sessionWatcher = (listener) => subscribeToSession(() => listener());
  network = netInfoMonitor;
  appState = appStateMonitor;
  baseUrlOverride = null;
  clock = () => Date.now();
  random = Math.random;
}

// ---------------------------------------------------------------------------
// Logging — redacted by construction
// ---------------------------------------------------------------------------

/**
 * The only log statement in this module. It carries a short reason string and
 * never a token, never a URL query string, and never an event payload
 * (R15.1, R15.2). No-op outside a development build.
 */
function log(reason: string): void {
  if (!__DEV__) {
    return;
  }
  // eslint-disable-next-line no-console
  console.log(`[ws] ${reason}`);
}

// ---------------------------------------------------------------------------
// The service
// ---------------------------------------------------------------------------

class DriverWebSocketService {
  private socket: WebSocketLike | null = null;
  private queryClient: QueryClient | null = null;

  private state: ConnectionState = 'idle';
  private attempts = 0;
  private online = true;
  private started = false;
  private connecting = false;
  /** A 4001 buys exactly one credential re-read before the channel gives up. */
  private reauthAttempted = false;

  private lastConnected = 0;
  private lastAck = 0;

  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private handshakeTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private stalenessTimer: ReturnType<typeof setInterval> | null = null;

  private teardownNetwork: (() => void) | null = null;
  private teardownAppState: (() => void) | null = null;
  private teardownSession: (() => void) | null = null;

  private messageListeners = new Set<MessageListener>();
  private statusListeners = new Set<(status: ConnectionStatus) => void>();

  // -------------------------------------------------------------------------
  // Public surface
  // -------------------------------------------------------------------------

  /** Install the cache every event invalidates. Call before `initialize()`. */
  setQueryClient(queryClient: QueryClient | null): void {
    this.queryClient = queryClient;
  }

  /**
   * Subscribe to raw frames — used for the `escalation` in-app alert. A listener
   * must treat the payload as a notification, never as entity state (R14.9).
   */
  onMessage(listener: MessageListener): () => void {
    this.messageListeners.add(listener);
    return () => {
      this.messageListeners.delete(listener);
    };
  }

  /** Subscribe to connection-state changes, for the offline indicator. */
  onStatusChange(listener: (status: ConnectionStatus) => void): () => void {
    this.statusListeners.add(listener);
    return () => {
      this.statusListeners.delete(listener);
    };
  }

  isConnected(): boolean {
    return this.socket?.readyState === OPEN;
  }

  getConnectionStatus(): ConnectionStatus {
    return {
      state: this.state,
      connected: this.isConnected(),
      attempts: this.attempts,
      maxAttempts: MAX_RECONNECT_ATTEMPTS,
      networkAvailable: this.online,
      lastConnected: this.lastConnected,
      lastAck: this.lastAck,
    };
  }

  /**
   * Install the network, application-state, and session subscriptions, then
   * open the channel. Idempotent.
   *
   * The donor subscribed in its constructor, which made importing the module
   * subscribe to NetInfo; here the subscriptions belong to the lifecycle.
   */
  async initialize(): Promise<void> {
    if (this.started) {
      return;
    }
    this.started = true;

    this.teardownNetwork = network.subscribe((online) => this.handleNetworkChange(online));
    this.teardownAppState = appState.subscribe((status) => this.handleAppStateChange(status));
    // A refresh replaces the access token, and the server validated the old one
    // at handshake time only — so the channel is rebuilt on the new credential.
    this.teardownSession = sessionWatcher(() => this.handleSessionChange());

    await this.connect();
  }

  /** Close the channel, drop every subscription, and stop reconnecting. */
  disconnect(): void {
    this.started = false;
    this.clearReconnectTimer();
    this.closeSocket(CLOSE_NORMAL, 'client disconnect');

    this.teardownNetwork?.();
    this.teardownAppState?.();
    this.teardownSession?.();
    this.teardownNetwork = null;
    this.teardownAppState = null;
    this.teardownSession = null;

    this.attempts = 0;
    this.reauthAttempted = false;
    this.setState('idle');
  }

  /** Reset the ladder and reconnect now. */
  async forceReconnect(): Promise<void> {
    this.attempts = 0;
    this.reauthAttempted = false;
    this.clearReconnectTimer();
    this.closeSocket(CLOSE_NORMAL, 'force reconnect');
    if (this.started) {
      await this.connect();
    }
  }

  /**
   * The only two frames this client ever sends are `heartbeat` and
   * `location_update` — the accepted inbound vocabulary at
   * `driver/ws/driver_ws_manager.py:37-42`. There is no `ack`, no
   * `status_update`, and no `exception` sender anywhere in this module: those
   * are REST operations (R14.11).
   */
  sendLocationUpdate(location: Record<string, unknown>): boolean {
    return this.send({ type: 'location_update', location });
  }

  // -------------------------------------------------------------------------
  // Connection lifecycle
  // -------------------------------------------------------------------------

  private async connect(): Promise<void> {
    if (!this.started || this.connecting || this.isConnected()) {
      return;
    }
    if (!this.online) {
      this.setState('waiting');
      return;
    }

    this.clearReconnectTimer();
    this.connecting = true;
    this.setState('connecting');

    let token: string | null = null;
    try {
      token = await tokenProvider();
    } catch {
      token = null;
    }

    if (!this.started) {
      this.connecting = false;
      return;
    }
    if (!token) {
      // No credential: reconnecting cannot help. The session module publishes a
      // change when one arrives, which restarts this path.
      this.connecting = false;
      this.setState('unauthorized');
      log('no session credential, channel idle');
      return;
    }

    let url: string;
    try {
      url = buildDriverWsUrl(token, baseUrlOverride ?? undefined);
    } catch {
      this.connecting = false;
      log('backend origin unusable, retrying on the ladder');
      this.scheduleReconnect();
      return;
    }

    try {
      log(`connecting ${redactUrl(url)}`);
      const socket = socketFactory(url, undefined, {
        // Supplied as well as the query parameter: `_extract_session_credential`
        // prefers the header where React Native honours it, and ignores it where
        // it does not (R14.1).
        headers: { Authorization: `Bearer ${token}` },
      });
      this.socket = socket;

      socket.onopen = () => this.handleOpen();
      socket.onmessage = (event) => this.handleFrame(event.data);
      socket.onerror = () => this.handleError();
      socket.onclose = (event) => this.handleClose(event.code);

      this.handshakeTimer = setTimeout(() => {
        this.handshakeTimer = null;
        if (this.socket === socket && socket.readyState === CONNECTING) {
          log('handshake timeout');
          this.recycle('handshake timeout');
        }
      }, CONNECTION_TIMEOUT_MS);
    } catch {
      this.connecting = false;
      this.socket = null;
      log('socket construction failed');
      this.scheduleReconnect();
      return;
    }

    this.connecting = false;
  }

  private handleOpen(): void {
    this.clearHandshakeTimer();
    this.connecting = false;
    this.attempts = 0;
    this.reauthAttempted = false;
    this.lastConnected = clock();
    this.lastAck = clock();
    this.setState('open');
    log('open');

    this.startHeartbeat();

    // Events that fired while this client was disconnected were not buffered
    // server-side, so the assigned-work list is assumed stale on every open.
    this.invalidate([WORK_SCOPE]);
  }

  private handleError(): void {
    this.clearHandshakeTimer();
    this.connecting = false;
    log('socket error');
    // `onclose` follows `onerror` on every platform this app targets; the
    // reconnect is scheduled there so a single failure costs one attempt.
  }

  private handleClose(code: number): void {
    this.clearHandshakeTimer();
    this.stopHeartbeat();
    this.connecting = false;
    this.socket = null;

    if (!this.started) {
      this.setState('idle');
      return;
    }

    log(`closed code=${code}`);

    if (code === CLOSE_UNAUTHORIZED) {
      // The credential was rejected. Re-read it once — a refresh may have
      // landed between building the URL and the handshake — then stop, rather
      // than spending 25 attempts on a token the server will not accept.
      if (this.reauthAttempted) {
        this.setState('unauthorized');
        return;
      }
      this.reauthAttempted = true;
      this.attempts = 0;
      this.scheduleReconnect();
      return;
    }

    // Every other code, `4002` heartbeat timeout and a server-initiated `1000`
    // alike, runs the ladder. The donor skipped `1000` entirely, which left a
    // driver silently disconnected across a backend restart.
    this.scheduleReconnect();
  }

  /** Close the current socket without treating it as an intentional teardown. */
  private recycle(reason: string): void {
    this.closeSocket(CLOSE_NORMAL, reason);
    this.scheduleReconnect();
  }

  /** Detach the handlers first, so this close does not re-enter `handleClose`. */
  private closeSocket(code: number, reason: string): void {
    this.clearHandshakeTimer();
    this.stopHeartbeat();
    this.connecting = false;

    const socket = this.socket;
    this.socket = null;
    if (!socket) {
      return;
    }
    socket.onopen = null;
    socket.onmessage = null;
    socket.onerror = null;
    socket.onclose = null;
    try {
      socket.close(code, reason);
    } catch {
      // A socket that refuses to close is already unusable.
    }
  }

  private scheduleReconnect(): void {
    this.clearReconnectTimer();

    if (!this.started) {
      return;
    }
    if (!this.online) {
      // The offline → online transition reconnects immediately (R14.6); burning
      // attempts against a down radio would only exhaust the ladder.
      this.setState('waiting');
      return;
    }
    if (this.attempts >= MAX_RECONNECT_ATTEMPTS) {
      this.setState('exhausted');
      log(`giving up after ${MAX_RECONNECT_ATTEMPTS} attempts`);
      return;
    }

    const delay = reconnectDelayMs(this.attempts);
    this.attempts += 1;
    this.setState('waiting');
    log(`reconnect attempt ${this.attempts} in ${delay}ms`);

    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.connect();
    }, delay);
  }

  // -------------------------------------------------------------------------
  // Heartbeat and staleness
  // -------------------------------------------------------------------------

  private startHeartbeat(): void {
    this.stopHeartbeat();

    // 30 s against the server's 120 s timeout (R14.4).
    this.heartbeatTimer = setInterval(() => {
      if (!this.send({ type: 'heartbeat' })) {
        this.recycle('heartbeat send failed');
      }
    }, HEARTBEAT_INTERVAL_MS);

    // The donor's 90 s client staleness detector, retained: three missed acks
    // and the channel is rebuilt rather than trusted.
    this.stalenessTimer = setInterval(() => {
      if (clock() - this.lastAck > STALENESS_THRESHOLD_MS) {
        log('stale channel, no acknowledgement in 90s');
        this.recycle('stale channel');
      }
    }, STALENESS_CHECK_INTERVAL_MS);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    if (this.stalenessTimer) {
      clearInterval(this.stalenessTimer);
      this.stalenessTimer = null;
    }
  }

  private send(frame: Record<string, unknown>): boolean {
    const socket = this.socket;
    if (!socket || socket.readyState !== OPEN) {
      return false;
    }
    try {
      socket.send(JSON.stringify({ ...frame, timestamp: new Date(clock()).toISOString() }));
      return true;
    } catch {
      return false;
    }
  }

  // -------------------------------------------------------------------------
  // Inbound frames
  // -------------------------------------------------------------------------

  private handleFrame(raw: string): void {
    let message: WebSocketMessage;
    try {
      message = JSON.parse(raw) as WebSocketMessage;
    } catch {
      log('unparseable frame discarded');
      return;
    }

    const type = message.type;

    if (type === 'heartbeat_ack' || type === 'pong') {
      this.lastAck = clock();
      return;
    }

    if (type === 'error') {
      // The server rejects an unsupported inbound type with an error frame
      // (R14.10). This client sends only `heartbeat` and `location_update`, so
      // an error frame here means a contract drift worth surfacing, not a
      // state change to apply.
      log('server rejected an outbound frame');
      return;
    }

    if (!type || !DRIVER_EVENT_TYPES.includes(type as DriverEventType)) {
      log(`unrecognised event type=${type ?? 'none'}`);
      return;
    }

    log(`event ${type}`);
    this.emit(message);
    this.invalidateFor(type as DriverEventType, message);
  }

  /**
   * Every event is a cache-invalidation signal and nothing else (R14.9). No
   * branch here writes an entity into the cache, so order detail is always
   * fetched over an authenticated request before it is displayed.
   */
  private invalidateFor(type: DriverEventType, message: WebSocketMessage): void {
    const orderId = orderIdOf(message);

    switch (type) {
      case 'assignment':
      case 'assignment_revoked':
        // `['work']` reaches the list and, being the shared prefix, every
        // order detail; the explicit order key is kept so the intent of R14.8
        // is readable at the call site.
        this.invalidate([WORK_SCOPE]);
        if (orderId) {
          this.invalidate(queryKeys.order(orderId));
        }
        break;

      case 'new_route':
        this.invalidate([WORK_SCOPE]);
        if (orderId) {
          this.invalidate(queryKeys.order(orderId));
        }
        break;

      case 'escalation':
        if (orderId) {
          this.invalidate(queryKeys.order(orderId));
        } else {
          this.invalidate([WORK_SCOPE]);
        }
        break;

      case 'message': {
        const workRef = threadRefOf(message);
        if (workRef) {
          this.invalidate(queryKeys.messages(workRef));
        }
        break;
      }
    }
  }

  private invalidate(queryKey: readonly unknown[]): void {
    void this.queryClient?.invalidateQueries({ queryKey });
  }

  private emit(message: WebSocketMessage): void {
    this.messageListeners.forEach((listener) => {
      try {
        listener(message);
      } catch {
        // A listener's failure is its own; it never stops the invalidation.
        log('a message listener threw');
      }
    });
  }

  // -------------------------------------------------------------------------
  // Environment transitions
  // -------------------------------------------------------------------------

  /** R14.6 — offline → online resets the attempt counter and reconnects now. */
  private handleNetworkChange(online: boolean): void {
    const was = this.online;
    this.online = online;

    if (!this.started || was === online) {
      return;
    }

    if (online) {
      log('network restored');
      this.attempts = 0;
      this.reauthAttempted = false;
      this.clearReconnectTimer();
      this.closeSocket(CLOSE_NORMAL, 'network restored');
      void this.connect();
    } else {
      log('network lost');
      this.clearReconnectTimer();
      this.closeSocket(CLOSE_NORMAL, 'network lost');
      this.setState('waiting');
    }
  }

  /** R14.7 — verify on foreground, reconnect when the channel is not open. */
  private handleAppStateChange(status: AppStateStatus): void {
    if (!this.started || status !== 'active') {
      return;
    }
    if (this.isConnected()) {
      return;
    }
    log('foregrounded with the channel closed');
    this.attempts = 0;
    this.reauthAttempted = false;
    this.clearReconnectTimer();
    this.closeSocket(CLOSE_NORMAL, 'foreground reconnect');
    void this.connect();
  }

  /** A new credential, or none: tear down and rebuild on what is current. */
  private handleSessionChange(): void {
    if (!this.started) {
      return;
    }
    log('session changed, rebuilding the channel');
    this.attempts = 0;
    this.reauthAttempted = false;
    this.clearReconnectTimer();
    this.closeSocket(CLOSE_NORMAL, 'session changed');
    void this.connect();
  }

  // -------------------------------------------------------------------------
  // Bookkeeping
  // -------------------------------------------------------------------------

  private setState(state: ConnectionState): void {
    if (this.state === state) {
      return;
    }
    this.state = state;
    const status = this.getConnectionStatus();
    this.statusListeners.forEach((listener) => {
      try {
        listener(status);
      } catch {
        log('a status listener threw');
      }
    });
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private clearHandshakeTimer(): void {
    if (this.handshakeTimer) {
      clearTimeout(this.handshakeTimer);
      this.handshakeTimer = null;
    }
  }
}

/** The one driver realtime channel. */
export const driverWebSocket = new DriverWebSocketService();
