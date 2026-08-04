/**
 * `lib/websocket.ts` — the authenticated driver realtime channel.
 *
 * **Validates: Requirements 14.1, 14.4, 14.5, 14.6, 14.7, 14.8, 14.9, 14.11, 16.4**
 */

import { QueryClient } from '@tanstack/react-query';
import type { AppStateStatus } from 'react-native';

import {
  HEARTBEAT_INTERVAL_MS,
  MAX_RECONNECT_ATTEMPTS,
  RECONNECT_BASE_MS,
  RECONNECT_CAP_MS,
  STALENESS_THRESHOLD_MS,
  buildDriverWsUrl,
  configureDriverWebSocket,
  driverWebSocket,
  reconnectDelayMs,
  resetDriverWebSocketConfig,
  type WebSocketLike,
  type WebSocketOptions,
} from '@/lib/websocket';

const BASE_URL = 'https://api.runsheet.example.com';

class FakeSocket implements WebSocketLike {
  readyState = 0;
  readonly sent: string[] = [];
  closedWith: { code?: number; reason?: string } | null = null;

  onopen: ((event?: unknown) => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: ((event?: unknown) => void) | null = null;
  onclose: ((event: { code: number; reason?: string }) => void) | null = null;

  send(data: string): void {
    if (this.readyState !== 1) {
      throw new Error('socket is not open');
    }
    this.sent.push(data);
  }

  close(code?: number, reason?: string): void {
    this.readyState = 3;
    this.closedWith = { code, reason };
  }

  /** Server accepted the handshake. */
  accept(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  /** Server pushed a frame. */
  push(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  /** Server closed the connection. */
  serverClose(code: number): void {
    this.readyState = 3;
    this.onclose?.({ code });
  }

  frames(): { type?: string }[] {
    return this.sent.map((raw) => JSON.parse(raw) as { type?: string });
  }
}

const sockets: FakeSocket[] = [];
const urls: string[] = [];
const optionsSeen: WebSocketOptions[] = [];

let emitNetwork: (online: boolean) => void = () => undefined;
let emitAppState: (status: AppStateStatus) => void = () => undefined;
let emitSessionChange: () => void = () => undefined;

let token: string | null = 'access-token-value';
let now = Date.parse('2026-07-29T12:00:00Z');
let invalidateSpy: jest.SpyInstance;

function latest(): FakeSocket {
  return sockets[sockets.length - 1];
}

function invalidatedKeys(): unknown[][] {
  return invalidateSpy.mock.calls.map((call) => (call[0] as { queryKey: unknown[] }).queryKey);
}

beforeEach(() => {
  jest.useFakeTimers();
  sockets.length = 0;
  urls.length = 0;
  optionsSeen.length = 0;
  token = 'access-token-value';
  now = Date.parse('2026-07-29T12:00:00Z');

  const queryClient = new QueryClient();
  invalidateSpy = jest.spyOn(queryClient, 'invalidateQueries').mockResolvedValue(undefined);
  driverWebSocket.setQueryClient(queryClient);

  configureDriverWebSocket({
    baseUrl: BASE_URL,
    tokenProvider: async () => token,
    now: () => now,
    // Deterministic ladder: the low end of the jitter band.
    random: () => 0,
    socketFactory: (url, _protocols, options) => {
      urls.push(url);
      optionsSeen.push(options);
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    },
    network: {
      subscribe: (listener) => {
        emitNetwork = listener;
        return () => {
          emitNetwork = () => undefined;
        };
      },
    },
    appState: {
      subscribe: (listener) => {
        emitAppState = listener;
        return () => {
          emitAppState = () => undefined;
        };
      },
    },
    sessionWatcher: (listener) => {
      emitSessionChange = listener;
      return () => {
        emitSessionChange = () => undefined;
      };
    },
  });
});

afterEach(() => {
  driverWebSocket.disconnect();
  driverWebSocket.setQueryClient(null);
  resetDriverWebSocketConfig();
  invalidateSpy.mockRestore();
  jest.useRealTimers();
});

async function connectAndOpen(): Promise<FakeSocket> {
  await driverWebSocket.initialize();
  latest().accept();
  return latest();
}

describe('connection URL (R14.1)', () => {
  it('presents the credential as the only query parameter over wss', () => {
    expect(buildDriverWsUrl('tok/en+1', BASE_URL)).toBe(
      'wss://api.runsheet.example.com/ws/driver?token=tok%2Fen%2B1',
    );
  });

  it('places no driver identifier and no user identifier in the query string', async () => {
    await connectAndOpen();

    const url = urls[0];
    expect(url.startsWith('wss://api.runsheet.example.com/ws/driver?token=')).toBe(true);
    expect(url).not.toMatch(/driver_?id/i);
    expect(url).not.toMatch(/user_?id/i);
    expect(url).not.toMatch(/rider/i);
  });

  it('supplies the same credential in the headers option', async () => {
    await connectAndOpen();

    expect(optionsSeen[0].headers).toEqual({ Authorization: 'Bearer access-token-value' });
  });

  it('does not connect at all without a credential', async () => {
    token = null;

    await driverWebSocket.initialize();

    expect(sockets).toHaveLength(0);
    expect(driverWebSocket.getConnectionStatus().state).toBe('unauthorized');
  });
});

describe('reconnect ladder (R14.5)', () => {
  it('starts at 1 second and doubles', () => {
    configureDriverWebSocket({ random: () => 0.5 });

    expect(reconnectDelayMs(0)).toBe(RECONNECT_BASE_MS);
    expect(reconnectDelayMs(1)).toBe(2_000);
    expect(reconnectDelayMs(3)).toBe(8_000);
  });

  it('never falls below 1 second or exceeds 30 seconds, at either jitter bound', () => {
    for (const r of [0, 0.5, 1]) {
      configureDriverWebSocket({ random: () => r });
      for (let attempt = 0; attempt <= 30; attempt += 1) {
        const delay = reconnectDelayMs(attempt);
        expect(delay).toBeGreaterThanOrEqual(RECONNECT_BASE_MS);
        expect(delay).toBeLessThanOrEqual(RECONNECT_CAP_MS);
      }
    }
    // The cap binds the un-jittered base, so the top of the band is exactly 30 s.
    configureDriverWebSocket({ random: () => 1 });
    expect(reconnectDelayMs(20)).toBe(RECONNECT_CAP_MS);
  });

  it('reconnects after an unexpected close and counts the attempt', async () => {
    await connectAndOpen();

    latest().serverClose(4002);

    expect(driverWebSocket.getConnectionStatus().attempts).toBe(1);
    await jest.advanceTimersByTimeAsync(RECONNECT_BASE_MS);
    expect(sockets).toHaveLength(2);
  });

  it('stops after 25 attempts', async () => {
    await connectAndOpen();

    for (let i = 0; i < MAX_RECONNECT_ATTEMPTS; i += 1) {
      latest().serverClose(4002);
      await jest.advanceTimersByTimeAsync(RECONNECT_CAP_MS);
    }
    latest().serverClose(4002);

    expect(driverWebSocket.getConnectionStatus().state).toBe('exhausted');
    expect(sockets).toHaveLength(MAX_RECONNECT_ATTEMPTS + 1);
  });

  it('gives a rejected credential one retry, then stops', async () => {
    await connectAndOpen();

    latest().serverClose(4001);
    await jest.advanceTimersByTimeAsync(RECONNECT_BASE_MS);
    expect(sockets).toHaveLength(2);

    latest().serverClose(4001);
    await jest.advanceTimersByTimeAsync(RECONNECT_CAP_MS);

    expect(sockets).toHaveLength(2);
    expect(driverWebSocket.getConnectionStatus().state).toBe('unauthorized');
  });
});

describe('heartbeat and staleness (R14.4, R16.4)', () => {
  it('sends a heartbeat every 30 seconds and nothing else', async () => {
    const socket = await connectAndOpen();

    await jest.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS * 3);

    const frames = socket.frames();
    expect(frames).toHaveLength(3);
    expect(frames.every((frame) => frame.type === 'heartbeat')).toBe(true);
  });

  it('rebuilds the channel after 90 seconds without an acknowledgement', async () => {
    const socket = await connectAndOpen();

    now += STALENESS_THRESHOLD_MS + 1;
    // One staleness tick is enough; the heartbeat interval has not elapsed yet.
    await jest.advanceTimersByTimeAsync(15_000);

    expect(socket.closedWith).not.toBeNull();
    await jest.advanceTimersByTimeAsync(RECONNECT_BASE_MS);
    expect(sockets).toHaveLength(2);
  });

  it('keeps the channel while acknowledgements arrive', async () => {
    const socket = await connectAndOpen();

    for (let i = 0; i < 6; i += 1) {
      now += HEARTBEAT_INTERVAL_MS;
      await jest.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
      socket.push({ type: 'heartbeat_ack' });
    }

    expect(socket.closedWith).toBeNull();
    expect(sockets).toHaveLength(1);
  });

  it('sends no ack, status_update, or exception frame (R14.11)', async () => {
    const socket = await connectAndOpen();

    socket.push({ type: 'assignment', data: { order_id: 'ord_1' } });
    socket.push({ type: 'escalation', data: { order_id: 'ord_1' } });
    await jest.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);

    const types = socket.frames().map((frame) => frame.type);
    expect(types).not.toContain('ack');
    expect(types).not.toContain('status_update');
    expect(types).not.toContain('exception');
  });
});

describe('events are invalidation signals only (R14.8, R14.9)', () => {
  it('invalidates the work list unconditionally on open', async () => {
    await connectAndOpen();

    expect(invalidatedKeys()).toEqual([['work']]);
  });

  it.each(['assignment', 'assignment_revoked'])(
    'invalidates the work list and the order detail on %s',
    async (type) => {
      const socket = await connectAndOpen();
      invalidateSpy.mockClear();

      socket.push({ type, data: { order_id: 'ord_42' } });

      expect(invalidatedKeys()).toEqual([['work'], ['work', 'ord_42']]);
    },
  );

  it('invalidates the order detail on escalation', async () => {
    const socket = await connectAndOpen();
    invalidateSpy.mockClear();

    socket.push({ type: 'escalation', data: { order_id: 'ord_7' } });

    expect(invalidatedKeys()).toEqual([['work', 'ord_7']]);
  });

  it('invalidates the thread on message', async () => {
    const socket = await connectAndOpen();
    invalidateSpy.mockClear();

    socket.push({ type: 'message', data: { work_ref: 'ord_9' } });

    expect(invalidatedKeys()).toEqual([['messages', 'ord_9']]);
  });

  it('invalidates the work list and the route on new_route', async () => {
    const socket = await connectAndOpen();
    invalidateSpy.mockClear();

    socket.push({ type: 'new_route', data: { order_id: 'ord_3' } });

    expect(invalidatedKeys()).toEqual([['work'], ['work', 'ord_3']]);
  });

  it('ignores an unrecognised event and an unparseable frame', async () => {
    const socket = await connectAndOpen();
    invalidateSpy.mockClear();

    socket.push({ type: 'wallet_credited', data: { amount: 1 } });
    socket.onmessage?.({ data: 'not json' });

    expect(invalidateSpy).not.toHaveBeenCalled();
  });
});

describe('environment transitions (R14.6, R14.7)', () => {
  it('resets the attempt counter and reconnects immediately when the network returns', async () => {
    await connectAndOpen();
    latest().serverClose(4002);
    await jest.advanceTimersByTimeAsync(RECONNECT_BASE_MS);
    latest().serverClose(4002);
    expect(driverWebSocket.getConnectionStatus().attempts).toBeGreaterThan(0);

    emitNetwork(false);
    expect(driverWebSocket.getConnectionStatus().state).toBe('waiting');

    const before = sockets.length;
    emitNetwork(true);
    await Promise.resolve();
    await Promise.resolve();

    expect(sockets.length).toBe(before + 1);
    expect(driverWebSocket.getConnectionStatus().attempts).toBe(0);
  });

  it('reconnects on foreground when the channel is closed', async () => {
    await connectAndOpen();
    latest().serverClose(4002);
    const before = sockets.length;

    emitAppState('active');
    await Promise.resolve();
    await Promise.resolve();

    expect(sockets.length).toBe(before + 1);
  });

  it('leaves an open channel alone on foreground', async () => {
    await connectAndOpen();

    emitAppState('active');
    await Promise.resolve();

    expect(sockets).toHaveLength(1);
  });

  it('tears down and reconnects on a token refresh', async () => {
    const socket = await connectAndOpen();
    token = 'rotated-token-value';

    emitSessionChange();
    await Promise.resolve();
    await Promise.resolve();

    expect(socket.closedWith).not.toBeNull();
    expect(sockets).toHaveLength(2);
    expect(urls[1]).toContain('token=rotated-token-value');
  });
});
