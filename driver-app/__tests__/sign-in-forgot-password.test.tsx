/**
 * The sign-in screen's forgot-password affordance.
 *
 * The link is the driver's only self-service recovery path: without it the
 * driver has to phone dispatch to have an admin mint a reset link. It links out
 * to the web app, whose origin the screen fetches from the backend's
 * unauthenticated `GET /api/auth/public-config` — the backend holds the
 * authoritative value, since it is the origin SuperTokens mints reset links
 * against.
 *
 * Because the origin now arrives over the network, the cases that matter are:
 * the link must be **hidden until the fetch resolves** (never a flash of a link
 * that then vanishes), hidden when the answer is unusable or the call fails, and
 * a failing call must not touch sign-in.
 *
 * These drive the real `lib/web-app` and `lib/api-client` modules through an
 * injected `fetchImpl`, so what is under test is the actual fetch-and-normalize
 * path rather than a stubbed accessor.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import { openURL } from 'expo-linking';

import SignInScreen from '../app/sign-in';
import { configureApiClient, resetApiClient } from '../lib/api-client';
import { resetWebAppConfig } from '../lib/web-app';
import { signIn } from '../lib/session';

jest.mock('expo-linking', () => ({ openURL: jest.fn() }));

let mockDemoPreviewEnabled = false;

jest.mock('../lib/demo-preview', () => ({
  // A getter, so each render reads the current value rather than the value at
  // module-init time.
  get demoPreviewEnabled() {
    return mockDemoPreviewEnabled;
  },
  installDemoPreview: jest.fn(),
}));

// The screen imports these for the sign-in path only; the real modules open a
// socket and a notification channel.
jest.mock('../lib/session', () => ({ signIn: jest.fn() }));
jest.mock('../lib/notification-manager', () => ({
  notificationManager: { retry: jest.fn() },
}));
jest.mock('../lib/websocket', () => ({
  driverWebSocket: { initialize: jest.fn() },
}));

const openURLMock = openURL as jest.MockedFunction<typeof openURL>;
const signInMock = signIn as jest.MockedFunction<typeof signIn>;

const API_ORIGIN = 'https://api.runsheet.example.com';
const WEB_ORIGIN = 'https://app.runsheet.example.com';
const CONFIG_URL = `${API_ORIGIN}/api/auth/public-config`;
const LINK_LABEL = 'Forgot your password?';

const fetchMock = jest.fn();

/** A minimal `Response` of the shape `lib/api-client` reads. */
function jsonResponse(body: unknown, status = 200) {
  return {
    status,
    ok: status >= 200 && status < 300,
    text: async () => JSON.stringify(body),
    headers: { get: () => null },
  };
}

/** The backend answers the config call with this body. */
function backendReports(websiteDomain: string | null): void {
  fetchMock.mockResolvedValue(jsonResponse({ website_domain: websiteDomain }));
}

beforeEach(() => {
  resetWebAppConfig();
  configureApiClient({
    baseUrl: API_ORIGIN,
    fetchImpl: fetchMock as unknown as typeof fetch,
  });
});

afterEach(() => {
  resetWebAppConfig();
  resetApiClient();
  mockDemoPreviewEnabled = false;
  jest.clearAllMocks();
});

describe('sign-in forgot-password link', () => {
  it('opens the web app reset page in the system browser once the backend reports the origin', async () => {
    backendReports(WEB_ORIGIN);
    openURLMock.mockResolvedValue(true);

    render(<SignInScreen />);
    fireEvent.press(await screen.findByText(LINK_LABEL));

    expect(fetchMock).toHaveBeenCalledWith(CONFIG_URL, expect.anything());
    expect(openURLMock).toHaveBeenCalledWith(
      `${WEB_ORIGIN}/auth/forgot-password`,
    );
  });

  it('renders nothing until the origin has been fetched', async () => {
    // A call that never settles: the screen is in the state it holds between
    // mount and the config response arriving.
    fetchMock.mockReturnValue(new Promise(() => {}));

    render(<SignInScreen />);

    // Nothing to await — the assertion is precisely that the link is absent
    // while the origin is unknown, so revealing it optimistically fails here.
    expect(screen.queryByText(LINK_LABEL)).toBeNull();
    // Still absent after the microtask queue drains, since nothing resolved.
    await Promise.resolve();
    expect(screen.queryByText(LINK_LABEL)).toBeNull();
  });

  it('renders nothing when the backend reports no web origin', async () => {
    backendReports(null);

    render(<SignInScreen />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByText(LINK_LABEL)).toBeNull();
  });

  it('renders nothing when the reported origin is not TLS', async () => {
    backendReports('http://localhost:3000');

    render(<SignInScreen />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByText(LINK_LABEL)).toBeNull();
  });

  it('renders nothing in demo preview, where there is no account to recover', async () => {
    backendReports(WEB_ORIGIN);
    mockDemoPreviewEnabled = true;

    render(<SignInScreen />);

    await Promise.resolve();
    expect(screen.queryByText(LINK_LABEL)).toBeNull();
    // Demo preview does not even ask: there is nothing to link to.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('keeps the link hidden and still signs in when the config call fails', async () => {
    fetchMock.mockRejectedValue(new Error('network is unreachable'));
    signInMock.mockResolvedValue(undefined as never);

    render(<SignInScreen />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByText(LINK_LABEL)).toBeNull();

    // The failed config call must not break the screen's actual job.
    fireEvent.changeText(screen.getByPlaceholderText('driver@company.com'), 'd@x.io');
    fireEvent.changeText(screen.getByPlaceholderText('Password'), 'correct-horse');
    fireEvent.press(screen.getByText('Sign in'));

    await waitFor(() =>
      expect(signInMock).toHaveBeenCalledWith({
        email: 'd@x.io',
        password: 'correct-horse',
      }),
    );
  });

  it('surfaces a message instead of throwing when the browser cannot be opened', async () => {
    backendReports(WEB_ORIGIN);
    openURLMock.mockRejectedValue(new Error('no activity found to handle intent'));

    render(<SignInScreen />);
    fireEvent.press(await screen.findByText(LINK_LABEL));

    expect(await screen.findByText(/could not be opened/)).toBeTruthy();
  });
});
