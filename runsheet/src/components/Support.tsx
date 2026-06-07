import { Bell, Filter, MessageSquare, Plus, Settings, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { apiService, type SupportTicket } from "../services/api";
import LoadingSpinner from "./LoadingSpinner";
import NotificationHistoryTab from "./NotificationHistoryTab";
import NotificationSettingsTab from "./NotificationSettingsTab";
import {
  Badge,
  type BadgeVariant,
  Button,
  EmptyState,
  FilterBar,
  PageHeader,
  Pagination,
  StatsBar,
  type Tab,
  Table,
  TabNavigation,
} from "./ui";

type SupportTab = "tickets" | "notifications" | "settings";

const TABS: Tab[] = [
  {
    id: "tickets",
    label: "Tickets",
    icon: <MessageSquare className="w-4 h-4" />,
  },
  {
    id: "notifications",
    label: "Notifications",
    icon: <Bell className="w-4 h-4" />,
  },
  { id: "settings", label: "Settings", icon: <Settings className="w-4 h-4" /> },
];

const TICKET_STATUSES: { value: string; label: string }[] = [
  { value: "all", label: "All Status" },
  { value: "open", label: "Open" },
  { value: "in_progress", label: "In Progress" },
  { value: "resolved", label: "Resolved" },
  { value: "closed", label: "Closed" },
];

const TICKET_PRIORITIES: { value: string; label: string }[] = [
  { value: "all", label: "All Priorities" },
  { value: "urgent", label: "Urgent" },
  { value: "high", label: "High" },
  { value: "medium", label: "Medium" },
  { value: "low", label: "Low" },
];

/**
 * Valid status transitions for support tickets.
 * open → in_progress, resolved, closed
 * in_progress → resolved, closed
 * resolved → closed
 * closed → (none)
 */
const STATUS_TRANSITIONS: Record<string, string[]> = {
  open: ["in_progress", "resolved", "closed"],
  in_progress: ["resolved", "closed"],
  resolved: ["closed"],
  closed: [],
};

/**
 * Support — full support ticket management page.
 *
 * Summary bar, data table, search, filters, create modal, inline status updates,
 * detail panel with status actions.
 *
 * Validates: Requirements 6.1–6.6, 12.1–12.4
 */
export default function Support() {
  const [activeTab, setActiveTab] = useState<SupportTab>("tickets");
  const [tickets, setTickets] = useState<SupportTicket[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  const [filterPriority, setFilterPriority] = useState("all");
  const [filterStatus, setFilterStatus] = useState("all");
  const [selectedTicket, setSelectedTicket] = useState<SupportTicket | null>(
    null,
  );
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [actionError, setActionError] = useState<Record<string, string>>({});
  const [page, setPage] = useState(1);

  const loadSupportData = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await apiService.getSupportTickets();
      setTickets(response.data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load support tickets",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSupportData();
  }, [loadSupportData]);

  const handleStatusUpdate = async (ticketId: string, newStatus: string) => {
    // Clear previous error for this ticket
    setActionError((prev) => {
      const next = { ...prev };
      delete next[ticketId];
      return next;
    });
    try {
      const response = await apiService.updateSupportTicket(ticketId, {
        status: newStatus as SupportTicket["status"],
      });
      const updatedTicket = response.data;
      setTickets((prev) =>
        prev.map((t) => (t.id === ticketId ? updatedTicket : t)),
      );
      // Also update the detail panel if this ticket is selected
      if (selectedTicket?.id === ticketId) {
        setSelectedTicket(updatedTicket);
      }
    } catch (err) {
      setActionError((prev) => ({
        ...prev,
        [ticketId]:
          err instanceof Error ? err.message : "Failed to update status",
      }));
    }
  };

  const getPriorityVariant = (priority: string): BadgeVariant => {
    switch (priority) {
      case "urgent":
        return "error";
      case "high":
        return "warning";
      case "medium":
        return "info";
      case "low":
        return "neutral";
      default:
        return "neutral";
    }
  };

  const getStatusVariant = (status: string): BadgeVariant => {
    switch (status) {
      case "open":
        return "error";
      case "in_progress":
        return "info";
      case "resolved":
        return "success";
      case "closed":
        return "neutral";
      default:
        return "neutral";
    }
  };

  const getStatusText = (status: string) => {
    return status
      .replace("_", " ")
      .split(" ")
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join(" ");
  };

  const filteredTickets = tickets.filter((ticket) => {
    const matchesSearch =
      ticket.customer.toLowerCase().includes(searchTerm.toLowerCase()) ||
      ticket.issue.toLowerCase().includes(searchTerm.toLowerCase()) ||
      ticket.id.toLowerCase().includes(searchTerm.toLowerCase());
    const matchesPriority =
      filterPriority === "all" || ticket.priority === filterPriority;
    const matchesStatus =
      filterStatus === "all" || ticket.status === filterStatus;
    return matchesSearch && matchesPriority && matchesStatus;
  });

  const PAGE_SIZE = 20;
  const totalPages = Math.max(1, Math.ceil(filteredTickets.length / PAGE_SIZE));
  const paginatedTickets = filteredTickets.slice(
    (page - 1) * PAGE_SIZE,
    page * PAGE_SIZE,
  );
  const ticketColumns = [
    {
      key: "ticket",
      label: "Ticket",
      render: (ticket: SupportTicket) => (
        <div>
          <div className="font-medium text-primary">{ticket.id}</div>
          {ticket.relatedOrder && (
            <div className="text-sm text-gray-500">
              Order: {ticket.relatedOrder}
            </div>
          )}
        </div>
      ),
    },
    {
      key: "customer",
      label: "Customer",
      render: (ticket: SupportTicket) => (
        <span className="text-sm text-primary">{ticket.customer}</span>
      ),
    },
    {
      key: "issue",
      label: "Issue",
      render: (ticket: SupportTicket) => (
        <div>
          <div className="text-sm text-primary">{ticket.issue}</div>
          <div className="text-sm text-gray-500 line-clamp-1">
            {ticket.description}
          </div>
        </div>
      ),
    },
    {
      key: "priority",
      label: "Priority",
      render: (ticket: SupportTicket) => (
        <Badge variant={getPriorityVariant(ticket.priority)} size="sm">
          {ticket.priority.charAt(0).toUpperCase() + ticket.priority.slice(1)}
        </Badge>
      ),
    },
    {
      key: "status",
      label: "Status",
      render: (ticket: SupportTicket) => (
        <Badge variant={getStatusVariant(ticket.status)} size="sm">
          {getStatusText(ticket.status)}
        </Badge>
      ),
    },
    {
      key: "assigned",
      label: "Assigned",
      render: (ticket: SupportTicket) => (
        <span className="text-sm text-gray-700">
          {ticket.assignedTo || (
            <span className="text-gray-500">Unassigned</span>
          )}
        </span>
      ),
    },
    {
      key: "created",
      label: "Created",
      render: (ticket: SupportTicket) => (
        <span className="text-sm text-gray-600">
          {new Date(ticket.createdAt).toLocaleDateString("en-US", {
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
          })}
        </span>
      ),
    },
    {
      key: "actions",
      label: "Actions",
      render: (ticket: SupportTicket) => (
        <div
          onClick={(e) => e.stopPropagation()}
          className="flex flex-col gap-1"
        >
          {(STATUS_TRANSITIONS[ticket.status] || []).length > 0 ? (
            <div className="flex gap-1 flex-wrap">
              {STATUS_TRANSITIONS[ticket.status].map((targetStatus) => (
                <Button
                  key={targetStatus}
                  type="button"
                  variant="secondary"
                  size="sm"
                  className="whitespace-nowrap"
                  onClick={() => handleStatusUpdate(ticket.id, targetStatus)}
                >
                  {getStatusText(targetStatus)}
                </Button>
              ))}
            </div>
          ) : (
            <span className="text-xs text-gray-500">-</span>
          )}
          {actionError[ticket.id] && (
            <p className="text-xs text-error">{actionError[ticket.id]}</p>
          )}
        </div>
      ),
    },
  ];

  if (loading && activeTab === "tickets") {
    return <LoadingSpinner message="Loading support tickets..." />;
  }

  if (error && tickets.length === 0 && activeTab === "tickets") {
    return (
      <div className="h-full flex items-center justify-center bg-white">
        <div className="text-center">
          <div className="bg-error-light text-error-dark px-6 py-4 rounded-xl mb-4 max-w-md">
            <p className="text-sm font-medium">
              Failed to load support tickets
            </p>
            <p className="text-sm mt-1">{error}</p>
          </div>
          <Button onClick={loadSupportData}>Retry</Button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Tab Navigation */}
      <TabNavigation
        tabs={TABS}
        activeTab={activeTab}
        onChange={(id) => setActiveTab(id as SupportTab)}
      />

      {/* Tab Content */}
      {activeTab === "tickets" && (
        <div className="flex-1 flex bg-white overflow-hidden">
          {/* Tickets List */}
          <div className="flex-1 flex flex-col">
            {/* Header */}
            <PageHeader
              title="Support Tickets"
              subtitle="Manage customer support requests"
              icon={<MessageSquare className="w-5 h-5" />}
              actions={
                <Button
                  variant="primary"
                  size="md"
                  icon={<Plus className="w-4 h-4" />}
                  onClick={() => setShowCreateModal(true)}
                >
                  Create Ticket
                </Button>
              }
            />

            {/* Search and Filters */}
            <FilterBar
              searchPlaceholder="Search tickets, customers, issues..."
              searchValue={searchTerm}
              onSearchChange={(value) => {
                setSearchTerm(value);
                setPage(1);
              }}
              filters={
                <>
                  <div className="relative">
                    <Filter className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-500" />
                    <select
                      value={filterPriority}
                      onChange={(e) => {
                        setFilterPriority(e.target.value);
                        setPage(1);
                      }}
                      className="pl-10 pr-8 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 focus:outline-none bg-white min-w-[140px]"
                      aria-label="Priority"
                    >
                      {TICKET_PRIORITIES.map((opt) => (
                        <option key={opt.value} value={opt.value}>
                          {opt.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <select
                    value={filterStatus}
                    onChange={(e) => {
                      setFilterStatus(e.target.value);
                      setPage(1);
                    }}
                    className="px-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 focus:outline-none bg-white min-w-[140px]"
                    aria-label="Status"
                  >
                    {TICKET_STATUSES.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                </>
              }
            />

            {/* Stats */}
            <StatsBar
              variant="grid"
              stats={[
                { label: "Total Tickets", value: tickets.length },
                {
                  label: "Open",
                  value: tickets.filter((t) => t.status === "open").length,
                  color: "error",
                },
                {
                  label: "In Progress",
                  value: tickets.filter((t) => t.status === "in_progress")
                    .length,
                  color: "info",
                },
                {
                  label: "Urgent",
                  value: tickets.filter((t) => t.priority === "urgent").length,
                  color: "warning",
                },
              ]}
            />

            <div className="flex-1 overflow-y-auto">
              <Table
                columns={ticketColumns}
                data={paginatedTickets}
                getRowId={(ticket) => ticket.id}
                selectedId={selectedTicket?.id}
                onRowClick={setSelectedTicket}
                emptyState={
                  <EmptyState
                    icon={<MessageSquare />}
                    title="No support tickets found"
                    description="Try adjusting your search or filter criteria"
                  />
                }
              />
              <Pagination
                currentPage={page}
                totalPages={totalPages}
                totalItems={filteredTickets.length}
                onPageChange={setPage}
              />
            </div>
          </div>

          {/* Ticket Details Panel */}
          {selectedTicket && (
            <div className="w-96 border-l border-gray-100 bg-gray-50 flex flex-col">
              <div className="px-6 py-4 border-b border-gray-100">
                <div className="flex items-center justify-between">
                  <h3 className="font-semibold text-primary">Ticket Details</h3>
                  <button
                    onClick={() => setSelectedTicket(null)}
                    className="text-gray-500 hover:text-primary p-2 rounded-lg hover:bg-white transition-colors"
                  >
                    <X className="w-5 h-5" />
                  </button>
                </div>
              </div>

              <div className="flex-1 overflow-y-auto p-6 space-y-6">
                <div>
                  <label className="block text-sm font-medium text-gray-500 mb-2">
                    Ticket ID
                  </label>
                  <p className="text-sm text-primary font-medium">
                    {selectedTicket.id}
                  </p>
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-500 mb-2">
                    Customer
                  </label>
                  <p className="text-sm text-primary">
                    {selectedTicket.customer}
                  </p>
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-500 mb-2">
                    Issue
                  </label>
                  <p className="text-sm text-primary">{selectedTicket.issue}</p>
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-500 mb-2">
                    Description
                  </label>
                  <p className="text-sm text-primary leading-relaxed">
                    {selectedTicket.description}
                  </p>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium text-gray-500 mb-2">
                      Priority
                    </label>
                    <Badge
                      variant={getPriorityVariant(selectedTicket.priority)}
                      size="sm"
                    >
                      {selectedTicket.priority.charAt(0).toUpperCase() +
                        selectedTicket.priority.slice(1)}
                    </Badge>
                  </div>

                  <div>
                    <label className="block text-sm font-medium text-gray-500 mb-2">
                      Status
                    </label>
                    <Badge
                      variant={getStatusVariant(selectedTicket.status)}
                      size="sm"
                    >
                      {getStatusText(selectedTicket.status)}
                    </Badge>
                  </div>
                </div>

                {selectedTicket.assignedTo && (
                  <div>
                    <label className="block text-sm font-medium text-gray-500 mb-2">
                      Assigned To
                    </label>
                    <p className="text-sm text-primary">
                      {selectedTicket.assignedTo}
                    </p>
                  </div>
                )}

                {selectedTicket.relatedOrder && (
                  <div>
                    <label className="block text-sm font-medium text-gray-500 mb-2">
                      Related Order
                    </label>
                    <p className="text-sm text-primary hover:text-gray-600 cursor-pointer font-medium">
                      {selectedTicket.relatedOrder}
                    </p>
                  </div>
                )}

                <div>
                  <label className="block text-sm font-medium text-gray-500 mb-2">
                    Created
                  </label>
                  <p className="text-sm text-primary">
                    {new Date(selectedTicket.createdAt).toLocaleString(
                      "en-US",
                      {
                        month: "short",
                        day: "numeric",
                        year: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                      },
                    )}
                  </p>
                </div>

                {/* Status Update Actions in Detail Panel */}
                {(STATUS_TRANSITIONS[selectedTicket.status] || []).length >
                  0 && (
                  <div>
                    <label className="block text-sm font-medium text-gray-500 mb-2">
                      Update Status
                    </label>
                    <div className="flex gap-2 flex-wrap">
                      {STATUS_TRANSITIONS[selectedTicket.status].map(
                        (targetStatus) => (
                          <Button
                            key={targetStatus}
                            type="button"
                            variant="secondary"
                            size="sm"
                            onClick={() =>
                              handleStatusUpdate(
                                selectedTicket.id,
                                targetStatus,
                              )
                            }
                            className="whitespace-nowrap"
                          >
                            {getStatusText(targetStatus)}
                          </Button>
                        ),
                      )}
                    </div>
                    {actionError[selectedTicket.id] && (
                      <p className="text-xs text-error mt-2">
                        {actionError[selectedTicket.id]}
                      </p>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Create Ticket Modal */}
          {showCreateModal && (
            <CreateTicketModal
              onClose={() => setShowCreateModal(false)}
              onCreated={(ticket) => setTickets((prev) => [ticket, ...prev])}
            />
          )}
        </div>
      )}

      {activeTab === "notifications" && <NotificationHistoryTab />}

      {activeTab === "settings" && <NotificationSettingsTab />}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Create Ticket Modal                                                 */
/* ------------------------------------------------------------------ */

interface CreateTicketModalProps {
  onClose: () => void;
  onCreated: (ticket: SupportTicket) => void;
}

function CreateTicketModal({ onClose, onCreated }: CreateTicketModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState({
    customer: "",
    issue: "",
    description: "",
    priority: "medium" as SupportTicket["priority"],
    relatedOrder: "",
  });

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (
      !form.customer.trim() ||
      !form.issue.trim() ||
      !form.description.trim()
    ) {
      setError("Customer, issue, and description are required.");
      return;
    }
    setError("");
    setSubmitting(true);
    try {
      const response = await apiService.createSupportTicket({
        customer: form.customer.trim(),
        issue: form.issue.trim(),
        description: form.description.trim(),
        priority: form.priority,
        status: "open",
        relatedOrder: form.relatedOrder.trim() || undefined,
      });
      onCreated(response.data);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create ticket");
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-primary">
            Create Support Ticket
          </h2>
          <button
            onClick={onClose}
            className="p-1 text-gray-500 hover:text-gray-600 rounded"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
          {error && (
            <p className="text-sm text-error bg-error-light px-3 py-2 rounded-lg">
              {error}
            </p>
          )}

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Customer
            </label>
            <input
              type="text"
              value={form.customer}
              onChange={(e) => setForm({ ...form, customer: e.target.value })}
              placeholder="e.g. Acme Corp"
              className={inputClass}
              required
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Issue
            </label>
            <input
              type="text"
              value={form.issue}
              onChange={(e) => setForm({ ...form, issue: e.target.value })}
              placeholder="e.g. Delivery delay"
              className={inputClass}
              required
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Description
            </label>
            <textarea
              value={form.description}
              onChange={(e) =>
                setForm({ ...form, description: e.target.value })
              }
              placeholder="Describe the issue in detail..."
              rows={3}
              className={inputClass}
              required
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Priority
              </label>
              <select
                value={form.priority}
                onChange={(e) =>
                  setForm({
                    ...form,
                    priority: e.target.value as SupportTicket["priority"],
                  })
                }
                className={inputClass}
              >
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
                <option value="urgent">Urgent</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Related Order (optional)
              </label>
              <input
                type="text"
                value={form.relatedOrder}
                onChange={(e) =>
                  setForm({ ...form, relatedOrder: e.target.value })
                }
                placeholder="e.g. ORD-001"
                className={inputClass}
              />
            </div>
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <Button type="button" onClick={onClose} variant="ghost">
              Cancel
            </Button>
            <Button type="submit" disabled={submitting} loading={submitting}>
              {submitting ? "Creating..." : "Create Ticket"}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
