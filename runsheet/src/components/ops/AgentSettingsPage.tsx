"use client";

/**
 * Agent Settings page — Autonomy Configuration, Memory Management & Feedback.
 *
 * Three-section layout:
 * - Autonomy: Display/update autonomy level with radio options and confirm button
 * - Memory: Paginated list with type/tag filters and delete with confirmation
 * - Feedback: Paginated feedback entries with stats cards and type/date filters
 *
 * Validates:
 * - Requirement 3.1: Display current autonomy level for the tenant
 * - Requirement 3.2: Four radio options with descriptions
 * - Requirement 3.3: Confirm change, display previous and new levels
 * - Requirement 3.4: Read-only mode for non-admin users
 * - Requirement 3.5: Handle 403 errors without modifying displayed level
 * - Requirement 4.1: Paginated memory list via getMemories
 * - Requirement 4.2: Filter by memory_type and tags
 * - Requirement 4.3: Delete with confirmation dialog
 * - Requirement 4.4: Remove entry from list on success without full reload
 * - Requirement 4.5: Handle 404 on delete with "memory not found" message
 * - Requirement 7.1: Feedback section below Memory Management
 * - Requirement 7.2: Paginated feedback entries from GET /agent/feedback
 * - Requirement 7.3: Feedback stats (approval rate, rejection rate, total actions)
 * - Requirement 7.4: Filter controls for feedback type and date range
 * - Requirement 7.5: Load both feedback list and stats on mount
 * - Requirement 7.6: Inline error message on feedback API failure
 * - Requirement 7.7: Retain existing autonomy and memory functionality
 */

import {
  BarChart3,
  Brain,
  Calendar,
  ChevronLeft,
  ChevronRight,
  Filter,
  Loader2,
  MessageSquare,
  Settings,
  Shield,
  ShieldAlert,
  Tag,
  ThumbsDown,
  ThumbsUp,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../services/api";
import type {
  AutonomyLevel,
  AutonomyUpdateResponse,
  FeedbackEntry,
  FeedbackFilters,
  FeedbackStats,
  MemoryEntry,
  MemoryFilters,
  PaginatedFeedback,
  PaginatedMemories,
} from "../../services/agentApi";
import {
  deleteMemory,
  getAutonomyLevel,
  getFeedback,
  getFeedbackStats,
  getMemories,
  updateAutonomyLevel,
} from "../../services/agentApi";

// ─── Constants ───────────────────────────────────────────────────────────────

const TENANT_ID = "default";
const PAGE_SIZE = 10;

const AUTONOMY_OPTIONS: {
  value: AutonomyLevel;
  label: string;
  description: string;
}[] = [
  {
    value: "suggest-only",
    label: "Suggest Only",
    description:
      "Agents provide recommendations but take no autonomous actions. All changes require manual approval.",
  },
  {
    value: "auto-low",
    label: "Auto — Low",
    description:
      "Agents can execute low-risk actions automatically. Medium and high-risk actions require approval.",
  },
  {
    value: "auto-medium",
    label: "Auto — Medium",
    description:
      "Agents can execute low and medium-risk actions automatically. Only high-risk actions require approval.",
  },
  {
    value: "full-auto",
    label: "Full Auto",
    description:
      "Agents execute all actions autonomously. Use with caution — no approval gates are enforced.",
  },
];

// ─── Delete Confirmation Dialog ──────────────────────────────────────────────

interface DeleteConfirmDialogProps {
  memoryId: string;
  onConfirm: () => void;
  onCancel: () => void;
  deleting: boolean;
}

function DeleteConfirmDialog({
  memoryId,
  onConfirm,
  onCancel,
  deleting,
}: DeleteConfirmDialogProps) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-[#232323]">
            Delete Memory
          </h2>
          <button
            onClick={onCancel}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
            aria-label="Close delete confirmation"
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="px-6 py-4">
          <p className="text-sm text-gray-600">
            Are you sure you want to delete memory{" "}
            <span className="font-medium text-[#232323]">{memoryId}</span>? This
            action cannot be undone.
          </p>
        </div>
        <div className="flex justify-end gap-3 px-6 py-4 border-t border-gray-100">
          <button
            type="button"
            onClick={onCancel}
            disabled={deleting}
            className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={deleting}
            className="px-4 py-2 text-sm text-white bg-red-600 hover:bg-red-700 rounded-lg disabled:opacity-50"
          >
            {deleting ? "Deleting..." : "Delete"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Autonomy Configuration Section ──────────────────────────────────────────

interface AutonomySectionProps {
  isAdmin: boolean;
}

function AutonomySection({ isAdmin }: AutonomySectionProps) {
  const [currentLevel, setCurrentLevel] = useState<AutonomyLevel | null>(null);
  const [selectedLevel, setSelectedLevel] = useState<AutonomyLevel | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [successInfo, setSuccessInfo] =
    useState<AutonomyUpdateResponse | null>(null);

  const loadLevel = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await getAutonomyLevel(TENANT_ID);
      setCurrentLevel(result.level);
      setSelectedLevel(result.level);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Failed to load autonomy level",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadLevel();
  }, [loadLevel]);

  const handleConfirm = useCallback(async () => {
    if (!selectedLevel || selectedLevel === currentLevel) return;
    setSaving(true);
    setError("");
    setSuccessInfo(null);
    try {
      const result = await updateAutonomyLevel(selectedLevel, TENANT_ID);
      setCurrentLevel(selectedLevel);
      setSuccessInfo(result);
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        setError("Access denied. Admin privileges are required to change the autonomy level.");
      } else {
        setError(
          err instanceof Error
            ? err.message
            : "Failed to update autonomy level",
        );
      }
      // Revert selection on error — do not modify displayed level
      setSelectedLevel(currentLevel);
    } finally {
      setSaving(false);
    }
  }, [selectedLevel, currentLevel]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="w-6 h-6 text-gray-400 animate-spin" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Shield className="w-4 h-4 text-gray-500" />
        <h3 className="text-sm font-semibold text-[#232323]">
          Autonomy Configuration
        </h3>
      </div>

      {!isAdmin && (
        <div className="flex items-center gap-2 px-4 py-3 bg-yellow-50 border border-yellow-200 rounded-lg">
          <ShieldAlert className="w-4 h-4 text-yellow-600 flex-shrink-0" />
          <p className="text-sm text-yellow-700">
            Admin access required to change autonomy settings. Current level is
            displayed as read-only.
          </p>
        </div>
      )}

      {error && (
        <p className="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg">
          {error}
        </p>
      )}

      {successInfo && (
        <div className="px-4 py-3 bg-green-50 border border-green-200 rounded-lg">
          <p className="text-sm text-green-700">
            Autonomy level updated successfully.
          </p>
          <p className="text-xs text-green-600 mt-1">
            Previous: <span className="font-medium">{successInfo.previous_level}</span>
            {" → "}
            New: <span className="font-medium">{successInfo.new_level}</span>
          </p>
        </div>
      )}

      {/* Current level display */}
      {currentLevel && (
        <div className="text-xs text-gray-500">
          Current level:{" "}
          <span className="font-medium text-[#232323]">
            {AUTONOMY_OPTIONS.find((o) => o.value === currentLevel)?.label ??
              currentLevel}
          </span>
        </div>
      )}

      {/* Radio options */}
      <div className="space-y-3">
        {AUTONOMY_OPTIONS.map((option) => {
          const isSelected = selectedLevel === option.value;
          const isCurrent = currentLevel === option.value;
          return (
            <label
              key={option.value}
              className={`flex items-start gap-3 p-4 border rounded-lg cursor-pointer transition-colors ${
                isSelected
                  ? "border-gray-400 bg-gray-50"
                  : "border-gray-100 hover:border-gray-200"
              } ${!isAdmin ? "opacity-70 cursor-not-allowed" : ""}`}
            >
              <input
                type="radio"
                name="autonomy-level"
                value={option.value}
                checked={isSelected}
                onChange={() => {
                  if (isAdmin) {
                    setSelectedLevel(option.value);
                    setSuccessInfo(null);
                  }
                }}
                disabled={!isAdmin}
                className="mt-0.5 accent-[#232323]"
              />
              <div className="flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-[#232323]">
                    {option.label}
                  </span>
                  {isCurrent && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-blue-50 text-blue-600">
                      current
                    </span>
                  )}
                </div>
                <p className="text-xs text-gray-500 mt-1">
                  {option.description}
                </p>
              </div>
            </label>
          );
        })}
      </div>

      {/* Confirm button */}
      {isAdmin && (
        <div className="flex justify-end">
          <button
            onClick={handleConfirm}
            disabled={
              saving || !selectedLevel || selectedLevel === currentLevel
            }
            className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white rounded-lg disabled:opacity-50"
            style={{ backgroundColor: "#232323" }}
          >
            {saving ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Shield className="w-4 h-4" />
            )}
            {saving ? "Updating..." : "Confirm Change"}
          </button>
        </div>
      )}
    </div>
  );
}

// ─── Memory Management Section ───────────────────────────────────────────────

function MemorySection() {
  const [memories, setMemories] = useState<MemoryEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [deleteError, setDeleteError] = useState("");

  // Filters
  const [typeFilter, setTypeFilter] = useState<
    "" | "pattern" | "preference"
  >("");
  const [tagsFilter, setTagsFilter] = useState("");

  // Delete confirmation
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const loadMemories = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const filters: MemoryFilters = {
        tenant_id: TENANT_ID,
        page,
        size: PAGE_SIZE,
      };
      if (typeFilter) filters.memory_type = typeFilter;
      if (tagsFilter.trim()) filters.tags = tagsFilter.trim();

      const result = await getMemories(filters) as any;
      setMemories(result.entries ?? result.items ?? result.data ?? []);
      setTotal(result.total ?? result.pagination?.total ?? 0);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load memories",
      );
    } finally {
      setLoading(false);
    }
  }, [page, typeFilter, tagsFilter]);

  useEffect(() => {
    loadMemories();
  }, [loadMemories]);

  const handleDelete = useCallback(async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteError("");
    try {
      await deleteMemory(deleteTarget, TENANT_ID);
      // Remove from list without full reload (Requirement 4.4)
      setMemories((prev) =>
        prev.filter((m) => m.memory_id !== deleteTarget),
      );
      setTotal((prev) => prev - 1);
      setDeleteTarget(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setDeleteError("Memory not found. It may have already been deleted.");
        // Also remove from list since it doesn't exist
        setMemories((prev) =>
          prev.filter((m) => m.memory_id !== deleteTarget),
        );
        setTotal((prev) => prev - 1);
      } else {
        setDeleteError(
          err instanceof Error ? err.message : "Failed to delete memory",
        );
      }
      setDeleteTarget(null);
    } finally {
      setDeleting(false);
    }
  }, [deleteTarget]);

  const handlePageChange = useCallback((newPage: number) => {
    setPage(newPage);
  }, []);

  const inputClass =
    "px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Brain className="w-4 h-4 text-gray-500" />
        <h3 className="text-sm font-semibold text-[#232323]">
          Memory Management
        </h3>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex items-center gap-1.5">
          <Filter className="w-3.5 h-3.5 text-gray-400" />
          <select
            value={typeFilter}
            onChange={(e) => {
              setTypeFilter(
                e.target.value as "" | "pattern" | "preference",
              );
              setPage(1);
            }}
            className={inputClass}
          >
            <option value="">All types</option>
            <option value="pattern">Pattern</option>
            <option value="preference">Preference</option>
          </select>
        </div>
        <div className="flex items-center gap-1.5">
          <Tag className="w-3.5 h-3.5 text-gray-400" />
          <input
            type="text"
            value={tagsFilter}
            onChange={(e) => {
              setTagsFilter(e.target.value);
              setPage(1);
            }}
            placeholder="Filter by tags..."
            className={inputClass}
          />
        </div>
      </div>

      {error && (
        <p className="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg">
          {error}
        </p>
      )}

      {deleteError && (
        <p className="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg">
          {deleteError}
        </p>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-6 h-6 text-gray-400 animate-spin" />
        </div>
      ) : !memories || memories.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-gray-400">
          <Brain className="w-8 h-8 mb-2" />
          <p className="text-sm">No memories found</p>
          <p className="text-xs mt-1">
            Try adjusting your filters or check back later
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {memories.map((memory) => (
            <div
              key={memory.memory_id}
              className="flex items-start justify-between border border-gray-100 rounded-lg p-4 hover:border-gray-200 transition-colors"
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span
                    className={`inline-flex items-center text-[10px] px-1.5 py-0.5 rounded font-medium ${
                      memory.memory_type === "pattern"
                        ? "bg-purple-50 text-purple-600"
                        : "bg-blue-50 text-blue-600"
                    }`}
                  >
                    {memory.memory_type}
                  </span>
                  <span className="text-[10px] text-gray-400">
                    {memory.memory_id}
                  </span>
                </div>
                <p className="text-sm text-[#232323] mb-1.5 break-words">
                  {memory.content}
                </p>
                <div className="flex items-center gap-2 flex-wrap">
                  {memory.tags.map((tag) => (
                    <span
                      key={tag}
                      className="inline-flex items-center text-[10px] px-1.5 py-0.5 bg-gray-100 text-gray-600 rounded"
                    >
                      {tag}
                    </span>
                  ))}
                  <span className="text-[10px] text-gray-400">
                    {new Date(memory.created_at).toLocaleString()}
                  </span>
                </div>
              </div>
              <button
                onClick={() => setDeleteTarget(memory.memory_id)}
                className="ml-3 p-1.5 text-gray-400 hover:text-red-500 rounded hover:bg-red-50 transition-colors flex-shrink-0"
                aria-label={`Delete memory ${memory.memory_id}`}
              >
                <Trash2 className="w-4 h-4" />
              </button>
            </div>
          ))}

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between px-4 py-3 border-t border-gray-100">
              <span className="text-xs text-gray-500">
                Page {page} of {totalPages} ({total} total)
              </span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => handlePageChange(page - 1)}
                  disabled={page <= 1}
                  className="p-1.5 text-gray-400 hover:text-gray-600 disabled:opacity-30 disabled:cursor-not-allowed rounded"
                  aria-label="Previous page"
                >
                  <ChevronLeft className="w-4 h-4" />
                </button>
                <button
                  onClick={() => handlePageChange(page + 1)}
                  disabled={page >= totalPages}
                  className="p-1.5 text-gray-400 hover:text-gray-600 disabled:opacity-30 disabled:cursor-not-allowed rounded"
                  aria-label="Next page"
                >
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Delete confirmation dialog */}
      {deleteTarget && (
        <DeleteConfirmDialog
          memoryId={deleteTarget}
          onConfirm={handleDelete}
          onCancel={() => setDeleteTarget(null)}
          deleting={deleting}
        />
      )}
    </div>
  );
}

// ─── Feedback Section ────────────────────────────────────────────────────────

const FEEDBACK_PAGE_SIZE = 10;

function FeedbackSection() {
  const [entries, setEntries] = useState<FeedbackEntry[]>([]);
  const [stats, setStats] = useState<FeedbackStats | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Filters
  const [typeFilter, setTypeFilter] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");

  const totalPages = Math.max(1, Math.ceil(total / FEEDBACK_PAGE_SIZE));

  const loadFeedback = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const filters: FeedbackFilters = {
        tenant_id: TENANT_ID,
        page,
        size: FEEDBACK_PAGE_SIZE,
      };
      if (typeFilter) filters.feedback_type = typeFilter;
      if (startDate) filters.start_date = startDate;
      if (endDate) filters.end_date = endDate;

      const [feedbackResult, statsResult] = await Promise.allSettled([
        getFeedback(filters),
        getFeedbackStats(TENANT_ID),
      ]);

      if (feedbackResult.status === "fulfilled") {
        setEntries(feedbackResult.value.entries ?? []);
        setTotal(feedbackResult.value.total ?? 0);
      } else {
        throw feedbackResult.reason;
      }

      if (statsResult.status === "fulfilled") {
        setStats(statsResult.value);
      }
      // Stats failure is non-critical — we still show entries
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load feedback",
      );
    } finally {
      setLoading(false);
    }
  }, [page, typeFilter, startDate, endDate]);

  useEffect(() => {
    loadFeedback();
  }, [loadFeedback]);

  const handlePageChange = useCallback((newPage: number) => {
    setPage(newPage);
  }, []);

  const feedbackTypeBadge = (type: string) => {
    switch (type) {
      case "positive":
        return (
          <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded font-medium bg-green-50 text-green-600">
            <ThumbsUp className="w-3 h-3" />
            positive
          </span>
        );
      case "negative":
        return (
          <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded font-medium bg-red-50 text-red-600">
            <ThumbsDown className="w-3 h-3" />
            negative
          </span>
        );
      case "correction":
        return (
          <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded font-medium bg-yellow-50 text-yellow-600">
            <MessageSquare className="w-3 h-3" />
            correction
          </span>
        );
      default:
        return (
          <span className="inline-flex items-center text-[10px] px-1.5 py-0.5 rounded font-medium bg-gray-50 text-gray-600">
            {type}
          </span>
        );
    }
  };

  const inputClass =
    "px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <BarChart3 className="w-4 h-4 text-gray-500" />
        <h3 className="text-sm font-semibold text-[#232323]">
          Feedback
        </h3>
      </div>

      {/* Stats Cards */}
      {stats && (
        <div className="grid grid-cols-3 gap-4">
          <div className="bg-gray-50 rounded-lg p-4">
            <p className="text-xs text-gray-500 mb-1">Total Feedback</p>
            <p className="text-xl font-semibold text-[#232323]">
              {stats.total_feedback ?? stats.total_actions ?? 0}
            </p>
          </div>
          <div className="bg-green-50 rounded-lg p-4">
            <p className="text-xs text-gray-500 mb-1">Approval Rate</p>
            <p className="text-xl font-semibold text-green-700">
              {(() => {
                const rate = stats.approval_rate ?? (stats.total_feedback > 0 ? (1 - stats.rejection_rate) : 0);
                return Number.isFinite(rate) ? (rate * 100).toFixed(1) : "0.0";
              })()}%
            </p>
          </div>
          <div className="bg-red-50 rounded-lg p-4">
            <p className="text-xs text-gray-500 mb-1">Rejection Rate</p>
            <p className="text-xl font-semibold text-red-700">
              {Number.isFinite(stats.rejection_rate) ? (stats.rejection_rate * 100).toFixed(1) : "0.0"}%
            </p>
          </div>
        </div>
      )}

      {/* Filters */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex items-center gap-1.5">
          <Filter className="w-3.5 h-3.5 text-gray-400" />
          <select
            value={typeFilter}
            onChange={(e) => {
              setTypeFilter(e.target.value);
              setPage(1);
            }}
            className={inputClass}
          >
            <option value="">All types</option>
            <option value="positive">Positive</option>
            <option value="negative">Negative</option>
            <option value="correction">Correction</option>
          </select>
        </div>
        <div className="flex items-center gap-1.5">
          <Calendar className="w-3.5 h-3.5 text-gray-400" />
          <input
            type="date"
            value={startDate}
            onChange={(e) => {
              setStartDate(e.target.value);
              setPage(1);
            }}
            placeholder="Start date"
            className={inputClass}
            aria-label="Start date"
          />
        </div>
        <div className="flex items-center gap-1.5">
          <Calendar className="w-3.5 h-3.5 text-gray-400" />
          <input
            type="date"
            value={endDate}
            onChange={(e) => {
              setEndDate(e.target.value);
              setPage(1);
            }}
            placeholder="End date"
            className={inputClass}
            aria-label="End date"
          />
        </div>
      </div>

      {error && (
        <p className="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg">
          {error}
        </p>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-6 h-6 text-gray-400 animate-spin" />
        </div>
      ) : entries.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-gray-400">
          <MessageSquare className="w-8 h-8 mb-2" />
          <p className="text-sm">No feedback entries found</p>
          <p className="text-xs mt-1">
            Try adjusting your filters or check back later
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {/* Feedback entries table */}
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100">
                  <th className="text-left px-4 py-2 text-xs font-medium text-gray-500">
                    Type
                  </th>
                  <th className="text-left px-4 py-2 text-xs font-medium text-gray-500">
                    Action ID
                  </th>
                  <th className="text-left px-4 py-2 text-xs font-medium text-gray-500">
                    Comment
                  </th>
                  <th className="text-left px-4 py-2 text-xs font-medium text-gray-500">
                    Created
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {entries.map((entry) => (
                  <tr
                    key={entry.feedback_id}
                    className="hover:bg-gray-50 transition-colors"
                  >
                    <td className="px-4 py-3">
                      {feedbackTypeBadge(entry.feedback_type)}
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-600 font-mono">
                      {entry.action_id}
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-600 max-w-xs truncate">
                      {entry.comment || "—"}
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-400">
                      {new Date(entry.created_at).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between px-4 py-3 border-t border-gray-100">
              <span className="text-xs text-gray-500">
                Page {page} of {totalPages} ({total} total)
              </span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => handlePageChange(page - 1)}
                  disabled={page <= 1}
                  className="p-1.5 text-gray-400 hover:text-gray-600 disabled:opacity-30 disabled:cursor-not-allowed rounded"
                  aria-label="Previous feedback page"
                >
                  <ChevronLeft className="w-4 h-4" />
                </button>
                <button
                  onClick={() => handlePageChange(page + 1)}
                  disabled={page >= totalPages}
                  className="p-1.5 text-gray-400 hover:text-gray-600 disabled:opacity-30 disabled:cursor-not-allowed rounded"
                  aria-label="Next feedback page"
                >
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Main Page Component ─────────────────────────────────────────────────────

export default function AgentSettingsPage() {
  // In a real app, this would come from an auth context or user session.
  // For now, default to admin=true; the component supports read-only mode.
  const [isAdmin] = useState(true);

  return (
    <div className="flex-1 flex flex-col h-full bg-gray-50">
      {/* Header */}
      <div className="px-6 pt-6 pb-4">
        <div className="flex items-center gap-3 mb-1">
          <div className="w-9 h-9 bg-gray-700 rounded-lg flex items-center justify-center">
            <Settings className="w-5 h-5 text-white" />
          </div>
          <div>
            <h2 className="text-lg font-semibold text-[#232323]">
              Agent Settings
            </h2>
            <p className="text-xs text-gray-500">
              Configure autonomy levels, manage agent memory, and review feedback
            </p>
          </div>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 min-h-0 overflow-auto px-6 pb-6">
        <div className="space-y-8">
          {/* Autonomy Configuration */}
          <div className="bg-white border border-gray-200 rounded-lg p-6">
            <AutonomySection isAdmin={isAdmin} />
          </div>

          {/* Memory Management */}
          <div className="bg-white border border-gray-200 rounded-lg p-6">
            <MemorySection />
          </div>

          {/* Feedback */}
          <div className="bg-white border border-gray-200 rounded-lg p-6">
            <FeedbackSection />
          </div>
        </div>
      </div>
    </div>
  );
}
