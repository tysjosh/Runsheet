/**
 * Work screen (UI revamp task 4.3, R13.3, R13.4, R13.8, D16).
 *
 * One large title, the duty chip moved from Profile (same mutation), the next
 * stop as a large card with RP 1637 product chips and Navigate / Call / Arrive,
 * remaining stops as compact rows, and the pre-trip prompt on going on duty.
 */

import { act, fireEvent, screen, waitFor, within } from '@testing-library/react-native';
import { Linking } from 'react-native';

import AssignedWorkScreen from '../app/(tabs)/index';
import { configureFormat } from '../lib/format';
import { forgetPretripMarker, markPretripQueued } from '../lib/pretrip-marker';
import { localCalendarDay } from '../lib/inspection-api';
import { LATER_ORDER, NEXT_ORDER, renderWithQuery } from './helpers/screen-harness';

jest.mock('@react-native-community/netinfo', () =>
  require('@react-native-community/netinfo/jest/netinfo-mock.js'),
);
jest.mock('react-native-safe-area-context', () => require('react-native-safe-area-context/jest/mock').default);

const mockPush = jest.fn();
jest.mock('expo-router', () => ({
  useRouter: () => ({ push: mockPush, replace: jest.fn(), back: jest.fn() }),
  useLocalSearchParams: () => ({}),
  Stack: { Screen: () => null },
}));

jest.mock('../lib/work-api', () => ({
  loadAssignedWork: jest.fn(),
  loadWorkDetail: jest.fn(),
}));
jest.mock('../lib/offline-queue', () => ({
  queueDepth: jest.fn(async () => ({ pending: 0, inFlight: 0, failed: 0, conflict: 0, outstanding: 0 })),
  subscribeToQueue: jest.fn(() => () => undefined),
  drainQueue: jest.fn(async () => undefined),
  generateIdempotencyKey: jest.fn(() => 'key'),
}));
jest.mock('../lib/pod-api', () => ({ syncPendingPodCaptures: jest.fn(async () => undefined) }));
jest.mock('../lib/location-tracker', () => ({
  locationTracker: { applyDutyStatus: jest.fn(async () => undefined) },
}));
jest.mock('../lib/hos-api', () => ({
  ...jest.requireActual('../lib/hos-api'),
  loadHOSAdvisory: jest.fn(async () => ({ advisory: null, authoritativeRecordStatement: null })),
}));
const mockIdentity = { current: { driver_id: 'drv-1', assigned_truck_id: 'Tanker 24', duty_status: 'off_duty' } };
jest.mock('../lib/duty-api', () => ({
  ...jest.requireActual('../lib/duty-api'),
  loadDriverIdentity: jest.fn(async () => mockIdentity.current),
  submitDutyStatus: jest.fn(async (control: string) => ({
    status: control === 'on_duty' ? 'active' : control,
  })),
}));

const workApi = jest.requireMock('../lib/work-api') as {
  loadAssignedWork: jest.Mock;
  loadWorkDetail: jest.Mock;
};
const dutyApi = jest.requireMock('../lib/duty-api') as { submitDutyStatus: jest.Mock };

beforeEach(() => {
  configureFormat({ timeZone: 'UTC', locale: 'en-US' });
  mockPush.mockReset();
  forgetPretripMarker();
  mockIdentity.current = { driver_id: 'drv-1', assigned_truck_id: 'Tanker 24', duty_status: 'off_duty' };
  workApi.loadAssignedWork.mockResolvedValue({
    data: [LATER_ORDER, NEXT_ORDER],
    pagination: { page: 1, size: 50, total: 2, total_pages: 1 },
  });
  workApi.loadWorkDetail.mockImplementation(async (id: string) => (id === NEXT_ORDER.order_id ? NEXT_ORDER : LATER_ORDER));
  dutyApi.submitDutyStatus.mockClear();
  jest.spyOn(Linking, 'openURL').mockResolvedValue(true);
});

afterEach(() => jest.restoreAllMocks());

it('shows one title, and the in-transit order as the next stop with product chips and gallons', async () => {
  renderWithQuery(<AssignedWorkScreen />);
  expect(screen.getAllByRole('heading')).toHaveLength(1);
  expect(screen.getByText('Today')).toBeTruthy();

  const card = await screen.findByTestId('next-stop');
  expect(within(card).getByText('Midwest Grain Cooperative')).toBeTruthy();
  expect(within(card).getByText('Window 08:30–10:30')).toBeTruthy();
  // Manifest lines summed by grade, product name not code, whole gallons.
  await waitFor(() => expect(within(card).getByText(/^Diesel #2 \(on-road\)/)).toBeTruthy());
  expect(within(card).getByText('4,200 gal')).toBeTruthy();
  expect(within(card).queryByText('DIESEL_2')).toBeNull();
  expect(within(card).getByText('In transit')).toBeTruthy();

  // The dispatched order is a compact row below.
  const rows = screen.getAllByTestId('stop-row');
  expect(rows).toHaveLength(1);
  expect(within(rows[0]).getByText('Riverside Truck Stop')).toBeTruthy();
  expect(within(rows[0]).getByLabelText('Regular unleaded')).toBeTruthy();
});

it('Navigate opens the maps URL for this platform and Call opens tel:', async () => {
  renderWithQuery(<AssignedWorkScreen />);
  const card = await screen.findByTestId('next-stop');
  fireEvent.press(within(card).getByLabelText(`Navigate to ${NEXT_ORDER.destination.address}`));
  // jest-expo runs as iOS.
  expect(Linking.openURL).toHaveBeenCalledWith(
    `maps:?daddr=${encodeURIComponent(NEXT_ORDER.destination.address)}`,
  );
  await waitFor(() => within(card).getByLabelText(`Call ${NEXT_ORDER.customer_name}`));
  fireEvent.press(within(card).getByLabelText(`Call ${NEXT_ORDER.customer_name}`));
  expect(Linking.openURL).toHaveBeenCalledWith('tel:+13175550142');
});

it('Arrive opens the existing Route check-in for the next pending stop', async () => {
  renderWithQuery(<AssignedWorkScreen />);
  const card = await screen.findByTestId('next-stop');
  await waitFor(() => expect(workApi.loadWorkDetail).toHaveBeenCalledWith('ord-1'));
  await waitFor(() => within(card).getByText(/^Diesel #2 \(on-road\)/));
  fireEvent.press(within(card).getByLabelText(/Arrive at Midwest Grain Cooperative/));
  expect(mockPush).toHaveBeenCalledWith({
    pathname: '/route',
    params: { orderId: 'ord-1', checkin: '0' },
  });
});

it('the duty chip opens the duty sheet with checked radios, and going on duty uses the same mutation then opens the pre-trip form', async () => {
  renderWithQuery(<AssignedWorkScreen />);
  const chip = await screen.findByLabelText('Duty status: Off duty. Change duty status');
  fireEvent.press(chip);

  const offDuty = await screen.findByTestId('duty-off_duty');
  expect(offDuty.props.accessibilityState).toEqual(expect.objectContaining({ checked: true }));
  expect(screen.getByTestId('duty-on_duty').props.accessibilityState).toEqual(
    expect.objectContaining({ checked: false }),
  );

  await act(async () => {
    fireEvent.press(screen.getByTestId('duty-on_duty'));
  });
  expect(dutyApi.submitDutyStatus).toHaveBeenCalledWith('on_duty');
  await waitFor(() =>
    expect(mockPush).toHaveBeenCalledWith({
      pathname: '/inspection/new',
      params: { inspectionType: 'pre_trip', assetId: 'Tanker 24' },
    }),
  );
});

it('does not reopen the pre-trip form when one was queued today', async () => {
  markPretripQueued(localCalendarDay());
  renderWithQuery(<AssignedWorkScreen />);
  const start = await screen.findByTestId('start-shift');
  await act(async () => {
    fireEvent.press(within(start).getByText('Go on duty'));
  });
  expect(dutyApi.submitDutyStatus).toHaveBeenCalledWith('on_duty');
  expect(mockPush).not.toHaveBeenCalledWith(expect.objectContaining({ pathname: '/inspection/new' }));
});
