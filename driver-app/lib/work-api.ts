import { apiRequest } from './api-client';
import { demoPreviewEnabled } from './demo-preview';
import { drainQueue, enqueueMutation } from './offline-queue';
import type { FuelOrder, OrderStatus } from '@/types/order';

export interface WorkResponse {
  data: FuelOrder[];
  pagination: {
    page: number;
    size: number;
    total: number;
    total_pages: number;
  };
}

export interface WorkDetailResponse {
  data: FuelOrder;
  request_id?: string;
}

export async function loadAssignedWork(): Promise<WorkResponse> {
  return apiRequest<WorkResponse>({
    method: 'GET',
    path: '/api/driver/work?status=dispatched&status=in_transit&size=50',
  });
}

export async function loadWorkDetail(orderId: string): Promise<FuelOrder> {
  const response = await apiRequest<WorkDetailResponse>({
    method: 'GET',
    path: `/api/driver/work/${encodeURIComponent(orderId)}`,
  });
  return response.data;
}

/**
 * Persist a driver status action before attempting the network.  The existing
 * queue keeps the same idempotency key and order-relative sequence across
 * reconnects, so "start delivery" cannot disappear in a dead zone.
 */
export async function queueOrderStatus(
  orderId: string,
  status: Extract<OrderStatus, 'in_transit'>,
): Promise<{ synced: boolean }> {
  const eventTimestamp = new Date().toISOString();
  if (demoPreviewEnabled) {
    await apiRequest({
      method: 'POST',
      path: `/api/driver/orders/${encodeURIComponent(orderId)}/status`,
      body: {
        status,
        event_timestamp: eventTimestamp,
      },
    });
    return { synced: true };
  }
  await enqueueMutation({
    kind: 'order_status',
    method: 'POST',
    path: `/api/driver/orders/${encodeURIComponent(orderId)}/status`,
    orderId,
    eventTimestamp,
    body: {
      status,
      event_timestamp: eventTimestamp,
    },
  });
  const summary = await drainQueue();
  return {
    synced:
      summary.succeeded > 0 &&
      summary.failed === 0 &&
      summary.conflicted === 0 &&
      !summary.offline,
  };
}
