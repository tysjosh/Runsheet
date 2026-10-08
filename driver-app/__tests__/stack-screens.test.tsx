/**
 * Stack and tab screens after the UI revamp (tasks 4.2–4.4):
 *
 * - one title per screen (the stack header's; `Stack.Screen` is stubbed, so any
 *   copy of the title found in the tree is an in-content duplicate),
 * - every single-choice option is a radio carrying `checked`,
 * - the cross-contamination warning is an exception badge, not red text alone,
 * - POD gallons start at the planned gallons, whole, editable, decimal-pad,
 * - Route stop chips are radios and compartments show product caps,
 * - Profile no longer carries duty status or Hours of Service (D16).
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react-native';

import RouteScreen from '../app/(tabs)/route';
import ProfileScreen from '../app/(tabs)/profile';
import NewInspectionScreen from '../app/inspection/new';
import DeliveryDetailScreen from '../app/order/[orderId]';
import ExceptionReportScreen from '../app/order/[orderId]/exception';
import ProofOfDeliveryScreen from '../app/order/[orderId]/pod';
import { configureFormat } from '../lib/format';
import { LATER_ORDER, NEXT_ORDER, renderWithQuery } from './helpers/screen-harness';

jest.mock('@react-native-community/netinfo', () =>
  require('@react-native-community/netinfo/jest/netinfo-mock.js'),
);
jest.mock('react-native-safe-area-context', () => require('react-native-safe-area-context/jest/mock').default);

const mockParams: { current: Record<string, string> } = { current: {} };
jest.mock('expo-router', () => ({
  useRouter: () => ({ push: jest.fn(), replace: jest.fn(), back: jest.fn() }),
  useLocalSearchParams: () => mockParams.current,
  Stack: { Screen: () => null },
}));
jest.mock('expo-camera', () => ({
  CameraView: () => null,
  useCameraPermissions: () => [{ granted: true }, jest.fn()],
}));
jest.mock('../components/SignaturePad', () => ({ SignaturePad: () => null }));
jest.mock('../lib/work-api', () => ({
  loadAssignedWork: jest.fn(),
  loadWorkDetail: jest.fn(),
  queueOrderStatus: jest.fn(),
}));
jest.mock('../lib/offline-queue', () => ({
  queueDepth: jest.fn(async () => ({ pending: 0, inFlight: 0, failed: 0, conflict: 0, outstanding: 0 })),
  subscribeToQueue: jest.fn(() => () => undefined),
  drainQueue: jest.fn(async () => undefined),
  enqueueMutation: jest.fn(async () => ({ inserted: true })),
  generateIdempotencyKey: jest.fn(() => 'key'),
}));
jest.mock('../lib/pod-api', () => ({
  queuePodCapture: jest.fn(),
  uploadPodArtifact: jest.fn(),
  syncPendingPodCaptures: jest.fn(async () => undefined),
  REFUSAL_REASONS: [
    { value: 'customer_refused', label: 'Customer refused the delivery' },
    { value: 'other', label: 'Other' },
  ],
}));
jest.mock('../lib/geotag', () => ({
  requestGeotag: jest.fn(async () => ({ fix: null, permission: 'granted' })),
  locationPermissionDecision: jest.fn(async () => 'granted'),
}));
jest.mock('../lib/session', () => ({
  currentSessionIdentity: () => ({ driverId: 'drv-1' }),
  signOut: jest.fn(),
}));
jest.mock('../lib/location-tracker', () => ({ locationTracker: { shutdown: jest.fn() } }));
jest.mock('../lib/websocket', () => ({ driverWebSocket: { disconnect: jest.fn() } }));
jest.mock('../lib/notification-manager', () => {
  // useSyncExternalStore needs a stable snapshot.
  const snapshot = { alertsDisabled: false, permission: 'granted' };
  return {
    notificationManager: { subscribe: () => () => undefined, getSnapshot: () => snapshot },
  };
});
jest.mock('../lib/duty-api', () => ({
  ...jest.requireActual('../lib/duty-api'),
  loadDriverIdentity: jest.fn(async () => ({ driver_id: 'drv-1', driver_name: 'Jordan Ellis', duty_status: 'active' })),
}));
jest.mock('../lib/qualification-api', () => ({
  ...jest.requireActual('../lib/qualification-api'),
  loadDriverQualifications: jest.fn(() => new Promise(() => undefined)),
}));

const workApi = jest.requireMock('../lib/work-api') as {
  loadAssignedWork: jest.Mock;
  loadWorkDetail: jest.Mock;
};

function radios() {
  return screen.getAllByRole('radio');
}

function expectEveryRadioHasChecked() {
  const all = radios();
  expect(all.length).toBeGreaterThan(0);
  for (const radio of all) {
    expect(typeof radio.props.accessibilityState?.checked).toBe('boolean');
  }
  return all;
}

beforeEach(() => {
  configureFormat({ timeZone: 'UTC', locale: 'en-US' });
  mockParams.current = { orderId: NEXT_ORDER.order_id };
  workApi.loadAssignedWork.mockResolvedValue({
    data: [NEXT_ORDER, LATER_ORDER],
    pagination: { page: 1, size: 50, total: 2, total_pages: 1 },
  });
  workApi.loadWorkDetail.mockImplementation(async (id: string) => (id === NEXT_ORDER.order_id ? NEXT_ORDER : LATER_ORDER));
});

describe('Delivery detail', () => {
  it('names the customer only in the header and shows the warning as an exception badge', async () => {
    renderWithQuery(<DeliveryDetailScreen />);
    await screen.findByText(NEXT_ORDER.destination.address);
    expect(screen.queryByText(NEXT_ORDER.customer_name)).toBeNull();
    const warning = screen.getByTestId('contamination-C-2');
    expect(within(warning).getByLabelText('Cross-contamination')).toBeTruthy();
    expect(within(warning).getByText(/last held Regular unleaded and is now loaded with Diesel #2/)).toBeTruthy();
    expect(screen.getByLabelText(`Navigate to ${NEXT_ORDER.destination.address}`)).toBeTruthy();
    expect(screen.getByLabelText(`Call ${NEXT_ORDER.customer_name}`)).toBeTruthy();
    // The phone number is no longer plain text.
    expect(screen.queryByText(NEXT_ORDER.customer_phone as string)).toBeNull();
    expect(screen.getByText('Thu 30 Jul · 08:30–10:30')).toBeTruthy();
  });
});

describe('POD', () => {
  it('has no in-content title and prefills whole planned gallons on a decimal pad', async () => {
    renderWithQuery(<ProofOfDeliveryScreen />);
    const field = await screen.findByTestId('pod-gallons');
    await waitFor(() => expect(field.props.value).toBe('4200'));
    expect(field.props.keyboardType).toBe('decimal-pad');
    fireEvent.changeText(field, '4150');
    expect(screen.getByTestId('pod-gallons').props.value).toBe('4150');
    expect(screen.queryByText('Proof of delivery')).toBeNull();
    // Photo and ticket capture sit side by side.
    expect(screen.getByTestId('pod-capture-photo')).toBeTruthy();
    expect(screen.getByTestId('pod-capture-ticket')).toBeTruthy();
  });

  it('refusal reasons are radios with checked state', async () => {
    renderWithQuery(<ProofOfDeliveryScreen />);
    fireEvent.press(await screen.findByText('The delivery was refused'));
    const [first] = expectEveryRadioHasChecked();
    fireEvent.press(first);
    expect(radios()[0].props.accessibilityState.checked).toBe(true);
  });
});

describe('Exception', () => {
  it('has one title and checked-state radios for type and severity', () => {
    renderWithQuery(<ExceptionReportScreen />);
    expect(screen.queryByText('Report a problem')).toBeNull();
    const all = expectEveryRadioHasChecked();
    expect(all.filter((r) => r.props.accessibilityState.checked)).toHaveLength(2);
  });
});

describe('Inspection', () => {
  it('has one title, a prefilled unit and checked-state radios', () => {
    mockParams.current = { inspectionType: 'pre_trip', assetId: 'Tanker 24' };
    renderWithQuery(<NewInspectionScreen />);
    expect(screen.queryByText('Vehicle inspection')).toBeNull();
    expect(screen.getByLabelText('Vehicle unit number').props.value).toBe('Tanker 24');
    const all = expectEveryRadioHasChecked();
    expect(all.find((r) => r.props.accessibilityState.checked)).toBeTruthy();
  });
});

describe('Route', () => {
  it('stop chips are radios and compartments carry product caps', async () => {
    renderWithQuery(<RouteScreen />);
    await waitFor(() => expect(screen.getAllByTestId('route-stop-chip')).toHaveLength(2));
    const chips = screen.getAllByTestId('route-stop-chip');
    expect(chips.every((c) => c.props.accessibilityRole === 'radio')).toBe(true);
    expect(chips.filter((c) => c.props.accessibilityState.checked)).toHaveLength(1);
    await screen.findByText('Compartment C-1');
    expect(screen.getAllByText(/^Diesel #2 \(on-road\)/).length).toBeGreaterThan(0);
    expect(screen.queryByText('Active route')).toBeNull();
  });

  it('Arrive from Work opens the check-in for the requested stop, prefilled', async () => {
    mockParams.current = { orderId: NEXT_ORDER.order_id, checkin: '0' };
    renderWithQuery(<RouteScreen />);
    expect(await screen.findByText('Check in · 1. TANK-MGC-01')).toBeTruthy();
    expect(screen.getByLabelText('Diesel #2 (on-road) (gal)').props.value).toBe('4200');
  });
});

describe('Profile', () => {
  it('no longer carries duty status or Hours of Service', async () => {
    renderWithQuery(<ProfileScreen />);
    await screen.findByText('Jordan Ellis');
    expect(screen.queryByText('Duty status')).toBeNull();
    expect(screen.queryByText('Hours of Service')).toBeNull();
    expect(screen.queryByRole('radio')).toBeNull();
    expect(screen.queryByText('Driver profile')).toBeNull();
  });
});
