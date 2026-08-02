/**
 * The sign-in screen's forgot-password affordance.
 *
 * The link is the driver's only self-service recovery path: without it the
 * driver has to phone dispatch to have an admin mint a reset link. It links out
 * to the web app, whose origin comes from `EXPO_PUBLIC_WEB_BASE_URL`, so the
 * cases that matter are the two where the link must *not* appear — an unknown
 * web origin and demo preview — plus the open failing on a real device.
 */

import { fireEvent, render, screen } from '@testing-library/react-native';
import { openURL } from 'expo-linking';

import SignInScreen from '../app/sign-in';

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

// The screen imports these for the sign-in path only; none of them is exercised
// here, and the real modules open a socket and a notification channel.
jest.mock('../lib/session', () => ({ signIn: jest.fn() }));
jest.mock('../lib/notification-manager', () => ({
  notificationManager: { retry: jest.fn() },
}));
jest.mock('../lib/websocket', () => ({
  driverWebSocket: { initialize: jest.fn() },
}));

const openURLMock = openURL as jest.MockedFunction<typeof openURL>;

const WEB_ORIGIN = 'https://app.runsheet.example.com';
const LINK_LABEL = 'Forgot your password?';

const originalWebBaseUrl = process.env.EXPO_PUBLIC_WEB_BASE_URL;

function setWebOrigin(value: string | undefined): void {
  if (value === undefined) {
    delete process.env.EXPO_PUBLIC_WEB_BASE_URL;
  } else {
    process.env.EXPO_PUBLIC_WEB_BASE_URL = value;
  }
}

afterEach(() => {
  setWebOrigin(originalWebBaseUrl);
  mockDemoPreviewEnabled = false;
  jest.clearAllMocks();
});

describe('sign-in forgot-password link', () => {
  it('opens the web app reset page in the system browser when the origin is configured', () => {
    setWebOrigin(WEB_ORIGIN);
    openURLMock.mockResolvedValue(true);

    render(<SignInScreen />);
    fireEvent.press(screen.getByText(LINK_LABEL));

    expect(openURLMock).toHaveBeenCalledWith(
      `${WEB_ORIGIN}/auth/forgot-password`,
    );
  });

  it('renders nothing when the web origin is unset', () => {
    setWebOrigin(undefined);

    render(<SignInScreen />);

    expect(screen.queryByText(LINK_LABEL)).toBeNull();
  });

  it('renders nothing when the configured web origin is not TLS', () => {
    setWebOrigin('http://localhost:3000');

    render(<SignInScreen />);

    expect(screen.queryByText(LINK_LABEL)).toBeNull();
  });

  it('renders nothing in demo preview, where there is no account to recover', () => {
    setWebOrigin(WEB_ORIGIN);
    mockDemoPreviewEnabled = true;

    render(<SignInScreen />);

    expect(screen.queryByText(LINK_LABEL)).toBeNull();
  });

  it('surfaces a message instead of throwing when the browser cannot be opened', async () => {
    setWebOrigin(WEB_ORIGIN);
    openURLMock.mockRejectedValue(new Error('no activity found to handle intent'));

    render(<SignInScreen />);
    fireEvent.press(screen.getByText(LINK_LABEL));

    expect(await screen.findByText(/could not be opened/)).toBeTruthy();
  });
});
