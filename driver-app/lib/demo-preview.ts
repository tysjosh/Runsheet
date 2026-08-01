import { Platform } from 'react-native';

import { configureApiClient } from './api-client';
import type { FuelOrder } from '@/types/order';

/**
 * Local browser-only showroom mode. It is unavailable in release builds and
 * native apps, so production authentication and transport behavior cannot
 * accidentally fall back to demo data.
 */
export const demoPreviewEnabled =
  __DEV__ &&
  Platform.OS === 'web' &&
  process.env.EXPO_PUBLIC_DEMO_MODE === 'true';

const orders: FuelOrder[] = [
  {
    order_id: 'ord-demo-1001',
    status: 'dispatched',
    delivery_window_start: '2026-07-30T08:30:00-04:00',
    delivery_window_end: '2026-07-30T10:30:00-04:00',
    destination: {
      address: '1840 County Road 12, Greenfield, IN',
      lat: 39.785,
      lon: -85.769,
    },
    customer_name: 'Midwest Grain Cooperative',
    customer_phone: '+1 (317) 555-0142',
    product_grade: 'Diesel #2',
    ordered_gallons: 4200,
    quantity_unit: 'us_gallon',
    manifest_available: true,
    compartment_manifest: [
      {
        compartment_id: 'C-1',
        product_grade: 'Diesel #2',
        planned_gallons: 2500,
        prior_product_grade: 'Diesel #2',
        cross_contamination_warning: false,
        last_cleaned_at: '2026-07-28T17:20:00-04:00',
      },
      {
        compartment_id: 'C-2',
        product_grade: 'Diesel #2',
        planned_gallons: 1700,
        prior_product_grade: 'Regular 87',
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
        planned_arrival: '2026-07-30T09:05:00-04:00',
        planned_gallons_by_grade: { 'Diesel #2': 4200 },
        status: 'pending',
      },
      {
        sequence: 1,
        station_id: 'TANK-RTS-02',
        lat: 39.824,
        lon: -85.612,
        planned_arrival: '2026-07-30T11:20:00-04:00',
        planned_gallons_by_grade: { 'Regular 87': 3100 },
        status: 'pending',
      },
    ],
  },
  {
    order_id: 'ord-demo-1002',
    status: 'in_transit',
    delivery_window_start: '2026-07-30T10:45:00-04:00',
    delivery_window_end: '2026-07-30T12:30:00-04:00',
    destination: {
      address: '902 East Main Street, New Palestine, IN',
      lat: 39.721,
      lon: -85.889,
    },
    customer_name: 'Riverside Truck Stop',
    customer_phone: '+1 (317) 555-0198',
    product_grade: 'Regular 87',
    ordered_gallons: 3100,
    quantity_unit: 'us_gallon',
    manifest_available: true,
    compartment_manifest: [
      {
        compartment_id: 'C-3',
        product_grade: 'Regular 87',
        planned_gallons: 3100,
        prior_product_grade: 'Regular 87',
        cross_contamination_warning: false,
        last_cleaned_at: '2026-07-29T06:40:00-04:00',
      },
    ],
    route_available: true,
    stops: [
      {
        sequence: 1,
        station_id: 'TANK-RTS-02',
        lat: 39.721,
        lon: -85.889,
        planned_arrival: '2026-07-30T11:20:00-04:00',
        planned_gallons_by_grade: { 'Regular 87': 3100 },
        status: 'pending',
      },
    ],
  },
];

interface DemoMessage {
  message_id: string;
  order_id: string;
  sender_id: string;
  sender_role: string;
  body: string;
  timestamp: string;
}

const messages: DemoMessage[] = [
  {
    message_id: 'msg-demo-1',
    order_id: 'ord-demo-1002',
    sender_id: 'dispatcher-demo-3',
    sender_role: 'dispatcher',
    body: 'Riverside gate code is 4417. Call the manager when you arrive.',
    timestamp: '2026-07-30T10:12:00-04:00',
  },
];

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'Content-Type': 'application/json',
      'X-Request-ID': 'demo-preview',
    },
  });
}

async function demoFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const url = new URL(
    typeof input === 'string' ? input : input instanceof URL ? input : input.url,
  );
  const method = (init?.method ?? 'GET').toUpperCase();

  if (url.pathname === '/auth/driver/session' && method === 'POST') {
    return json({
      access_token: 'demo-access-token',
      refresh_token: 'demo-refresh-token',
      token_type: 'bearer',
      expires_in: 3600,
      driver_id: 'driver-demo-17',
      tenant_id: 'tenant-demo',
    });
  }
  if (
    url.pathname === '/auth/driver/session/refresh' &&
    method === 'POST'
  ) {
    return json({
      access_token: 'demo-access-token-refreshed',
      refresh_token: 'demo-refresh-token-refreshed',
      token_type: 'bearer',
      expires_in: 3600,
      driver_id: 'driver-demo-17',
      tenant_id: 'tenant-demo',
    });
  }
  if (url.pathname === '/auth/driver/session' && method === 'DELETE') {
    return json({ revoked: true });
  }
  if (url.pathname === '/api/driver/work' && method === 'GET') {
    return json({
      data: orders,
      pagination: {
        page: 1,
        size: 50,
        total: orders.length,
        total_pages: 1,
      },
    });
  }
  if (url.pathname === '/api/driver/me' && method === 'GET') {
    return json({
      data: {
        driver_id: 'driver-demo-17',
        tenant_id: 'tenant-demo',
        driver_name: 'Jordan Ellis',
        assigned_truck_id: 'Tanker 24',
        duty_status: 'active',
        duty_status_updated_at: '2026-07-30T07:45:00-04:00',
      },
    });
  }

  const detailMatch = url.pathname.match(/^\/api\/driver\/work\/([^/]+)$/);
  if (detailMatch && method === 'GET') {
    const order = orders.find(
      (candidate) => candidate.order_id === decodeURIComponent(detailMatch[1]),
    );
    return order
      ? json({ data: order, request_id: 'demo-preview' })
      : json({ error_code: 'RESOURCE_NOT_FOUND', message: 'Order not found' }, 404);
  }

  if (url.pathname === '/api/driver/duty-status' && method === 'POST') {
    return json({
      data: {
        previous_status: 'active',
        new_status: 'active',
        event_timestamp: new Date().toISOString(),
      },
    });
  }

  const messagesMatch = url.pathname.match(
    /^\/api\/driver\/orders\/([^/]+)\/messages$/,
  );
  if (messagesMatch) {
    const threadOrderId = decodeURIComponent(messagesMatch[1]);
    if (method === 'GET') {
      return json({
        data: messages.filter((entry) => entry.order_id === threadOrderId),
        pagination: { page: 1, size: 50, total: messages.length },
      });
    }
    if (method === 'POST') {
      let body: { body?: string } = {};
      try {
        body = JSON.parse(String(init?.body ?? '{}')) as { body?: string };
      } catch {
        body = {};
      }
      const posted = {
        message_id: `msg-demo-${messages.length + 1}`,
        order_id: threadOrderId,
        sender_id: 'driver-demo-17',
        sender_role: 'driver',
        body: body.body ?? '',
        timestamp: new Date().toISOString(),
      };
      messages.push(posted);
      return json({ data: posted });
    }
  }

  if (url.pathname === '/api/driver/pod/uploads/presign' && method === 'POST') {
    let body: { category?: string; content_type?: string } = {};
    try {
      body = JSON.parse(String(init?.body ?? '{}')) as {
        category?: string;
        content_type?: string;
      };
    } catch {
      body = {};
    }
    return json({
      data: {
        file_ref: `tenants/tenant-demo/${body.category ?? 'photo'}/demo-${Date.now()}`,
        // The showroom never PUTs bytes anywhere real; the URL is TLS so the
        // client's own transport assertion still holds.
        upload_url: 'https://demo.invalid/upload',
        expires_at: new Date(Date.now() + 600_000).toISOString(),
        content_type: body.content_type ?? 'image/jpeg',
        max_file_bytes: 10 * 1024 * 1024,
      },
    });
  }

  const statusMatch = url.pathname.match(
    /^\/api\/driver\/orders\/([^/]+)\/status$/,
  );
  if (statusMatch && method === 'POST') {
    const order = orders.find(
      (candidate) => candidate.order_id === decodeURIComponent(statusMatch[1]),
    );
    if (order) {
      order.status = 'in_transit';
      return json({ data: order });
    }
  }

  return json({
    data: {},
    status: 'demo',
  });
}

export function installDemoPreview(): void {
  if (demoPreviewEnabled) {
    configureApiClient({ fetchImpl: demoFetch as typeof fetch });
  }
}
