"use client";

/**
 * Global notification bell for the dashboard header.
 *
 * Shows a live unread badge and a dropdown of the most recent notifications,
 * with a link through to the full /dashboard/notifications history. Subscribes
 * to the same /ws/notifications channel as the history view, so new
 * notifications appear (and bump the unread count) in real time across every
 * dashboard page. "Unread" is tracked client-side as notifications that arrived
 * since the dropdown was last opened — the pipeline has no per-user read state.
 */

import {
  Bell,
  CheckCircle,
  Clock,
  Mail,
  MessageSquare,
  Phone,
  Send,
  XCircle,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNotificationWebSocket } from "../hooks/useNotificationWebSocket";
import {
  type DeliveryStatus,
  getNotifications,
  type Notification,
} from "../services/notificationApi";

const RECENT_LIMIT = 8;

function getStatusIcon(status: string) {
  switch (status) {
    case "pending":
      return <Clock className="w-3.5 h-3.5 text-warning" />;
    case "sent":
      return <Send className="w-3.5 h-3.5 text-info" />;
    case "delivered":
      return <CheckCircle className="w-3.5 h-3.5 text-success" />;
    case "failed":
      return <XCircle className="w-3.5 h-3.5 text-error" />;
    default:
      return null;
  }
}

function getChannelIcon(channel: string) {
  switch (channel) {
    case "sms":
      return <Phone className="w-3 h-3" />;
    case "email":
      return <Mail className="w-3 h-3" />;
    case "whatsapp":
      return <MessageSquare className="w-3 h-3" />;
    default:
      return null;
  }
}

function typeLabel(type: string) {
  return type
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function relativeTime(dateStr: string | null | undefined) {
  if (!dateStr) return "";
  const then = new Date(dateStr).getTime();
  if (Number.isNaN(then)) return "";
  const diff = Date.now() - then;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(dateStr).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
  });
}

export default function NotificationBell() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Notification[]>([]);
  const [unread, setUnread] = useState(0);
  const [loading, setLoading] = useState(true);
  const containerRef = useRef<HTMLDivElement>(null);

  // Initial load of the most recent notifications.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await getNotifications({ size: RECENT_LIMIT, page: 1 });
        if (!cancelled) setItems(res.data);
      } catch {
        // Non-fatal — the bell just starts empty until a live event arrives.
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Live updates: prepend new notifications and bump the unread badge.
  const handleCreated = useCallback((event: { notification: Notification }) => {
    setItems((prev) => [event.notification, ...prev].slice(0, RECENT_LIMIT));
    setUnread((n) => n + 1);
  }, []);

  const handleStatusChanged = useCallback(
    (event: { notification_id: string; delivery_status: string }) => {
      setItems((prev) =>
        prev.map((n) =>
          n.notification_id === event.notification_id
            ? {
                ...n,
                delivery_status: event.delivery_status as DeliveryStatus,
              }
            : n,
        ),
      );
    },
    [],
  );

  useNotificationWebSocket({
    autoConnect: true,
    onNotificationCreated: handleCreated,
    onStatusChanged: handleStatusChanged,
  });

  // Close on outside click / Escape.
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const toggle = () => {
    setOpen((prev) => {
      const next = !prev;
      // Opening the panel clears the "new since last look" badge.
      if (next) setUnread(0);
      return next;
    });
  };

  const goToAll = () => {
    setOpen(false);
    router.push("/dashboard/notifications");
  };

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={toggle}
        aria-label={
          unread > 0 ? `Notifications, ${unread} new` : "Notifications"
        }
        aria-haspopup="true"
        aria-expanded={open}
        className="relative flex items-center justify-center w-9 h-9 rounded-lg transition-colors hover:bg-[color-mix(in_srgb,var(--color-primary)_8%,transparent)] focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:ring-[color:var(--color-primary)]"
        style={{ color: "var(--color-primary)" }}
      >
        <Bell className="w-5 h-5" />
        {unread > 0 && (
          <span
            className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] px-1 flex items-center justify-center rounded-full text-[10px] font-semibold text-white"
            style={{ backgroundColor: "var(--color-error)" }}
          >
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </button>

      {open && (
        <div
          role="menu"
          className="absolute right-0 mt-2 w-80 sm:w-96 max-h-[28rem] flex flex-col rounded-xl border bg-[color:var(--color-surface)] shadow-lg z-50"
          style={{
            borderColor:
              "color-mix(in srgb, var(--color-primary) 12%, transparent)",
          }}
        >
          <div
            className="flex items-center justify-between px-4 py-3 border-b"
            style={{
              borderColor:
                "color-mix(in srgb, var(--color-primary) 10%, transparent)",
            }}
          >
            <span
              className="text-sm font-semibold"
              style={{ color: "var(--color-primary)" }}
            >
              Notifications
            </span>
            <button
              type="button"
              onClick={goToAll}
              className="text-xs font-medium underline hover:no-underline focus:outline-none"
              style={{ color: "var(--color-primary)" }}
            >
              View all
            </button>
          </div>

          <div className="flex-1 overflow-y-auto">
            {loading ? (
              <div className="px-4 py-8 text-center text-sm text-gray-500">
                Loading…
              </div>
            ) : items.length === 0 ? (
              <div className="px-4 py-10 text-center">
                <Bell className="w-8 h-8 mx-auto mb-2 text-gray-300" />
                <p className="text-sm text-gray-500">No notifications yet</p>
              </div>
            ) : (
              <ul className="divide-y divide-gray-100">
                {items.map((n) => (
                  <li key={n.notification_id}>
                    <button
                      type="button"
                      onClick={goToAll}
                      className="w-full text-left px-4 py-3 transition-colors hover:bg-[color-mix(in_srgb,var(--color-primary)_5%,transparent)] focus:outline-none"
                    >
                      <div className="flex items-start gap-2">
                        <span className="mt-0.5">
                          {getStatusIcon(n.delivery_status)}
                        </span>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-sm font-medium text-primary truncate">
                              {typeLabel(n.notification_type)}
                            </span>
                            <span className="text-[11px] text-gray-400 flex-shrink-0">
                              {relativeTime(n.created_at)}
                            </span>
                          </div>
                          <div className="text-xs text-gray-600 truncate">
                            {n.subject || n.message_body}
                          </div>
                          <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-gray-500">
                            {getChannelIcon(n.channel)}
                            <span className="truncate">
                              {n.recipient_name || n.recipient_reference}
                            </span>
                          </div>
                        </div>
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
