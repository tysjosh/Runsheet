/**
 * The sign-in screen's message for a throttled sign-in (staging finding F5).
 *
 * `POST /auth/driver/session` answers 429 `RATE_LIMITED` with
 * `details.retry_after_seconds` after too many attempts. The screen must tell
 * the driver how long to wait; other failures keep showing the server message.
 */
import { fireEvent, render, screen } from '@testing-library/react-native';
import SignInScreen from '../app/sign-in';
import { ApiError } from '../lib/api-client';
import { signIn } from '../lib/session';

jest.mock('expo-linking', () => ({ openURL: jest.fn() }));
jest.mock('../lib/demo-preview', () => ({
  demoPreviewEnabled: false,
  installDemoPreview: jest.fn(),
}));
jest.mock('../lib/session', () => ({ signIn: jest.fn() }));
jest.mock('../lib/notification-manager', () => ({
  notificationManager: { retry: jest.fn() },
}));
jest.mock('../lib/websocket', () => ({
  driverWebSocket: { initialize: jest.fn() },
}));
jest.mock('../lib/web-app', () => ({
  forgotPasswordUrl: jest.fn().mockResolvedValue(null),
}));

const signInMock = signIn as jest.MockedFunction<typeof signIn>;

function submit() {
  fireEvent.changeText(screen.getByPlaceholderText('driver@company.com'), 'driver@x.test');
  fireEvent.changeText(screen.getByPlaceholderText('Password'), 'wrong-pass');
  fireEvent.press(screen.getByText('Sign in'));
}

beforeEach(() => {
  signInMock.mockReset();
});

describe('SignInScreen throttling', () => {
  it('shows the retry wait on a 429', async () => {
    signInMock.mockRejectedValue(
      new ApiError({
        status: 429,
        errorCode: 'RATE_LIMITED',
        message: 'server text',
        details: { retry_after_seconds: 42 },
      }),
    );
    render(<SignInScreen />);
    submit();
    expect(
      await screen.findByText('Too many attempts, try again in 42 seconds'),
    ).toBeTruthy();
  });

  it('still shows the server message for bad credentials', async () => {
    signInMock.mockRejectedValue(
      new ApiError({
        status: 401,
        errorCode: 'UNAUTHORIZED',
        message: 'Invalid credentials',
      }),
    );
    render(<SignInScreen />);
    submit();
    expect(await screen.findByText('Invalid credentials')).toBeTruthy();
  });
});
