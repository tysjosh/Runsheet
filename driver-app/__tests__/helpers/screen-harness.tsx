/**
 * Shared fixtures for the UI revamp screen tests: a QueryClient wrapper and two
 * orders shaped like `GET /api/driver/work/{id}`.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react-native';
import type { ReactElement } from 'react';

import type { FuelOrder } from '@/types/order';

export function renderWithQuery(ui: ReactElement) {
  const client = new QueryClient({
    // gcTime 0 for mutations too: the default keeps a 5-minute timer alive and
    // Jest would not exit.
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false, gcTime: 0 } },
  });
  const result = render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
  // Re-render the same tree with the same client (e.g. after changing params).
  const rerenderWithQuery = (next: ReactElement) =>
    result.rerender(<QueryClientProvider client={client}>{next}</QueryClientProvider>);
  return { ...result, rerenderWithQuery };
}

export const NEXT_ORDER: FuelOrder = {
  order_id: 'ord-1',
  status: 'in_transit',
  delivery_window_start: '2026-07-30T08:30:00Z',
  delivery_window_end: '2026-07-30T10:30:00Z',
  destination: { address: '1840 County Road 12, Greenfield, IN', lat: 39.785, lon: -85.769 },
  customer_name: 'Midwest Grain Cooperative',
  customer_phone: '+1 (317) 555-0142',
  product_grade: 'DIESEL_2',
  ordered_gallons: 4200,
  quantity_unit: 'us_gallon',
  manifest_available: true,
  compartment_manifest: [
    {
      compartment_id: 'C-1',
      product_grade: 'DIESEL_2',
      planned_gallons: 2500.4,
      prior_product_grade: 'DIESEL_2',
      cross_contamination_warning: false,
      last_cleaned_at: null,
    },
    {
      compartment_id: 'C-2',
      product_grade: 'DIESEL_2',
      planned_gallons: 1700,
      prior_product_grade: 'GASOLINE_REG',
      cross_contamination_warning: true,
      last_cleaned_at: null,
    },
  ],
  route_available: true,
  stops: [
    {
      sequence: 0,
      station_id: 'TANK-MGC-01',
      lat: 39.785,
      lon: -85.769,
      planned_arrival: '2026-07-30T09:05:00Z',
      planned_gallons_by_grade: { DIESEL_2: 4200 },
      status: 'pending',
    },
  ],
  plan_id: 'plan-1',
  route_id: 'route-1',
};

export const LATER_ORDER: FuelOrder = {
  order_id: 'ord-2',
  status: 'dispatched',
  delivery_window_start: '2026-07-30T12:45:00Z',
  delivery_window_end: '2026-07-30T14:30:00Z',
  destination: { address: '902 East Main Street, New Palestine, IN', lat: 39.721, lon: -85.889 },
  customer_name: 'Riverside Truck Stop',
  customer_phone: null,
  product_grade: 'GASOLINE_REG',
  ordered_gallons: 3100,
  quantity_unit: 'us_gallon',
};
