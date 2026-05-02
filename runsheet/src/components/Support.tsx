import { Filter, MessageSquare, Plus, Search, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { apiService, type SupportTicket } from "../services/api";
import LoadingSpinner from "./LoadingSpinner";

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

  const getPriorityColor = (priority: string) => {
    switch (priority) {
      case "urgent":
        return "text-red-700 bg-red-50";
      case "high":
        return "text-orange-700 bg-orange-50";
      case "medium":
        return "text-yellow-700 bg-yellow-50";
      case "low":
        return "text-gray-700 bg-gray-50";
      default:
        return "text-gray-700 bg-gray-50";
    }
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case "open":
        return "text-red-700 bg-red-50";
      case "in_progress":
        return "text-blue-700 bg-blue-50";
      case "resolved":
        return "text-green-700 bg-green-50";
      case "closed":
        return "text-gray-700 bg-gray-50";
      default:
        return "text-gray-700 bg-gray-50";
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

  if (loading) {
    return <LoadingSpinner message="Loading support tickets..." />;
  }

  if (error && tickets.length === 0) {
    return (
      <div className="h-full flex items-center justify-center bg-white">
        <div className="text-center">
          <div className="bg-red-50 text-red-700 px-6 py-4 rounded-xl mb-4 max-w-md">
            <p className="text-sm font-medium">Failed to load support tickets</p>
            <p className="text-sm mt-1">{error}</p>
          </div>
          <button
            onClick={loadSupportData}
            className="px-4 py-2 text-sm text-white rounded-lg hover:opacity-90"
            style={{ backgroundColor: "#232323" }}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex bg-white">
      {/* Tickets List */}
      <div className="flex-1 flex flex-col">
        {/* Header */}
        <div className="border-b border-gray-100 px-8 py-6">
          <div className="flex items-center justify-between mb-6">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 bg-[#232323] rounded-xl flex items-center justify-center">
                <MessageSquare className="w-5 h-5 text-white" />
              </div>
              <div>
                <h1 className="text-2xl font-semibold text-[#232323]">
                  Support Tickets
                </h1>
                <p className="text-gray-500">
                  Manage customer support requests
                </p>
              </div>
            </div>
            <button
              onClick={() => setShowCreateModal(true)}
              className="flex items-center gap-2 px-4 py-2 text-sm text-white rounded-lg transition-colors hover:opacity-90"
              style={{ backgroundColor: "#232323" }}
            >
              <Plus className="w-4 h-4" />
              Create Ticket
            </button>
          </div>

          {/* Search and Filters */}
          <div className="flex gap-4">
            <div className="flex-1 relative">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-400" />
              <input
                type="text"
                placeholder="Search tickets, customers, issues..."
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                className="w-full pl-10 pr-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
              />
            </div>
            <div className="relative">
              <Filter className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-400" />
              <select
                value={filterPriority}
                onChange={(e) => setFilterPriority(e.target.value)}
                className="pl-10 pr-8 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white min-w-[140px]"
              >
                {TICKET_PRIORITIES.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
            </div>
            <select
              value={filterStatus}
              onChange={(e) => setFilterStatus(e.target.value)}
              className="px-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white min-w-[120px]"
            >
              {TICKET_STATUSES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* Stats */}
        <div className="border-b border-gray-100 px-8 py-4">
          <div className="grid grid-cols-4 gap-6">
            <div className="text-center">
              <div className="text-2xl font-semibold text-[#232323]">
                {tickets.length}
              </div>
              <div className="text-sm text-gray-500">Total Tickets</div>
            </div>
            <div className="text-center">
              <div className="text-2xl font-semibold text-red-600">
                {tickets.filter((t) => t.status === "open").length}
              </div>
              <div className="text-sm text-gray-500">Open</div>
            </div>
            <div className="text-center">
              <div className="text-2xl font-semibold text-blue-600">
                {tickets.filter((t) => t.status === "in_progress").length}
              </div>
              <div className="text-sm text-gray-500">In Progress</div>
            </div>
            <div className="text-center">
              <div className="text-2xl font-semibold text-orange-600">
                {tickets.filter((t) => t.priority === "urgent").length}
              </div>
              <div className="text-sm text-gray-500">Urgent</div>
            </div>
          </div>
        </div>

        {/* Table View */}
        <div className="flex-1 overflow-y-auto">
          <table className="w-full">
            <thead className="bg-gray-50 sticky top-0 border-b border-gray-100">
              <tr>
                <th className="px-8 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Ticket
                </th>
                <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Customer
                </th>
                <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Issue
                </th>
                <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Priority
                </th>
                <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Status
                </th>
                <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Assigned
                </th>
                <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Created
                </th>
                <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {filteredTickets.map((ticket) => (
                <tr
                  key={ticket.id}
                  className={`cursor-pointer transition-colors ${
                    selectedTicket?.id === ticket.id
                      ? "bg-gray-50"
                      : "hover:bg-gray-50"
                  }`}
                  onClick={() => setSelectedTicket(ticket)}
                >
                  <td className="px-8 py-4">
                    <div className="font-medium text-[#232323]">
                      {ticket.id}
                    </div>
                    {ticket.relatedOrder && (
                      <div className="text-sm text-gray-500">
                        Order: {ticket.relatedOrder}
                      </div>
                    )}
                  </td>
                  <td className="px-6 py-4">
                    <span className="text-sm text-[#232323]">
                      {ticket.customer}
                    </span>
                  </td>
                  <td className="px-6 py-4">
                    <div className="text-sm text-[#232323]">{ticket.issue}</div>
                    <div className="text-sm text-gray-500 line-clamp-1">
                      {ticket.description}
                    </div>
                  </td>
                  <td className="px-6 py-4">
                    <span
                      className={`inline-flex items-center px-3 py-1 rounded-lg text-xs font-medium ${getPriorityColor(ticket.priority)}`}
                    >
                      {ticket.priority.charAt(0).toUpperCase() +
                        ticket.priority.slice(1)}
                    </span>
                  </td>
                  <td className="px-6 py-4">
                    <span
                      className={`inline-flex items-center px-3 py-1 rounded-lg text-xs font-medium ${getStatusColor(ticket.status)}`}
                    >
                      {getStatusText(ticket.status)}
                    </span>
                  </td>
                  <td className="px-6 py-4">
                    <span className="text-sm text-gray-700">
                      {ticket.assignedTo || (
                        <span className="text-gray-400">Unassigned</span>
                      )}
                    </span>
                  </td>
                  <td className="px-6 py-4">
                    <span className="text-sm text-gray-600">
                      {new Date(ticket.createdAt).toLocaleDateString("en-US", {
                        month: "short",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </span>
                  </td>
                  <td className="px-6 py-4" onClick={(e) => e.stopPropagation()}>
                    <div className="flex flex-col gap-1">
                      {(STATUS_TRANSITIONS[ticket.status] || []).length > 0 ? (
                        <div className="flex gap-1 flex-wrap">
                          {STATUS_TRANSITIONS[ticket.status].map(
                            (targetStatus) => (
                              <button
                                key={targetStatus}
                                onClick={() =>
                                  handleStatusUpdate(ticket.id, targetStatus)
                                }
                                className="px-2 py-1 text-xs rounded-md border border-gray-200 text-gray-600 hover:bg-gray-100 transition-colors whitespace-nowrap"
                              >
                                {getStatusText(targetStatus)}
                              </button>
                            ),
                          )}
                        </div>
                      ) : (
                        <span className="text-xs text-gray-400">—</span>
                      )}
                      {actionError[ticket.id] && (
                        <p className="text-xs text-red-600">
                          {actionError[ticket.id]}
                        </p>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {filteredTickets.length === 0 && (
            <div className="text-center py-16 text-gray-500">
              <MessageSquare className="w-16 h-16 mx-auto mb-4 text-gray-300" />
              <p className="text-lg font-medium text-gray-400">
                No support tickets found
              </p>
              <p className="text-sm text-gray-400 mt-1">
                Try adjusting your search or filter criteria
              </p>
            </div>
          )}
        </div>
      </div>

      {/* Ticket Details Panel */}
      {selectedTicket && (
        <div className="w-96 border-l border-gray-100 bg-gray-50 flex flex-col">
          <div className="px-6 py-4 border-b border-gray-100">
            <div className="flex items-center justify-between">
              <h3 className="font-semibold text-[#232323]">Ticket Details</h3>
              <button
                onClick={() => setSelectedTicket(null)}
                className="text-gray-400 hover:text-[#232323] p-2 rounded-lg hover:bg-white transition-colors"
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
              <p className="text-sm text-[#232323] font-medium">
                {selectedTicket.id}
              </p>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-500 mb-2">
                Customer
              </label>
              <p className="text-sm text-[#232323]">
                {selectedTicket.customer}
              </p>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-500 mb-2">
                Issue
              </label>
              <p className="text-sm text-[#232323]">{selectedTicket.issue}</p>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-500 mb-2">
                Description
              </label>
              <p className="text-sm text-[#232323] leading-relaxed">
                {selectedTicket.description}
              </p>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-500 mb-2">
                  Priority
                </label>
                <span
                  className={`inline-block px-3 py-1 rounded-lg text-xs font-medium ${getPriorityColor(selectedTicket.priority)}`}
                >
                  {selectedTicket.priority.charAt(0).toUpperCase() +
                    selectedTicket.priority.slice(1)}
                </span>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-500 mb-2">
                  Status
                </label>
                <span
                  className={`inline-block px-3 py-1 rounded-lg text-xs font-medium ${getStatusColor(selectedTicket.status)}`}
                >
                  {getStatusText(selectedTicket.status)}
                </span>
              </div>
            </div>

            {selectedTicket.assignedTo && (
              <div>
                <label className="block text-sm font-medium text-gray-500 mb-2">
                  Assigned To
                </label>
                <p className="text-sm text-[#232323]">
                  {selectedTicket.assignedTo}
                </p>
              </div>
            )}

            {selectedTicket.relatedOrder && (
              <div>
                <label className="block text-sm font-medium text-gray-500 mb-2">
                  Related Order
                </label>
                <p className="text-sm text-[#232323] hover:text-gray-600 cursor-pointer font-medium">
                  {selectedTicket.relatedOrder}
                </p>
              </div>
            )}

            <div>
              <label className="block text-sm font-medium text-gray-500 mb-2">
                Created
              </label>
              <p className="text-sm text-[#232323]">
                {new Date(selectedTicket.createdAt).toLocaleString("en-US", {
                  month: "short",
                  day: "numeric",
                  year: "numeric",
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </p>
            </div>

            {/* Status Update Actions in Detail Panel */}
            {(STATUS_TRANSITIONS[selectedTicket.status] || []).length > 0 && (
              <div>
                <label className="block text-sm font-medium text-gray-500 mb-2">
                  Update Status
                </label>
                <div className="flex gap-2 flex-wrap">
                  {STATUS_TRANSITIONS[selectedTicket.status].map(
                    (targetStatus) => (
                      <button
                        key={targetStatus}
                        onClick={() =>
                          handleStatusUpdate(selectedTicket.id, targetStatus)
                        }
                        className="px-3 py-1.5 text-xs rounded-lg border border-gray-200 text-gray-600 hover:bg-white transition-colors whitespace-nowrap"
                      >
                        {getStatusText(targetStatus)}
                      </button>
                    ),
                  )}
                </div>
                {actionError[selectedTicket.id] && (
                  <p className="text-xs text-red-600 mt-2">
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
    if (!form.customer.trim() || !form.issue.trim() || !form.description.trim()) {
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
      setError(
        err instanceof Error ? err.message : "Failed to create ticket",
      );
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
          <h2 className="text-lg font-semibold text-[#232323]">
            Create Support Ticket
          </h2>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
          {error && (
            <p className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg">
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
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50"
              style={{ backgroundColor: "#232323" }}
            >
              {submitting ? "Creating..." : "Create Ticket"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
