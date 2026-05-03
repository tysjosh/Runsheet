import {
  AlertTriangle,
  Bell,
  CheckCircle,
  ChevronLeft,
  ChevronRight,
  Clock,
  Filter,
  Mail,
  MessageSquare,
  Phone,
  RefreshCw,
  Search,
  Send,
  X,
  XCircle,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNotificationWebSocket } from "../hooks/useNotificationWebSocket";
import {
  getNotifications,
  getNotificationSummary,
  retryNotification,
  type DeliveryStatus,
  type Notification,
  type NotificationChannel,
  type NotificationFilters,
  type NotificationSummary,
  type NotificationType,
} from "../services/notificationApi";
import type { PaginationMeta } from "../services/schedulingApi";

// ─── Constants ───────────────────────────────────────────────────────────────

const NOTIFICATION_TYPES: { value: string; label: string }[] = [
  { value: "all", label: "All Types" },
  { value: "delivery_confirmation", label: "Delivery Confirmation" },
  { value: "delay_alert", label: "Delay Alert" },
  { value: "eta_change", label: "ETA Change" },
  { value: "order_status_update", label: "Order Status Update" },
];

const CHANNELS: { value: string; label: string }[] = [
  { value: "all", label: "All Channels" },
  { value: "sms", label: "SMS" },
  { value: "email", label: "Email" },
  { value: "whatsapp", label: "WhatsApp" },
];

const STATUSES: { value: string; label: string }[] = [
  { value: "all", label: "All Statuses" },
  { value: "pending", label: "Pending" },
  { value: "sent", label: "Sent" },
  { value: "delivered", label: "Delivered" },
  { value: "failed", label: "Failed" },
];

const PAGE_SIZE = 20;

// ─── Helpers ─────────────────────────────────────────────────────────────────

function getStatusColor(status: string) {
  switch (status) {
    case "pending":
      return "text-yellow-700 bg-yellow-50";
    case "sent":
      return "text-blue-700 bg-blue-50";
    case "delivered":
      return "text-green-700 bg-green-50";
    case "failed":
      return "text-red-700 bg-red-50";
    default:
      return "text-gray-700 bg-gray-50";
  }
}

function getStatusIcon(status: string) {
  switch (status) {
    case "pending":
      return <Clock className="w-3.5 h-3.5" />;
    case "sent":
      return <Send className="w-3.5 h-3.5" />;
    case "delivered":
      return <CheckCircle className="w-3.5 h-3.5" />;
    case "failed":
      return <XCircle className="w-3.5 h-3.5" />;
    default:
      return null;
  }
}

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
  return type
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function formatDate(dateStr: string | null | undefined) {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatFullDate(dateStr: string | null | undefined) {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

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
      setNotifications((prev) => [lastNotificationCreated.notification, ...prev]);
      // Update summary counts
      setSummary((prev) => ({
        ...prev,
        total: prev.total + 1,
        by_status: {
          ...prev.by_status,
          [lastNotificationCreated.notification.delivery_status]:
            (prev.by_status[lastNotificationCreated.notification.delivery_status] || 0) + 1,
        },
        by_type: {
          ...prev.by_type,
          [lastNotificationCreated.notification.notification_type]:
            (prev.by_type[lastNotificationCreated.notification.notification_type] || 0) + 1,
        },
        by_channel: {
          ...prev.by_channel,
          [lastNotificationCreated.notification.channel]:
            (prev.by_channel[lastNotificationCreated.notification.channel] || 0) + 1,
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
                delivery_status: lastStatusChanged.delivery_status as DeliveryStatus,
                updated_at: lastStatusChanged.updated_at,
                sent_at: lastStatusChanged.sent_at ?? n.sent_at,
                delivered_at: lastStatusChanged.delivered_at ?? n.delivered_at,
                failed_at: lastStatusChanged.failed_at ?? n.failed_at,
                failure_reason: lastStatusChanged.failure_reason ?? n.failure_reason,
              }
            : n,
        ),
      );
      // Update selected notification if it matches
      if (selectedNotification?.notification_id === lastStatusChanged.notification_id) {
        setSelectedNotification((prev) =>
          prev
            ? {
                ...prev,
                delivery_status: lastStatusChanged.delivery_status as DeliveryStatus,
                updated_at: lastStatusChanged.updated_at,
                sent_at: lastStatusChanged.sent_at ?? prev.sent_at,
                delivered_at: lastStatusChanged.delivered_at ?? prev.delivered_at,
                failed_at: lastStatusChanged.failed_at ?? prev.failed_at,
                failure_reason: lastStatusChanged.failure_reason ?? prev.failure_reason,
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
    if (filterType !== "all") filters.notification_type = filterType as NotificationType;
    if (filterChannel !== "all") filters.channel = filterChannel as NotificationChannel;
    if (filterStatus !== "all") filters.delivery_status = filterStatus as DeliveryStatus;
    if (searchTerm.trim()) filters.recipient_reference = searchTerm.trim();
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

  const handleFilterChange = (
    setter: (v: string) => void,
    value: string,
  ) => {
    setter(value);
    setCurrentPage(1);
  };

  // ── Summary counts ───────────────────────────────────────────────────────
  const summaryStats = useMemo(
    () => [
      {
        label: "Total",
        value: summary.total,
        color: "text-[#232323]",
        icon: <Bell className="w-5 h-5 text-gray-400" />,
      },
      {
        label: "Sent",
        value: summary.by_status.sent || 0,
        color: "text-blue-600",
        icon: <Send className="w-5 h-5 text-blue-400" />,
      },
      {
        label: "Delivered",
        value: summary.by_status.delivered || 0,
        color: "text-green-600",
        icon: <CheckCircle className="w-5 h-5 text-green-400" />,
      },
      {
        label: "Failed",
        value: summary.by_status.failed || 0,
        color: "text-red-600",
        icon: <AlertTriangle className="w-5 h-5 text-red-400" />,
      },
    ],
    [summary],
  );

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div className="flex-1 flex bg-white overflow-hidden">
      {/* Main content */}
      <div className="flex-1 flex flex-col">
        {/* Header */}
        <div className="border-b border-gray-100 px-8 py-6">
          <div className="flex items-center gap-3 mb-6">
            <div className="w-10 h-10 bg-[#232323] rounded-xl flex items-center justify-center">
              <Bell className="w-5 h-5 text-white" />
            </div>
            <div>
              <h1 className="text-2xl font-semibold text-[#232323]">
                Notification History
              </h1>
              <p className="text-gray-500">
                Track and manage customer notifications
              </p>
            </div>
          </div>

          {/* Search and Filters */}
          <div className="flex gap-4">
            <div className="flex-1 relative">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-400" />
              <input
                type="text"
                placeholder="Search by recipient, entity ID, or message..."
                value={searchTerm}
                onChange={(e) => handleSearch(e.target.value)}
                className="w-full pl-10 pr-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
              />
            </div>
            <div className="relative">
              <Filter className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-400" />
              <select
                value={filterType}
                onChange={(e) => handleFilterChange(setFilterType, e.target.value)}
                className="pl-10 pr-8 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white min-w-[180px]"
              >
                {NOTIFICATION_TYPES.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </select>
            </div>
            <select
              value={filterChannel}
              onChange={(e) => handleFilterChange(setFilterChannel, e.target.value)}
              className="px-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white min-w-[130px]"
            >
              {CHANNELS.map((c) => (
                <option key={c.value} value={c.value}>
                  {c.label}
                </option>
              ))}
            </select>
            <select
              value={filterStatus}
              onChange={(e) => handleFilterChange(setFilterStatus, e.target.value)}
              className="px-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white min-w-[130px]"
            >
              {STATUSES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* Summary Bar */}
        <div className="border-b border-gray-100 px-8 py-4">
          <div className="grid grid-cols-4 gap-6">
            {summaryStats.map((stat) => (
              <div key={stat.label} className="flex items-center gap-3">
                {stat.icon}
                <div>
                  <div className={`text-2xl font-semibold ${stat.color}`}>
                    {stat.value}
                  </div>
                  <div className="text-sm text-gray-500">{stat.label}</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Error Banner */}
        {error && (
          <div className="mx-8 mt-4 bg-red-50 text-red-700 px-4 py-3 rounded-xl text-sm">
            {error}
            <button
              onClick={loadNotifications}
              className="ml-3 underline hover:no-underline"
            >
              Retry
            </button>
          </div>
        )}

        {/* Table */}
        <div className="flex-1 overflow-y-auto">
          {loading ? (
            <div className="flex items-center justify-center py-16">
              <div className="text-center">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-[#232323] mx-auto mb-3" />
                <p className="text-sm text-gray-500">Loading notifications...</p>
              </div>
            </div>
          ) : (
            <table className="w-full">
              <thead className="bg-gray-50 sticky top-0 border-b border-gray-100">
                <tr>
                  <th className="px-8 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                    Type
                  </th>
                  <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                    Channel
                  </th>
                  <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                    Recipient
                  </th>
                  <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                    Subject
                  </th>
                  <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                    Status
                  </th>
                  <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                    Related Entity
                  </th>
                  <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                    Created
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {notifications.map((notification) => (
                  <tr
                    key={notification.notification_id}
                    className={`cursor-pointer transition-colors ${
                      selectedNotification?.notification_id === notification.notification_id
                        ? "bg-gray-50"
                        : "hover:bg-gray-50"
                    }`}
                    onClick={() => {
                      setSelectedNotification(notification);
                      setRetryError("");
                    }}
                  >
                    <td className="px-8 py-4">
                      <span className="text-sm font-medium text-[#232323]">
                        {getTypeLabel(notification.notification_type)}
                      </span>
                    </td>
                    <td className="px-6 py-4">
                      <span className="inline-flex items-center gap-1.5 text-sm text-gray-700">
                        {getChannelIcon(notification.channel)}
                        {notification.channel.toUpperCase()}
                      </span>
                    </td>
                    <td className="px-6 py-4">
                      <div className="text-sm text-[#232323]">
                        {notification.recipient_name || notification.recipient_reference}
                      </div>
                      {notification.recipient_name && (
                        <div className="text-xs text-gray-500">
                          {notification.recipient_reference}
                        </div>
                      )}
                    </td>
                    <td className="px-6 py-4">
                      <span className="text-sm text-gray-700 line-clamp-1">
                        {notification.subject || "—"}
                      </span>
                    </td>
                    <td className="px-6 py-4">
                      <span
                        className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-medium ${getStatusColor(notification.delivery_status)}`}
                      >
                        {getStatusIcon(notification.delivery_status)}
                        {notification.delivery_status.charAt(0).toUpperCase() +
                          notification.delivery_status.slice(1)}
                      </span>
                    </td>
                    <td className="px-6 py-4">
                      {notification.related_entity_id ? (
                        <div>
                          <span className="text-sm text-[#232323] font-medium">
                            {notification.related_entity_id}
                          </span>
                          {notification.related_entity_type && (
                            <div className="text-xs text-gray-500">
                              {notification.related_entity_type}
                            </div>
                          )}
                        </div>
                      ) : (
                        <span className="text-sm text-gray-400">—</span>
                      )}
                    </td>
                    <td className="px-6 py-4">
                      <span className="text-sm text-gray-600">
                        {formatDate(notification.created_at)}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {!loading && notifications.length === 0 && (
            <div className="text-center py-16 text-gray-500">
              <Bell className="w-16 h-16 mx-auto mb-4 text-gray-300" />
              <p className="text-lg font-medium text-gray-400">
                No notifications found
              </p>
              <p className="text-sm text-gray-400 mt-1">
                Try adjusting your search or filter criteria
              </p>
            </div>
          )}
        </div>

        {/* Pagination */}
        {pagination.total_pages > 1 && (
          <div className="border-t border-gray-100 px-8 py-4 flex items-center justify-between">
            <div className="text-sm text-gray-500">
              Showing {(currentPage - 1) * PAGE_SIZE + 1}–
              {Math.min(currentPage * PAGE_SIZE, pagination.total)} of{" "}
              {pagination.total} notifications
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
                disabled={currentPage <= 1}
                className="p-2 rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <span className="text-sm text-gray-700 px-3">
                Page {currentPage} of {pagination.total_pages}
              </span>
              <button
                onClick={() =>
                  setCurrentPage((p) => Math.min(pagination.total_pages, p + 1))
                }
                disabled={currentPage >= pagination.total_pages}
                className="p-2 rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Detail Panel */}
      {selectedNotification && (
        <div className="w-96 border-l border-gray-100 bg-gray-50 flex flex-col">
          <div className="px-6 py-4 border-b border-gray-100">
            <div className="flex items-center justify-between">
              <h3 className="font-semibold text-[#232323]">
                Notification Details
              </h3>
              <button
                onClick={() => {
                  setSelectedNotification(null);
                  setRetryError("");
                }}
                className="text-gray-400 hover:text-[#232323] p-2 rounded-lg hover:bg-white transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto p-6 space-y-6">
            {/* Notification ID */}
            <div>
              <label className="block text-sm font-medium text-gray-500 mb-2">
                Notification ID
              </label>
              <p className="text-sm text-[#232323] font-mono">
                {selectedNotification.notification_id}
              </p>
            </div>

            {/* Type & Channel */}
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-500 mb-2">
                  Type
                </label>
                <span className="text-sm text-[#232323] font-medium">
                  {getTypeLabel(selectedNotification.notification_type)}
                </span>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-500 mb-2">
                  Channel
                </label>
                <span className="inline-flex items-center gap-1.5 text-sm text-[#232323]">
                  {getChannelIcon(selectedNotification.channel)}
                  {selectedNotification.channel.toUpperCase()}
                </span>
              </div>
            </div>

            {/* Status */}
            <div>
              <label className="block text-sm font-medium text-gray-500 mb-2">
                Delivery Status
              </label>
              <span
                className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-medium ${getStatusColor(selectedNotification.delivery_status)}`}
              >
                {getStatusIcon(selectedNotification.delivery_status)}
                {selectedNotification.delivery_status.charAt(0).toUpperCase() +
                  selectedNotification.delivery_status.slice(1)}
              </span>
            </div>

            {/* Recipient */}
            <div>
              <label className="block text-sm font-medium text-gray-500 mb-2">
                Recipient
              </label>
              <p className="text-sm text-[#232323]">
                {selectedNotification.recipient_name || "—"}
              </p>
              <p className="text-xs text-gray-500 mt-0.5">
                {selectedNotification.recipient_reference}
              </p>
            </div>

            {/* Subject */}
            {selectedNotification.subject && (
              <div>
                <label className="block text-sm font-medium text-gray-500 mb-2">
                  Subject
                </label>
                <p className="text-sm text-[#232323]">
                  {selectedNotification.subject}
                </p>
              </div>
            )}

            {/* Message Body */}
            <div>
              <label className="block text-sm font-medium text-gray-500 mb-2">
                Message Body
              </label>
              <div className="bg-white border border-gray-200 rounded-lg p-3 text-sm text-[#232323] leading-relaxed whitespace-pre-wrap">
                {selectedNotification.message_body}
              </div>
            </div>

            {/* Related Entity */}
            {selectedNotification.related_entity_id && (
              <div>
                <label className="block text-sm font-medium text-gray-500 mb-2">
                  Related Entity
                </label>
                <p className="text-sm text-[#232323] font-medium">
                  {selectedNotification.related_entity_id}
                </p>
                {selectedNotification.related_entity_type && (
                  <p className="text-xs text-gray-500 mt-0.5">
                    Type: {selectedNotification.related_entity_type}
                  </p>
                )}
              </div>
            )}

            {/* Failure Reason */}
            {selectedNotification.failure_reason && (
              <div>
                <label className="block text-sm font-medium text-gray-500 mb-2">
                  Failure Reason
                </label>
                <div className="bg-red-50 text-red-700 px-3 py-2 rounded-lg text-sm">
                  {selectedNotification.failure_reason}
                </div>
              </div>
            )}

            {/* Retry Count */}
            <div>
              <label className="block text-sm font-medium text-gray-500 mb-2">
                Retry Count
              </label>
              <p className="text-sm text-[#232323]">
                {selectedNotification.retry_count}
              </p>
            </div>

            {/* Audit Trail Timestamps */}
            <div>
              <label className="block text-sm font-medium text-gray-500 mb-2">
                Audit Trail
              </label>
              <div className="space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-gray-500">Created</span>
                  <span className="text-[#232323]">
                    {formatFullDate(selectedNotification.created_at)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-500">Updated</span>
                  <span className="text-[#232323]">
                    {formatFullDate(selectedNotification.updated_at)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-500">Sent</span>
                  <span className="text-[#232323]">
                    {formatFullDate(selectedNotification.sent_at)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-500">Delivered</span>
                  <span className="text-[#232323]">
                    {formatFullDate(selectedNotification.delivered_at)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-500">Failed</span>
                  <span className="text-[#232323]">
                    {formatFullDate(selectedNotification.failed_at)}
                  </span>
                </div>
              </div>
            </div>

            {/* Retry Button for failed notifications */}
            {selectedNotification.delivery_status === "failed" && (
              <div>
                <button
                  onClick={() => handleRetry(selectedNotification.notification_id)}
                  disabled={retrying}
                  className="w-full flex items-center justify-center gap-2 px-4 py-2.5 text-sm text-white rounded-lg transition-colors hover:opacity-90 disabled:opacity-50"
                  style={{ backgroundColor: "#232323" }}
                >
                  <RefreshCw className={`w-4 h-4 ${retrying ? "animate-spin" : ""}`} />
                  {retrying ? "Retrying..." : "Retry Notification"}
                </button>
                {retryError && (
                  <p className="text-xs text-red-600 mt-2">{retryError}</p>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
