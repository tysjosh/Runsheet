import { API_TIMEOUTS, ApiError, ApiTimeoutError } from "./api";
import type { PaginatedResponse } from "./schedulingApi";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

// ─── Notification Types ──────────────────────────────────────────────────────

export type NotificationType =
  | "delivery_confirmation"
  | "delay_alert"
  | "eta_change"
  | "order_status_update";

export type DeliveryStatus = "pending" | "sent" | "delivered" | "failed";

export type NotificationChannel = "sms" | "email" | "whatsapp";

export interface Notification {
  notification_id: string;
  notification_type: NotificationType;
  channel: NotificationChannel;
  recipient_reference: string;
  recipient_name: string | null;
  subject: string | null;
  message_body: string;
  related_entity_type: string | null;
  related_entity_id: string | null;
  delivery_status: DeliveryStatus;
  created_at: string;
  updated_at: string;
  sent_at: string | null;
  delivered_at: string | null;
  failed_at: string | null;
  failure_reason: string | null;
  retry_count: number;
  tenant_id: string;
}

export interface EventPreference {
  event_type: string;
  enabled_channels: NotificationChannel[];
}

export interface NotificationPreference {
  preference_id: string;
  tenant_id: string;
  customer_id: string;
  customer_name: string;
  channels: Record<string, string>;
  event_preferences: EventPreference[];
  created_at: string;
  updated_at: string;
}

export interface NotificationTemplate {
  template_id: string;
  tenant_id: string;
  event_type: string;
  channel: NotificationChannel;
  subject_template: string | null;
  body_template: string;
  placeholders: string[];
  created_at: string;
  updated_at: string;
}

export interface NotificationRule {
  rule_id: string;
  tenant_id: string;
  event_type: string;
  enabled: boolean;
  default_channels: NotificationChannel[];
  template_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface NotificationSummary {
  by_type: Record<string, number>;
  by_channel: Record<string, number>;
  by_status: Record<string, number>;
  total: number;
}

// ─── Filter Types ────────────────────────────────────────────────────────────

export interface NotificationFilters {
  notification_type?: NotificationType;
  channel?: NotificationChannel;
  delivery_status?: DeliveryStatus;
  related_entity_id?: string;
  recipient_reference?: string;
  start_date?: string;
  end_date?: string;
  page?: number;
  size?: number;
}

export interface PreferenceFilters {
  search?: string;
  page?: number;
  size?: number;
}

export interface TemplateFilters {
  event_type?: string;
  channel?: NotificationChannel;
}

// ─── Request Payloads ────────────────────────────────────────────────────────

export interface RuleUpdatePayload {
  enabled?: boolean;
  default_channels?: string[];
  template_id?: string;
}

export interface PreferenceUpsertPayload {
  customer_name?: string;
  channels?: Record<string, string>;
  event_preferences?: EventPreference[];
}

export interface TemplateUpdatePayload {
  subject_template?: string;
  body_template?: string;
}

// ─── HTTP Helper ─────────────────────────────────────────────────────────────

async function fetchWithTimeout(
  url: string,
  options: RequestInit = {},
  timeout: number = API_TIMEOUTS.STANDARD,
): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
    });
    return response;
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new ApiTimeoutError(
        `Request timed out after ${timeout / 1000} seconds`,
      );
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
}

function buildQueryString(
  params: Record<string, string | number | boolean | undefined | null>,
): string {
  const entries = Object.entries(params).filter(
    ([, v]) => v !== undefined && v !== null && v !== "",
  );
  if (entries.length === 0) return "";
  const searchParams = new URLSearchParams();
  for (const [key, value] of entries) {
    searchParams.set(key, String(value));
  }
  return `?${searchParams.toString()}`;
}

async function notificationRequest<T>(
  endpoint: string,
  options?: RequestInit,
): Promise<T> {
  const url = `${API_BASE_URL}${endpoint}`;
  try {
    const response = await fetchWithTimeout(url, {
      headers: {
        "Content-Type": "application/json",
        ...options?.headers,
      },
      ...options,
    });

    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new ApiError(
        body.detail || body.message || `HTTP error! status: ${response.status}`,
        response.status,
      );
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiTimeoutError || error instanceof ApiError) {
      throw error;
    }
    throw new ApiError(
      error instanceof Error ? error.message : "Unknown error",
      0,
    );
  }
}

// ─── Notification History Endpoints ──────────────────────────────────────────

/** GET /api/notifications — paginated notification list with filters */
export async function getNotifications(
  filters: NotificationFilters = {},
): Promise<PaginatedResponse<Notification>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  const raw = await notificationRequest<{
    items: Notification[];
    total: number;
    page: number;
    size: number;
  }>(`/notifications${qs}`);
  return {
    data: raw.items ?? [],
    pagination: {
      page: raw.page ?? 1,
      size: raw.size ?? 20,
      total: raw.total ?? 0,
      total_pages: raw.size > 0 ? Math.ceil((raw.total ?? 0) / raw.size) : 0,
    },
    request_id: "",
  };
}

/** GET /api/notifications/:id — single notification with full audit trail */
export async function getNotification(
  notificationId: string,
): Promise<Notification> {
  return notificationRequest<Notification>(
    `/notifications/${encodeURIComponent(notificationId)}`,
  );
}

/** POST /api/notifications/:id/retry — retry a failed notification */
export async function retryNotification(
  notificationId: string,
): Promise<Notification> {
  return notificationRequest<Notification>(
    `/notifications/${encodeURIComponent(notificationId)}/retry`,
    { method: "POST" },
  );
}

/** GET /api/notifications/summary — aggregate counts by type, channel, status */
export async function getNotificationSummary(
  startDate?: string,
  endDate?: string,
): Promise<NotificationSummary> {
  const qs = buildQueryString({ start_date: startDate, end_date: endDate });
  return notificationRequest<NotificationSummary>(
    `/notifications/summary${qs}`,
  );
}

// ─── Notification Rules Endpoints ────────────────────────────────────────────

/** GET /api/notifications/rules — list all notification rules */
export async function getNotificationRules(): Promise<{
  items: NotificationRule[];
}> {
  return notificationRequest<{ items: NotificationRule[] }>(
    "/notifications/rules",
  );
}

/** PATCH /api/notifications/rules/:ruleId — update a notification rule */
export async function updateNotificationRule(
  ruleId: string,
  updates: RuleUpdatePayload,
): Promise<NotificationRule> {
  return notificationRequest<NotificationRule>(
    `/notifications/rules/${encodeURIComponent(ruleId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(updates),
    },
  );
}

// ─── Customer Preferences Endpoints ──────────────────────────────────────────

/** GET /api/notifications/preferences — paginated list of customer preferences */
export async function getNotificationPreferences(
  params: PreferenceFilters = {},
): Promise<PaginatedResponse<NotificationPreference>> {
  const qs = buildQueryString(
    params as Record<string, string | number | boolean | undefined>,
  );
  const raw = await notificationRequest<{
    items: NotificationPreference[];
    total: number;
    page: number;
    size: number;
  }>(`/notifications/preferences${qs}`);
  return {
    data: raw.items ?? [],
    pagination: {
      page: raw.page ?? 1,
      size: raw.size ?? 20,
      total: raw.total ?? 0,
      total_pages: raw.size > 0 ? Math.ceil((raw.total ?? 0) / raw.size) : 0,
    },
    request_id: "",
  };
}

/** GET /api/notifications/preferences/:customerId — get customer preference */
export async function getNotificationPreference(
  customerId: string,
): Promise<NotificationPreference> {
  return notificationRequest<NotificationPreference>(
    `/notifications/preferences/${encodeURIComponent(customerId)}`,
  );
}

/** PUT /api/notifications/preferences/:customerId — create or update preference */
export async function upsertNotificationPreference(
  customerId: string,
  data: PreferenceUpsertPayload,
): Promise<NotificationPreference> {
  return notificationRequest<NotificationPreference>(
    `/notifications/preferences/${encodeURIComponent(customerId)}`,
    {
      method: "PUT",
      body: JSON.stringify(data),
    },
  );
}

// ─── Template Endpoints ──────────────────────────────────────────────────────

/** GET /api/notifications/templates — list templates with optional filters */
export async function getNotificationTemplates(
  params: TemplateFilters = {},
): Promise<{ items: NotificationTemplate[] }> {
  const qs = buildQueryString(
    params as Record<string, string | number | boolean | undefined>,
  );
  return notificationRequest<{ items: NotificationTemplate[] }>(
    `/notifications/templates${qs}`,
  );
}

/** PUT /api/notifications/templates/:templateId — update a template */
export async function updateNotificationTemplate(
  templateId: string,
  updates: TemplateUpdatePayload,
): Promise<NotificationTemplate> {
  return notificationRequest<NotificationTemplate>(
    `/notifications/templates/${encodeURIComponent(templateId)}`,
    {
      method: "PUT",
      body: JSON.stringify(updates),
    },
  );
}
