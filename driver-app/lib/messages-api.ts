/**
 * The dispatch thread — one order, one conversation.
 *
 * `POST /api/driver/orders/{order_id}/messages` derives the sender from the
 * verified session and rejects a body naming anyone else with 403
 * `SENDER_IDENTITY_MISMATCH` (R7.5, R7.6). `MessageRequest` still declares
 * `sender_id` and `sender_role` as required fields, so this module fills them
 * from the session identity rather than from anything a screen holds — the two
 * values the server accepts are the only two values it can be handed.
 *
 * Reads are `GET .../messages?page&size`, sorted by timestamp ascending server
 * side (R7.12); nothing is re-sorted here.
 *
 * Requirements: 7.5, 7.6, 7.12, 7.14
 */

import { apiRequest } from './api-client';
import { currentSessionIdentity } from './session';

/** The role a driver posts in. The server derives its own; this must match. */
export const DRIVER_SENDER_ROLE = 'driver';

/** One persisted `job_messages` document, as the thread read returns it. */
export interface ThreadMessage {
  message_id: string;
  order_id?: string;
  job_id?: string;
  driver_id?: string;
  sender_id: string;
  sender_role: string;
  body: string;
  timestamp: string;
}

export interface ThreadPage {
  data: ThreadMessage[];
  pagination?: {
    page: number;
    size: number;
    total: number;
    total_pages?: number;
  };
}

/** Raised when there is no session to derive a sender identity from. */
export class NoSessionIdentityError extends Error {
  constructor() {
    super('The driver session is not available, so no message can be sent.');
    this.name = 'NoSessionIdentityError';
  }
}

function threadPath(orderId: string): string {
  return `/api/driver/orders/${encodeURIComponent(orderId)}/messages`;
}

/**
 * Read one order's thread.
 *
 * A response whose `data` is not an array — which a degraded or preview
 * transport can produce — is normalized to an empty thread rather than thrown,
 * so the screen shows "no messages" instead of an error the driver cannot act on.
 */
export async function loadThread(
  orderId: string,
  options: { page?: number; size?: number } = {},
): Promise<ThreadPage> {
  const page = options.page ?? 1;
  const size = options.size ?? 50;
  const response = await apiRequest<ThreadPage>({
    method: 'GET',
    path: `${threadPath(orderId)}?page=${page}&size=${size}`,
  });
  return {
    data: Array.isArray(response?.data) ? response.data : [],
    pagination: response?.pagination,
  };
}

/**
 * Post one message to an order thread.
 *
 * `sender_id` is the session's `driver_id`, which is exactly the value the
 * server derives, so the mismatch rejection (R7.6) is unreachable from this app
 * by construction.
 */
export async function sendThreadMessage(args: {
  orderId: string;
  body: string;
}): Promise<ThreadMessage | null> {
  const identity = currentSessionIdentity();
  if (!identity) {
    throw new NoSessionIdentityError();
  }
  const response = await apiRequest<{ data?: ThreadMessage }>({
    method: 'POST',
    path: threadPath(args.orderId),
    body: {
      body: args.body,
      sender_id: identity.driverId,
      sender_role: DRIVER_SENDER_ROLE,
    },
  });
  return response?.data ?? null;
}
