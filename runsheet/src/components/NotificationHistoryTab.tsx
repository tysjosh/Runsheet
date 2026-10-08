import { Mail, MessageSquare, Phone, RefreshCw } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import {
  Button,
  type Column,
  DataTable,
  Drawer,
  Field,
  FilterChips,
  FilterPopover,
  IconButton,
  Select,
  StatusBadge,
  Toolbar,
} from "@/components/ui";
import { useNotificationWebSocket } from "../hooks/useNotificationWebSocket";
import { dateTime, humanize, number } from "../lib/format";
import {
  type DeliveryStatus,
  getNotificationSummary,
  getNotifications,
  type Notification,
  type NotificationChannel,
  type NotificationFilters,
  type NotificationSummary,
  type NotificationType,
  retryNotification,
} from "../services/notificationApi";
import type { PaginationMeta } from "../services/schedulingApi";
import type { StatusKey } from "../styles/tokens";

// ─── Constants ───────────────────────────────────────────────────────────────

const NOTIFICATION_TYPES: { value: string; label: string }[] = [
  { value: "all", label: "All types" },
  { value: "delivery_confirmation", label: "Delivery Confirmation" },
  { value: "delay_alert", label: "Delay Alert" },
  { value: "eta_change", label: "ETA Change" },
  { value: "order_status_update", label: "Order Status Update" },
];

const CHANNELS: { value: string; label: string }[] = [
  { value: "all", label: "All channels" },
  { value: "sms", label: "SMS" },
  { value: "email", label: "Email" },
  { value: "whatsapp", label: "WhatsApp" },
];

/** Delivery status → badge style and label (icon + text, not colour alone). */
export const DELIVERY_STATUS: Record<
  DeliveryStatus,
  { status: StatusKey; label: string }
> = {
  pending: { status: "planned", label: "Pending" },
  sent: { status: "dispatched", label: "Sent" },
  delivered: { status: "delivered", label: "Delivered" },
  failed: { status: "exception", label: "Failed" },
};
const STATUS_IDS: DeliveryStatus[] = ["pending", "sent", "delivered", "failed"];

function DeliveryBadge({ status }: { status: string }) {
  const cfg = DELIVERY_STATUS[status as DeliveryStatus];
  return cfg ? (
    <StatusBadge status={cfg.status} label={cfg.label} />
  ) : (
    <span className="text-xs text-text-muted">{humanize(status)}</span>
  );
}

const PAGE_SIZE = 20;

// ─── Helpers ─────────────────────────────────────────────────────────────────

function getChannelIcon(channel: string) {
  switch (channel) {
    case "sms":
      return <Phone className="w-3.5 h-3.5" />;
    case "email":
      return <Mail className="w-3.5 h-3.5" />;
    case "whatsapp":
      return <MessageSquare className="w-3.5 h-3.5" />;
    default:
      return null;
  }
}

function getTypeLabel(type: string) {
  return humanize(type);
}

const CHANNEL_LABEL: Record<string, string> = {
  sms: "SMS",
  email: "Email",
  whatsapp: "WhatsApp",
};
const channelLabel = (c: string) => CHANNEL_LABEL[c] ?? humanize(c);

// ─── Table columns ───────────────────────────────────────────────────────────

const notificationColumns: Column<Notification>[] = [
  {
    key: "notification_type",
    label: "Type",
    render: (notification) => (
      <span className="font-medium text-text">
        {getTypeLabel(notification.notification_type)}
      </span>
    ),
  },
  {
    key: "channel",
    label: "Channel",
    render: (notification) => (
      <span className="inline-flex items-center gap-1.5 text-slate-700">
        {getChannelIcon(notification.channel)}
        {channelLabel(notification.channel)}
      </span>
    ),
  },
  {
    key: "recipient",
    label: "Recipient",
    render: (notification) => (
      <>
        <div className="text-text">
          {notification.recipient_name || notification.recipient_reference}
        </div>
        {notification.recipient_name && (
          <div className="text-xs text-text-muted">
            {notification.recipient_reference}
          </div>
        )}
      </>
    ),
  },
  {
    key: "subject",
    label: "Subject",
    render: (notification) => (
      <span className="line-clamp-1 text-slate-700">
        {notification.subject || "—"}
      </span>
    ),
  },
  {
    key: "delivery_status",
    label: "Status",
    width: 120,
    render: (notification) => (
      <DeliveryBadge status={notification.delivery_status} />
    ),
  },
  {
    key: "related_entity",
    label: "Related",
    render: (notification) =>
      notification.related_entity_id ? (
        <div>
          <span className="font-medium text-text">
            {notification.related_entity_id}
          </span>
          {notification.related_entity_type && (
            <div className="text-xs text-text-muted">
              {humanize(notification.related_entity_type)}
            </div>
          )}
        </div>
      ) : (
        <span className="text-text-muted">—</span>
      ),
  },
  {
    key: "created_at",
    label: "Created",
    render: (notification) => (
      <span className="whitespace-nowrap text-slate-700">
        {dateTime(notification.created_at)}
      </span>
    ),
  },
];

// ─── Component ───────────────────────────────────────────────────────────────

/**
 * NotificationHistoryTab — notification history view with summary bar,
 * paginated table, search, filters, detail panel, retry, and real-time updates.
 *
 * Validates: Requirements 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 10.2, 10.3, 10.4
 */
export default function NotificationHistoryTab() {
  // ── State ────────────────────────────────────────────────────────────────
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [pagination, setPagination] = useState<PaginationMeta>({
    page: 1,
    size: PAGE_SIZE,
    total: 0,
    total_pages: 0,
  });
  const [summary, setSummary] = useState<NotificationSummary>({
    by_type: {},
    by_channel: {},
    by_status: {},
    total: 0,
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  const [filterType, setFilterType] = useState("all");
  const [filterChannel, setFilterChannel] = useState("all");
  const [filterStatus, setFilterStatus] = useState("all");
  const [selectedNotification, setSelectedNotification] =
    useState<Notification | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState("");
  const [currentPage, setCurrentPage] = useState(1);

  // ── WebSocket for real-time updates ──────────────────────────────────────
  const { lastNotificationCreated, lastStatusChanged } =
    useNotificationWebSocket({ autoConnect: true });

  // Prepend new notifications from WebSocket
  useEffect(() => {
    if (lastNotificationCreated?.notification) {
      setNotifications((prev) => [
        lastNotificationCreated.notification,
        ...prev,
      ]);
      // Update summary counts
      setSummary((prev) => ({
        ...prev,
        total: prev.total + 1,
        by_status: {
          ...prev.by_status,
          [lastNotificationCreated.notification.delivery_status]:
            (prev.by_status[
              lastNotificationCreated.notification.delivery_status
            ] || 0) + 1,
        },
        by_type: {
          ...prev.by_type,
          [lastNotificationCreated.notification.notification_type]:
            (prev.by_type[
              lastNotificationCreated.notification.notification_type
            ] || 0) + 1,
        },
        by_channel: {
          ...prev.by_channel,
          [lastNotificationCreated.notification.channel]:
            (prev.by_channel[lastNotificationCreated.notification.channel] ||
              0) + 1,
        },
      }));
    }
  }, [lastNotificationCreated]);

  // Update notification status from WebSocket
  useEffect(() => {
    if (lastStatusChanged) {
      setNotifications((prev) =>
        prev.map((n) =>
          n.notification_id === lastStatusChanged.notification_id
            ? {
                ...n,
                delivery_status:
                  lastStatusChanged.delivery_status as DeliveryStatus,
                updated_at: lastStatusChanged.updated_at,
                sent_at: lastStatusChanged.sent_at ?? n.sent_at,
                delivered_at: lastStatusChanged.delivered_at ?? n.delivered_at,
                failed_at: lastStatusChanged.failed_at ?? n.failed_at,
                failure_reason:
                  lastStatusChanged.failure_reason ?? n.failure_reason,
              }
            : n,
        ),
      );
      // Update selected notification if it matches
      if (
        selectedNotification?.notification_id ===
        lastStatusChanged.notification_id
      ) {
        setSelectedNotification((prev) =>
          prev
            ? {
                ...prev,
                delivery_status:
                  lastStatusChanged.delivery_status as DeliveryStatus,
                updated_at: lastStatusChanged.updated_at,
                sent_at: lastStatusChanged.sent_at ?? prev.sent_at,
                delivered_at:
                  lastStatusChanged.delivered_at ?? prev.delivered_at,
                failed_at: lastStatusChanged.failed_at ?? prev.failed_at,
                failure_reason:
                  lastStatusChanged.failure_reason ?? prev.failure_reason,
              }
            : prev,
        );
      }
    }
  }, [lastStatusChanged, selectedNotification?.notification_id]);

  // ── Data fetching ────────────────────────────────────────────────────────
  const buildFilters = useCallback((): NotificationFilters => {
    const filters: NotificationFilters = {
      page: currentPage,
      size: PAGE_SIZE,
    };
    if (filterType !== "all")
      filters.notification_type = filterType as NotificationType;
    if (filterChannel !== "all")
      filters.channel = filterChannel as NotificationChannel;
    if (filterStatus !== "all")
      filters.delivery_status = filterStatus as DeliveryStatus;
    if (searchTerm.trim()) filters.q = searchTerm.trim();
    return filters;
  }, [currentPage, filterType, filterChannel, filterStatus, searchTerm]);

  const loadNotifications = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const filters = buildFilters();
      const [notifResponse, summaryResponse] = await Promise.all([
        getNotifications(filters),
        getNotificationSummary(),
      ]);
      setNotifications(notifResponse.data);
      setPagination(notifResponse.pagination);
      setSummary(summaryResponse);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load notifications",
      );
    } finally {
      setLoading(false);
    }
  }, [buildFilters]);

  useEffect(() => {
    loadNotifications();
  }, [loadNotifications]);

  // ── Retry handler ────────────────────────────────────────────────────────
  const handleRetry = async (notificationId: string) => {
    setRetrying(true);
    setRetryError("");
    try {
      const updated = await retryNotification(notificationId);
      setNotifications((prev) =>
        prev.map((n) => (n.notification_id === notificationId ? updated : n)),
      );
      setSelectedNotification(updated);
    } catch (err) {
      setRetryError(
        err instanceof Error ? err.message : "Failed to retry notification",
      );
    } finally {
      setRetrying(false);
    }
  };

  // ── Search handler (debounced via page reset) ────────────────────────────
  const handleSearch = (value: string) => {
    setSearchTerm(value);
    setCurrentPage(1);
  };

  const handleFilterChange = (setter: (v: string) => void, value: string) => {
    setter(value);
    setCurrentPage(1);
  };

  // ── Render ───────────────────────────────────────────────────────────────
  const sel = selectedNotification;
  const closeDetail = () => {
    setSelectedNotification(null);
    setRetryError("");
  };
  const filterCount =
    (filterType !== "all" ? 1 : 0) + (filterChannel !== "all" ? 1 : 0);
  return (
    <div className="flex h-full flex-1 flex-col bg-surface">
      <Toolbar
        label="Communications"
        search={
          <input
            type="search"
            placeholder="Search recipient, entity or message"
            aria-label="Search notifications"
            value={searchTerm}
            onChange={(e) => handleSearch(e.target.value)}
            className="h-7 w-full rounded-lg border border-slate-300 bg-surface px-2.5 text-xs text-slate-900 placeholder:text-slate-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          />
        }
        filters={
          <>
            <FilterChips
              label="Delivery status"
              collapse
              options={[
                { id: "all", label: "All", count: summary.total },
                ...STATUS_IDS.map((id) => ({
                  id,
                  label: DELIVERY_STATUS[id].label,
                  count: summary.by_status[id] || 0,
                  status: DELIVERY_STATUS[id].status,
                })),
              ]}
              value={filterStatus}
              onChange={(v) => handleFilterChange(setFilterStatus, v as string)}
            />
            <FilterPopover
              count={filterCount}
              label="Notification filters"
              onClear={() => {
                setFilterType("all");
                setFilterChannel("all");
                setCurrentPage(1);
              }}
            >
              <Field label="Type" id="notif-filter-type">
                <Select
                  id="notif-filter-type"
                  value={filterType}
                  onChange={(v) => handleFilterChange(setFilterType, v)}
                  options={NOTIFICATION_TYPES}
                />
              </Field>
              <Field label="Channel" id="notif-filter-channel">
                <Select
                  id="notif-filter-channel"
                  value={filterChannel}
                  onChange={(v) => handleFilterChange(setFilterChannel, v)}
                  options={CHANNELS}
                />
              </Field>
            </FilterPopover>
          </>
        }
        end={
          <IconButton
            label="Refresh"
            size="sm"
            onClick={() => loadNotifications()}
            icon={
              <RefreshCw
                className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`}
              />
            }
          />
        }
      />
      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<Notification>
          ariaLabel="Notification history"
          columns={notificationColumns}
          data={loading || error ? [] : notifications}
          loading={loading}
          error={error ? { message: error, onRetry: loadNotifications } : null}
          getRowId={(n) => n.notification_id}
          selectedId={sel?.notification_id}
          rowLabel={(n) =>
            `${getTypeLabel(n.notification_type)} to ${n.recipient_name || n.recipient_reference}`
          }
          onRowClick={(n) => {
            setSelectedNotification(n);
            setRetryError("");
          }}
          pagination={
            pagination.total_pages > 1
              ? {
                  page: currentPage,
                  totalPages: pagination.total_pages,
                  totalItems: pagination.total,
                  onPageChange: setCurrentPage,
                }
              : undefined
          }
          emptyState={
            <div className="text-text-muted">
              <p className="text-sm font-medium">No notifications found</p>
              <p className="mt-1 text-xs">
                Try adjusting your search or filters
              </p>
            </div>
          }
        />
      </div>

      <Drawer
        open={sel !== null}
        onClose={closeDetail}
        title="Notification details"
        width={420}
        footer={
          sel?.delivery_status === "failed" ? (
            <div className="flex flex-col gap-2">
              <Button
                onClick={() => handleRetry(sel.notification_id)}
                loading={retrying}
                icon={<RefreshCw className="h-3.5 w-3.5" />}
              >
                {retrying ? "Retrying…" : "Retry notification"}
              </Button>
              {retryError && (
                <p role="alert" className="text-xs text-red-700">
                  {retryError}
                </p>
              )}
            </div>
          ) : undefined
        }
      >
        {sel && (
          <dl className="space-y-4 text-sm">
            <Detail label="Notification ID">
              <span className="font-mono text-xs">{sel.notification_id}</span>
            </Detail>
            <div className="grid grid-cols-2 gap-4">
              <Detail label="Type">
                {getTypeLabel(sel.notification_type)}
              </Detail>
              <Detail label="Channel">
                <span className="inline-flex items-center gap-1.5">
                  {getChannelIcon(sel.channel)}
                  {channelLabel(sel.channel)}
                </span>
              </Detail>
            </div>
            <Detail label="Delivery status">
              <DeliveryBadge status={sel.delivery_status} />
            </Detail>
            <Detail label="Recipient">
              {sel.recipient_name || "—"}
              <span className="block text-xs text-text-muted">
                {sel.recipient_reference}
              </span>
            </Detail>
            {sel.subject && <Detail label="Subject">{sel.subject}</Detail>}
            <Detail label="Message">
              <div className="whitespace-pre-wrap rounded-lg border border-slate-200 bg-slate-50 p-3 leading-relaxed">
                {sel.message_body}
              </div>
            </Detail>
            {sel.related_entity_id && (
              <Detail label="Related">
                {sel.related_entity_id}
                {sel.related_entity_type && (
                  <span className="block text-xs text-text-muted">
                    {humanize(sel.related_entity_type)}
                  </span>
                )}
              </Detail>
            )}
            {sel.failure_reason && (
              <Detail label="Failure reason">
                <span className="block rounded-lg bg-red-50 px-3 py-2 text-red-800">
                  {sel.failure_reason}
                </span>
              </Detail>
            )}
            <Detail label="Retries">{number(sel.retry_count)}</Detail>
            <Detail label="Audit trail">
              <span className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
                {(
                  [
                    ["Created", sel.created_at],
                    ["Updated", sel.updated_at],
                    ["Sent", sel.sent_at],
                    ["Delivered", sel.delivered_at],
                    ["Failed", sel.failed_at],
                  ] as const
                ).map(([k, v]) => (
                  <span key={k} className="contents">
                    <span className="text-text-muted">{k}</span>
                    <span className="tabular-nums">{dateTime(v)}</span>
                  </span>
                ))}
              </span>
            </Detail>
          </dl>
        )}
      </Drawer>
    </div>
  );
}

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="mb-1 text-xs font-medium text-text-muted">{label}</dt>
      <dd className="text-text">{children}</dd>
    </div>
  );
}
