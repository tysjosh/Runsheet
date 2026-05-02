import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Database,
  FileSpreadsheet,
  Filter,
  Loader2,
  X,
  XCircle,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { DataType, ImportSessionRecord, ImportStatus } from "../../types/import";
import { importApi } from "../../services/importApi";

// ─── Props ───────────────────────────────────────────────────────────────────

interface ImportHistoryProps {
  onClose: () => void;
}

// ─── Constants ───────────────────────────────────────────────────────────────

const DATA_TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "All Data Types" },
  { value: "fleet", label: "Fleet" },
  { value: "orders", label: "Orders" },
  { value: "riders", label: "Riders" },
  { value: "fuel_stations", label: "Fuel Stations" },
  { value: "inventory", label: "Inventory" },
  { value: "support_tickets", label: "Support Tickets" },
  { value: "jobs", label: "Jobs / Scheduling" },
];

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "All Statuses" },
  { value: "completed", label: "Completed" },
  { value: "partial", label: "Partial" },
  { value: "failed", label: "Failed" },
];

const DATA_TYPE_LABELS: Record<string, string> = {
  fleet: "Fleet",
  orders: "Orders",
  riders: "Riders",
  fuel_stations: "Fuel Stations",
  inventory: "Inventory",
  support_tickets: "Support Tickets",
  jobs: "Jobs / Scheduling",
};

const SOURCE_TYPE_LABELS: Record<string, string> = {
  csv: "CSV",
  google_sheets: "Google Sheets",
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

function formatDate(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function formatDuration(seconds?: number): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = (seconds % 60).toFixed(0);
  return `${mins}m ${secs}s`;
}

// ─── Status Badge ────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: ImportStatus | string }) {
  const config: Record<string, { bg: string; text: string; icon: React.ReactNode }> = {
    completed: {
      bg: "bg-green-50",
      text: "text-green-700",
      icon: <CheckCircle2 className="w-3.5 h-3.5" />,
    },
    partial: {
      bg: "bg-amber-50",
      text: "text-amber-700",
      icon: <AlertTriangle className="w-3.5 h-3.5" />,
    },
    failed: {
      bg: "bg-red-50",
      text: "text-red-700",
      icon: <XCircle className="w-3.5 h-3.5" />,
    },
  };

  const c = config[status] ?? {
    bg: "bg-gray-50",
    text: "text-gray-600",
    icon: <Clock className="w-3.5 h-3.5" />,
  };

  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${c.bg} ${c.text}`}
    >
      {c.icon}
      {status.charAt(0).toUpperCase() + status.slice(1)}
    </span>
  );
}

// ─── Expanded Row Detail ─────────────────────────────────────────────────────

function SessionDetail({ session }: { session: ImportSessionRecord }) {
  return (
    <div className="px-6 py-4 bg-gray-50 border-t border-gray-100">
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-4">
        <DetailItem label="Session ID" value={session.session_id} mono />
        <DetailItem
          label="Source Type"
          value={SOURCE_TYPE_LABELS[session.source_type] ?? session.source_type}
        />
        <DetailItem label="Source Name" value={session.source_name} />
        <DetailItem
          label="Duration"
          value={formatDuration(session.duration_seconds)}
        />
        <DetailItem
          label="Total Records"
          value={String(session.total_records)}
        />
        <DetailItem
          label="Imported"
          value={String(session.imported_records)}
        />
        <DetailItem
          label="Skipped"
          value={String(session.skipped_records)}
        />
        <DetailItem
          label="Errors"
          value={String(session.error_count)}
        />
        <DetailItem
          label="Started"
          value={formatDate(session.created_at)}
        />
        <DetailItem
          label="Completed"
          value={session.completed_at ? formatDate(session.completed_at) : "—"}
        />
      </div>

      {/* Error messages */}
      {session.errors.length > 0 && (
        <div className="mt-3">
          <h4 className="text-xs font-medium text-red-600 uppercase tracking-wider mb-2">
            Error Messages
          </h4>
          <div className="rounded-lg border border-red-200 bg-red-50 p-3 max-h-40 overflow-y-auto">
            <ul className="space-y-1">
              {session.errors.map((err, idx) => (
                <li
                  key={idx}
                  className="text-xs text-red-700 flex items-start gap-2"
                >
                  <XCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                  <span>{err}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}

function DetailItem({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <p className="text-xs text-gray-400 mb-0.5">{label}</p>
      <p
        className={`text-sm text-[#232323] ${mono ? "font-mono text-xs" : ""} truncate`}
        title={value}
      >
        {value}
      </p>
    </div>
  );
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function ImportHistory({ onClose }: ImportHistoryProps) {
  const [sessions, setSessions] = useState<ImportSessionRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Filter state
  const [dataTypeFilter, setDataTypeFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");

  // ── Fetch history ────────────────────────────────────────────────────

  const fetchHistory = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const filters: { dataType?: string; status?: string } = {};
      if (dataTypeFilter) filters.dataType = dataTypeFilter;
      if (statusFilter) filters.status = statusFilter;

      const data = await importApi.getHistory(filters);
      setSessions(data);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Failed to load import history.",
      );
    } finally {
      setLoading(false);
    }
  }, [dataTypeFilter, statusFilter]);

  useEffect(() => {
    fetchHistory();
  }, [fetchHistory]);

  // ── Toggle row expansion ─────────────────────────────────────────────

  const toggleExpand = (sessionId: string) => {
    setExpandedId((prev) => (prev === sessionId ? null : sessionId));
  };

  // ── Loading state ────────────────────────────────────────────────────

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-gray-400">
        <Loader2 className="w-8 h-8 animate-spin mb-4" />
        <p className="text-sm font-medium">Loading import history…</p>
      </div>
    );
  }

  // ── Error state ──────────────────────────────────────────────────────

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-20">
        <XCircle className="w-10 h-10 text-red-500 mb-4" />
        <p className="text-sm font-medium text-red-600 mb-2">
          Failed to Load History
        </p>
        <p className="text-xs text-gray-500 mb-6 max-w-md text-center">
          {error}
        </p>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={fetchHistory}
            className="px-4 py-2 text-sm font-medium text-[#232323] hover:bg-gray-50 rounded-xl transition-colors"
          >
            Try Again
          </button>
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-gray-500 hover:text-gray-700 transition-colors"
          >
            Close
          </button>
        </div>
      </div>
    );
  }

  // ── Render ───────────────────────────────────────────────────────────

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-lg font-semibold text-[#232323] mb-1">
            Import History
          </h2>
          <p className="text-sm text-gray-500">
            View past import sessions and their results.
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-gray-600 hover:text-[#232323] hover:bg-gray-50 rounded-xl transition-colors"
        >
          <X className="w-4 h-4" />
          Close
        </button>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-3 mb-6">
        <Filter className="w-4 h-4 text-gray-400 flex-shrink-0" />

        <select
          value={dataTypeFilter}
          onChange={(e) => setDataTypeFilter(e.target.value)}
          className="px-3 py-2 text-sm border border-gray-200 rounded-xl bg-white text-[#232323] focus:outline-none focus:ring-2 focus:ring-gray-200"
          aria-label="Filter by data type"
        >
          {DATA_TYPE_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>

        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="px-3 py-2 text-sm border border-gray-200 rounded-xl bg-white text-[#232323] focus:outline-none focus:ring-2 focus:ring-gray-200"
          aria-label="Filter by status"
        >
          {STATUS_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>

        {(dataTypeFilter || statusFilter) && (
          <button
            type="button"
            onClick={() => {
              setDataTypeFilter("");
              setStatusFilter("");
            }}
            className="text-xs text-gray-400 hover:text-gray-600 transition-colors"
          >
            Clear filters
          </button>
        )}
      </div>

      {/* Empty state */}
      {sessions.length === 0 && (
        <div className="flex flex-col items-center justify-center py-16 text-gray-400">
          <Database className="w-12 h-12 mb-4" />
          <p className="text-sm font-medium mb-1">No import sessions found</p>
          <p className="text-xs">
            {dataTypeFilter || statusFilter
              ? "Try adjusting your filters."
              : "Completed imports will appear here."}
          </p>
        </div>
      )}

      {/* Sessions table */}
      {sessions.length > 0 && (
        <div className="rounded-xl border border-gray-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="w-8" />
                <th className="text-left px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Date
                </th>
                <th className="text-left px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Data Type
                </th>
                <th className="text-left px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Source
                </th>
                <th className="text-left px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Source Name
                </th>
                <th className="text-right px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Total
                </th>
                <th className="text-right px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Imported
                </th>
                <th className="text-left px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Status
                </th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((session, idx) => {
                const isExpanded = expandedId === session.session_id;

                return (
                  <SessionRow
                    key={session.session_id}
                    session={session}
                    isExpanded={isExpanded}
                    isLast={idx === sessions.length - 1}
                    onToggle={() => toggleExpand(session.session_id)}
                  />
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Session Row ─────────────────────────────────────────────────────────────

function SessionRow({
  session,
  isExpanded,
  isLast,
  onToggle,
}: {
  session: ImportSessionRecord;
  isExpanded: boolean;
  isLast: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr
        onClick={onToggle}
        className={`cursor-pointer hover:bg-gray-50 transition-colors ${
          !isLast && !isExpanded ? "border-b border-gray-100" : ""
        } ${isExpanded ? "bg-gray-50" : ""}`}
      >
        <td className="pl-3 py-3">
          {isExpanded ? (
            <ChevronDown className="w-4 h-4 text-gray-400" />
          ) : (
            <ChevronRight className="w-4 h-4 text-gray-400" />
          )}
        </td>
        <td className="px-4 py-3 text-gray-600 whitespace-nowrap">
          {formatDate(session.created_at)}
        </td>
        <td className="px-4 py-3 text-[#232323] font-medium whitespace-nowrap">
          {DATA_TYPE_LABELS[session.data_type] ?? session.data_type}
        </td>
        <td className="px-4 py-3 text-gray-600 whitespace-nowrap">
          {SOURCE_TYPE_LABELS[session.source_type] ?? session.source_type}
        </td>
        <td className="px-4 py-3 text-gray-600 truncate max-w-[200px]" title={session.source_name}>
          {session.source_name}
        </td>
        <td className="px-4 py-3 text-right text-gray-600 tabular-nums">
          {session.total_records}
        </td>
        <td className="px-4 py-3 text-right text-gray-600 tabular-nums">
          {session.imported_records}
        </td>
        <td className="px-4 py-3">
          <StatusBadge status={session.status} />
        </td>
      </tr>
      {isExpanded && (
        <tr>
          <td colSpan={8} className={!isLast ? "border-b border-gray-100" : ""}>
            <SessionDetail session={session} />
          </td>
        </tr>
      )}
    </>
  );
}
